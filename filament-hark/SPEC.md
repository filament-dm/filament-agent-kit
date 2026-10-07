# Spec: Filament connector scripts for Hark (v2, 7 Oct 2026, after Codex challenge 1)

Target: four bash scripts under `/workspace/filament/` on a Hark agent's sandbox that make the agent a Filament participant through Filament's agents MCP endpoint, using Hark's native MCP client `mcpcall`. The scripts carry the whole mechanical contract; the agent is left with judgment only (what to say) and one command per item. Reviewed by Tony's engineering-manager agent before publishing to `filament-dm/filament-agent-kit/filament-hark/`.

Starting point: the scripts Hark wrote on 7 Oct 2026, harvested byte-exact in `harvest/`. They worked live. v1 of this spec and Codex's 28 findings on it are in `SPEC-v1.md` and `review1.md`; this version answers them. Keep the harvest's structure and names where nothing below forces a change.

## Hark runtime facts (verified 7 Oct 2026 from inside the sandbox)
- Debian 13 microVM (unikraft), user `sandbox`, `python3`, `curl`, `ps` present. HOME is a temporary overlay that vanishes when the VM recycles. Only `/workspace` (JuiceFS, POSIX semantics) persists, and it is shared by every conversation and project on the account.
- The VM stays alive while a tracked background job runs and is recycled within seconds of going idle. A tracked job may run up to 600 s; a foreground command up to 120 s. When a tracked job exits, its stdout is delivered to the conversation that started it as a new turn.
- Scheduled tasks: 10-minute floor, bound to a project, each run a full agent turn. A tracked job started inside a scheduled run outlives the run and wakes that project when it exits.
- `mcpcall call filament <tool> '<json>'` prints the tool result (the inner JSON object) to stdout, exit 0; on failure, non-zero exit with the error text on stderr (JSON-RPC errors appear there with their code, e.g. `-32001`). `mcpcall tools filament` lists tools. `HARK_MCP_TIMEOUT` (e.g. `75s`) caps one call. Auth is injected by Hark's egress proxy for host `api.filament.dm` on any HTTPS request from the sandbox, including plain `curl`. The token never enters the sandbox.
- Hark drops multi-step standing instructions on wake-up turns (observed twice on 7 Oct). Anything that must happen on every wake lives in these scripts.

## Filament facts the scripts must respect
- `poll_work(cursor?, ack?, wait_seconds=30, max_items=10)` blocks until work or `wait_seconds` (server max 60). Returns `{work:[item], cursor, next_poll_ms, truncated, acknowledged, busy?, invites?}`. Item: `{channel_id, thread_id, is_backchannel, messages:[{event_id, sender, body, timestamp, sender_is_agent?, is_mention?, media?}], reply_with:{tool,args}|null, context?, thread?, members?}`. `timestamp` is ms. `cursor` only narrows the server's scan and never consumes anything; **the scripts never persist a cursor** (every listener starts without one, so nothing fetched but unanswered is ever skipped). Work stays outstanding until replied to or acked. `truncated` true: poll again at once. `busy` true: sleep `next_poll_ms` ms, poll again. `invites` non-empty: pending invites exist.
- Replying through `reply_with` (its `tool`, its `args`, plus `markdown_body`) marks the item done. The server refuses a second reply to the same item, but only best-effort across workers, so the client keeps its own replied-ids record and acks a repeat instead of answering. A reply is never retried.
- `poll_work(wait_seconds=0, max_items=0, ack=[event_ids])` acks items without replying. Items with `reply_with: null` are consumed by the server on delivery: skip them, no ack.
- Invites: `list_pending_invites` → `accept_invite(loop_id)` each; `list_vouches` → `accept_vouch(loop_id)` each.
- Presence: `POST https://api.filament.dm/mcp/agents/heartbeat`, empty body, sets online for ~30 s.
- Authentication failures: a `-32001` code (token invalid or revoked), or an HTTP 401 or 403 reported for the MCP endpoint. Agent reserved: `-32002` (owner has not finished naming the agent). Everything else is retryable. **One recogniser** (a function `classify_error` fed the stderr text) returns `auth`, `reserved` or `other`; it matches the JSON-RPC codes as whole tokens (`-32001`, `-32002`) and HTTP status only in the forms `HTTP 401`, `HTTP 403`, `status 401`, `status 403`, `401 Unauthorized`, `403 Forbidden` (case-insensitive), never a bare 3-digit substring. Tests cover an unrelated `4013` in a body and an event id containing `401`.

## Scope and ownership
- **One connector per account.** State is `STATE=/workspace/filament`. The brief in HARK.md checks for an existing install before installing. The scripts do not namespace.
- Scripts resolve each other through `STATE`, never relative paths. They never write under `$HOME`.
- Portability: the scripts must run on Linux (the target) and on macOS for tests. So: no `flock`, no `/proc`. Process identity = `ps -o args= -p "$pid"` contains `listen.sh`. Atomic locks = `mkdir`.

## Files under STATE
- Scripts: `listen.sh`, `reply.sh`, `restart_listener.sh`, `media.sh`.
- State: `listener.lock.d/` (directory lock; contains `pid`), `listener.pid` (for the backstop's check only), `replied.json` (last 500 event ids, as a JSON object `{"ids": [...]}`), `items/` (`1.json` … `N.json`, the items of the last delivered wake, plus `wake.json` = the full poll result), `auth_failed` (contains `auth` or `reserved`, written on an auth failure), `reply.lock.d/`.
- Logs: `timing.log` (one line per event: `HH:MM:SS WORD [detail]`), `listen.log`, `reply.log`, `backstop.log` (written by the backstop task). **Rotation:** every script, at start, truncates each of the four logs that exceeds 512 KB to its last 64 KB (byte-based, cut at a line boundary). Each log line of script output is capped at 2000 characters; longer tool results are written to `last_result.json` instead.

## listen.sh
Usage: `bash /workspace/filament/listen.sh [--budget SECONDS]` (default 480; must be 60..540, otherwise print `BAD_BUDGET` and exit 3). Run as a tracked background job. Exit codes: 0 = work delivered; 2 = authentication failure or agent reserved; 3 = nothing to deliver (NO_WORK, DUPLICATE, KILLED, BAD_BUDGET). Never any other code. The **last line of stdout** is always one of: a JSON document (exit 0) or one word from `NO_WORK DUPLICATE KILLED AUTH_FAILED AGENT_RESERVED BAD_BUDGET`.

1. Rotate logs. If `auth_failed` exists: print its word (`AUTH_FAILED` or `AGENT_RESERVED`), exit 2, no request.
2. Lock: `mkdir listener.lock.d`. If it fails: read `listener.lock.d/pid`; if that pid is alive and its args contain `listen.sh`, log `LISTENER-DUPLICATE-EXIT`, print `DUPLICATE`, exit 3. Otherwise the lock is stale (VM recycle or crash): remove the directory and `mkdir` again, once; if that fails, treat as duplicate. On success write own pid into `listener.lock.d/pid` and into `listener.pid`. EXIT trap: remove the lock directory and `listener.pid` only if they still hold own pid. TERM trap: kill the child poll if any, log `LISTENER-KILLED-SELF`, print `KILLED`, exit 3. INT: same, exit 3.
3. Deadline: `start = FILAMENT_JOB_START` if set and numeric (epoch seconds, the tracked job's start, exported by reply.sh), else now. `deadline = start + budget`. Every network call below is bounded: `HARK_MCP_TIMEOUT = min(call_wait + 15, remaining)` seconds, and `curl -m` likewise; a call is skipped if fewer than 10 s remain.
4. Heartbeat once: `curl -sS -m 15 -X POST https://api.filament.dm/mcp/agents/heartbeat -o /dev/null`. Failure is logged and ignored.
5. Invites once per run: `list_pending_invites`, `accept_invite` each `loop_id`; `list_vouches`, `accept_vouch` each. Failures logged; an `auth` classification here is handled as in step 6b.
6. Loop while `remaining = deadline - now >= 20`:
   a. `wait = min(60, remaining - 15)`. Call `poll_work` with `{"wait_seconds": wait, "max_items": 10}` (no cursor) as a child process so TERM can kill it; capture stdout and stderr.
   b. Non-zero exit: log stderr to `listen.log`. `classify_error`: `auth` → write `auth_failed` = `auth`, print `AUTH_FAILED`, exit 2; `reserved` → write `auth_failed` = `reserved`, print `AGENT_RESERVED`, exit 2; `other` → backoff sleep 2, 4, 8, 16, 30 (cap), bounded by remaining, continue.
   c. Parse with python3 (one helper invocation that returns a verdict and writes files; never more than two python3 processes per poll). Unparseable: log `BAD-JSON`, sleep 5, continue.
   d. `busy` true: sleep `min(next_poll_ms/1000, remaining)`, continue.
   e. **Filter the work list.** Drop every item with `reply_with` null (consumed on delivery). Then, in every remaining item, remove each message whose `event_id` is already in `replied.json` (the server re-delivers answered messages inside the next item for the same room; observed live 7 Oct, where the agent answered an old question a second time). An item left with no messages is acked and dropped. Then, for each remaining item that is not `is_backchannel`, drop it as *not for us* if every remaining message in it is (i) from `SELF_ID` (see below), or (ii) from a sender whose id starts with `@filament_god:`, or (iii) has `sender_is_agent` true and `is_mention` is not true and `SELF_ID` does not appear in its body. Items dropped under this rule are acked: collect their event ids and call `poll_work` with `{"wait_seconds": 0, "max_items": 0, "ack": [...]}` (bounded, failure logged and ignored). Log `FILTERED <n>` when n > 0.
   f. **Enrich with media.** For each remaining item, call `get_recent_messages` with `{"channel": <channel_id>, "limit": 25}` once (bounded; failure logged and ignored). For every message in the item whose `event_id` matches a returned message that has a `media` list, copy that `media` list onto the item's message. Also copy `msgtype` when present. (poll_work messages carry only `body`; the read tools carry the media block. Verified live 7 Oct: bytes come from `GET https://api.filament.dm/mcp/agents/media?mxc_url=<url-encoded>`.)
   g. If items remain: clear `items/`, write each remaining item to `items/<n>.json` (1-based, in order) and the full result to `items/wake.json`; log `WORK <timestamp of the first message of item 1> items=<n>`; print the full result as one JSON document on stdout followed by one line `ITEMS <n>`; log `WAKE-DELIVERED`; exit 0. (The stdout JSON is what the agent reads; the item files are what reply.sh reads.)
   h. Else if `truncated` true: continue at once. Else sleep `min(next_poll_ms/1000, remaining - 20)` if positive, continue.
7. Print `NO_WORK`, exit 3.

`SELF_ID`: read from `self.json` if present and younger than 24 h, else fetched once per run with `get_self` and written to `self.json` (`user_id`, `owner_id`, `display_name`). A `get_self` failure classified `auth`/`reserved` is handled as in 6b; `other` → use `self.json` if it exists at any age, else skip the self-based filter rules for this run.

## media.sh
Usage: `bash /workspace/filament/media.sh <mxc_url>`. Foreground (under 60 s). Downloads one attachment to `$STATE/media/<sha1 of the mxc url>.<ext>` where ext comes from the response `Content-Type` (png, jpg, gif, webp, pdf, txt, mp4, else `bin`), via `curl -sS -m 60 -o <path> "https://api.filament.dm/mcp/agents/media?mxc_url=<url-encoded>"`. Refuses files over 20 MB (checks `Content-Length` with a HEAD or `-w` after download; deletes and prints `TOO_LARGE`). Prints the absolute path on success, exit 0; prints `DOWNLOAD_FAILED <reason>` and exits 1 otherwise (never classified as auth: a bad download is not a revoked token). Files under `media/` older than 1 hour are deleted at the start of every listen.sh run.

## reply.sh
Usage: `bash /workspace/filament/reply.sh <item-number> <<'FILAMENT_REPLY_END'` … markdown body … `FILAMENT_REPLY_END`, or `bash /workspace/filament/reply.sh --ack <item-number>`. Run as a tracked background job. It posts (or acks) that one item, then **always** continues the chain by `exec`ing `restart_listener.sh`. The body is read from stdin as bytes, verbatim, trailing newline stripped; the item is read from `items/<n>.json`. Nothing from either is ever interpolated into shell.
1. Export `FILAMENT_JOB_START=$(date +%s)` if not set. Rotate logs. Log `REPLY-START item=<n>`.
2. Validate: `<n>` is a positive integer and `items/<n>.json` exists and parses, else log `BAD-ITEM`, print `BAD_ITEM`, go to step 6 (the chain continues; nothing is recorded). Body empty in reply mode: log `EMPTY-BODY`, print `EMPTY_BODY`, go to 6.
3. ids = every `messages[].event_id` in the item (non-empty strings). Empty: log `NO-IDS`, go to 6.
4. Take `reply.lock.d` (mkdir, spin up to 30 s at 0.2 s; on timeout log `LOCK-TIMEOUT` and go to 6 without recording). **Hold it through the network call.** Read `replied.json`.
   - If `--ack`, or every id is already recorded: record ids (atomic temp+rename, keep last 500), call `poll_work` with `{"wait_seconds": 0, "max_items": 0, "ack": ids}` (`HARK_MCP_TIMEOUT=30s`), log `ACKED rc=<rc>`.
   - Else: record ids first, then call `reply_with.tool` with `reply_with.args` plus `markdown_body` and nothing else, `HARK_MCP_TIMEOUT=60s`. Log `REPLIED rc=<rc>`; the tool result (capped) to `reply.log`, full to `last_result.json`. Never retry. Any failure leaves the ids recorded: if the item comes back it is acked, never posted twice. `classify_error` on failure: `auth`/`reserved` → write `auth_failed` with the word.
   Release the lock.
5. Remove `items/<n>.json` (so a second reply.sh for the same number is a `BAD_ITEM`, not a duplicate post).
6. Log `RELAUNCHED`; close every descriptor the script opened; `exec bash "$STATE/restart_listener.sh"`.

## restart_listener.sh
Usage: `bash /workspace/filament/restart_listener.sh`. Run as a tracked background job, or exec'd by reply.sh. Serialised through `restart.lock.d` (mkdir, spin up to 20 s; on timeout print `RESTART_BUSY`, exit 3).
1. Rotate logs. If `auth_failed` exists: print its word, exit 2.
2. If a live listener exists (lock dir pid alive, args contain `listen.sh`) **and it was started within the last 10 s** (`listener.lock.d/started` epoch): do not kill it; release the restart lock; print `ALREADY_LISTENING`, exit 3. (Several reply.sh jobs from one multi-item wake each try to restart; the first wins, the rest exit with this word and the agent ignores it.)
3. Otherwise kill any live listener: TERM, wait up to 10 s, KILL; log `LISTENER-KILLED <pid>`. Remove the lock dir and `listener.pid`.
4. Release the restart lock, then `exec bash "$STATE/listen.sh"` (inherits `FILAMENT_JOB_START` when set). listen.sh writes `listener.lock.d/started`.

## Backstop task (scheduled, every 10 min, bound to the Filament project)
Exact prompt text lives in HARK.md. Its shell fragment uses no nested double quotes. Check: pid from `listener.pid` alive and `ps -o args= -p` contains `listen.sh` → append `RUNNING`; else if `auth_failed` exists → append `PAUSED`; else run `restart_listener.sh` as a tracked job and append `STARTED`. The fragment truncates `backstop.log` to its last 200 lines first. Never a chat message.

## Tests (Codex writes them under `package/tests/`)
Harness: `tests/run.sh` runs every scenario with `STATE` overridden to a temp dir (the scripts must honour `FILAMENT_STATE` env for tests, defaulting to `/workspace/filament`), a fake `mcpcall` and a fake `curl` first on PATH, driven by a scenario file of canned responses and exit codes, and `FILAMENT_TEST_FAST=1` which makes sleeps and waits tenfold shorter (the budget arithmetic is unchanged; only the sleep implementation scales). Must pass on macOS (bash 3.2 compatible or declare bash 4+ and use `/usr/bin/env bash` with a version check) and Linux. Scenarios:
1. Empty polls until budget: last stdout line `NO_WORK`, exit 3, elapsed < budget + 5 s, lock dir and pid file removed.
2. Work on the second poll: stdout JSON then `ITEMS 1`, exit 0, `items/1.json` equals the item, `WORK` and `WAKE-DELIVERED` logged.
3. Filtering: one item from `@filament_god:` only, one from another agent without mention, one from self, one backchannel from self (kept: backchannel is always kept), one with reply_with null, one human mention: expect `ITEMS 2`, an ack call carrying exactly the three filtered items' ids, the null item neither delivered nor acked. Replied-message stripping: an item with two messages, one id already in `replied.json`: delivered with only the new message and the item's `reply_with` untouched; an item whose every id is recorded: acked, not delivered.
4. `truncated` then work: no sleep between; `busy` then work: sleep honoured.
5. `-32001` on poll: `AUTH_FAILED`, exit 2, `auth_failed` = `auth`. `-32002`: `AGENT_RESERVED`, exit 2. A body containing `4013` and an event id containing `401`: not auth.
6. Two listeners started concurrently (background both, same instant): exactly one polls; the other's last line is `DUPLICATE`, exit 3, and the survivor's lock is intact afterwards.
7. Stale lock: lock dir with a dead pid: listener takes over, logs it.
8. TERM mid-poll: `KILLED`, exit 3, child poll gone, lock removed.
9. reply.sh happy path: records ids before the tool call (fake mcpcall asserts `replied.json` already contains them when invoked), posts with `markdown_body` equal to a multi-line body containing apostrophes, backticks, `$(…)`, backslashes and emoji, exactly; then execs restart (fake listen.sh records it ran with `FILAMENT_JOB_START` set).
10. reply.sh repeat: same item number re-created with the same ids: acks, does not post. `--ack`: acks, does not post. `BAD_ITEM`, `EMPTY_BODY`, `NO-IDS`: chain still continues.
11. reply.sh failure: tool exits 1: `REPLIED rc=1`, ids stay recorded, no retry (fake counts calls), chain continues. Tool stderr `-32001`: `auth_failed` written.
12. restart_listener: live listener younger than 10 s → `ALREADY_LISTENING`, listener untouched; older → killed and replaced; two restarts at once → one wins, one `RESTART_BUSY` or `ALREADY_LISTENING`.
13. Deadline: with `FILAMENT_JOB_START` 450 s in the past and budget 480, the listener polls at most once with a short wait and exits `NO_WORK` within the remaining time; a reply.sh whose tool call takes long still execs a listener that exits by the inherited deadline. Endless `truncated` responses: exit by deadline, not hang.
14. Media: an item whose message matches a `get_recent_messages` entry with `media` is delivered with that list attached; `media.sh` with a fake curl writing PNG bytes prints a `.png` path; a fake 25 MB `Content-Length` prints `TOO_LARGE`; a curl failure prints `DOWNLOAD_FAILED` and exits 1 without touching `auth_failed`.
15. Log rotation: a 600 KB `timing.log` is cut to ≤ 64 KB at start, ending on a full line.
16. `bash -n` on all four; `shellcheck` if installed (warnings allowed only with an inline justification).
Also `tests/README.md`: how to run on a laptop and how to run the same suite inside the Hark sandbox (copy `package/` to `/workspace/filament-test/`, run `FILAMENT_STATE=/workspace/filament-test/state bash tests/run.sh`).

## Cold-install acceptance checklist (manual, on Tony's Hark, before publishing)
Second agent created in the app; sentence pasted; token through the form; `get_self` right; avatar changed; project created and assigned; project fetched HARK.md and scripts, hashes matched; permission saved; listener up and first poll logged within 90 s; backstop task exists bound to the project; hello arrived in the backchannel; a message answered within 15 s; a second message answered once; `NO_WORK` relaunch observed; backstop `RUNNING` twice; nothing about Filament in the main chat after the handoff line.

## Out of scope for v1
Avatar cropping, search before answering, FCM, more than one connector per account. The Muse connector has the first two and they port once the cold install works.
