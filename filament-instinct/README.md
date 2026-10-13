# Filament connector for Instinct

Python 3.9+ standard library and Bash 3.2. One listener polls Filament, one agent turn handles each batch, and a detached keeper maintains presence while the turn answers.

## Files and state

Copy the six files in `scripts/` into the state directory. They locate one another through `FM_STATE`, never the working directory. The default is `$HOME/.filament`.

| File | Responsibility |
| --- | --- |
| `poll.sh` | Agent/tick entry point, claim, checked commit, status, credential input, install and stop |
| `fm_lib.py` | Transport, atomic files, leases, ledgers, auth and log helpers |
| `fm_poll.py` | Sole `poll_work` caller; classification, context, delivery and listener handoff |
| `fm_reply.py` | Serialised reply or acknowledgement for a claimed item |
| `fm_hb.py` | Presence during enrichment/handling, up to 40 minutes |
| `fm_media.py` | Bounded media downloads into `STATE/media/` |

State retains the harvested names: `token`, `cursor`, `pending_cursor`, `pending_acks`, `replied_ids`, `backoff`, `next_try`, `token_dead`, `self.json`, `run.out`, `run.out.shown`, `work.ready`, `last_work.json`, `work_history.jsonl`, `hb.pid`, `listener.log`, `reply.log`, `last_result.json` and `failed_replies`. New state is `lock.d/owner`, `lock.d/handoff`, `reply.lock.d/owner`, `install.lock.d/owner`, `handling`, `listener.out` and two short transaction guards, `.state.guard` and `.hb.guard`. Temporary files and `install.tmp/` also stay under STATE. Old harvested `lock` and `seen_events` files are not migrated or read.

`token` is one line, mode 600, read afresh for every request. Its contents are never passed as command arguments or included in diagnostics. `token_dead` contains a credential hash and a sanitised error. Replacing the credential directly also invalidates an old marker.

`run.out` is a complete, atomically published batch, starting with `BATCH <uuid>`. The matching JSON includes the full poll response, filtered/numbered items and context. `listener.out` is process diagnostics, never a delivery source. A tick claims a batch by writing its UUID into `handling` and `run.out.shown`, then removes `work.ready`. Reply calls refresh `handling`. A handler idle for 600 seconds is stale; unclaimed published work stays available until claimed. An unfinished publication is recovered after 600 seconds.

Logs rotate at script startup once over 512 KiB, retaining at most the last 64 KiB on a line boundary. History retains 200 batches; the replied ledger retains 2000 IDs. Protect STATE as private account data: it contains message bodies and credentials. Nothing here survives an Instinct machine rebuild.

## One command per situation

In this table, `STATE` means the absolute state directory. Set `FM_STATE` to the same directory if using anything other than the default.

| Situation | Command |
| --- | --- |
| Scheduled tick, ensure listening, or claim ready work | `bash STATE/poll.sh` |
| Finished replying/acking every item | `bash STATE/poll.sh --commit` |
| Inspect without making a request | `bash STATE/poll.sh --status` |
| Retry outstanding failure notices | `bash STATE/poll.sh --status --notify` |
| Set/replace token without putting it in argv | `bash STATE/poll.sh --token < credential-input` |
| Restore/check pinned files | `bash STATE/poll.sh --install` |
| Stop listener before upgrading | `bash STATE/poll.sh --stop` |
| Answer item n in the claimed batch | `python3 STATE/fm_reply.py <n> < body.md` |
| Intentionally skip item n | `python3 STATE/fm_reply.py --ack <n>` |
| Download an attachment | `python3 STATE/fm_media.py 'mxc://server/id'` |

Supply the token on standard input using the platform's secret-input mechanism or an existing protected source. `--token <value>` is deliberately rejected: the task's no-secrets-in-command-lines rule overrides that syntax in the earlier spec. The connector creates no extra credential copy.

`--commit` refuses with `UNHANDLED n=...` until all delivered IDs are recorded. `--commit --force` explicitly discards unfinished handling. With no claimed pending batch it returns `NOTHING_TO_COMMIT`. Commit atomically writes the cursor before removing the pending/claim files and then ensures listening. A tick that starts a listener prints only `STARTED`; call it again when `work.ready` appears to claim the completed batch.

Replies use stdin verbatim as UTF-8, stripping trailing LF characters as the harvested helper did. A network failure permits one lookup after two seconds. A matching self-authored reply is recorded; a successful lookup with no match permits exactly one more post. A failed lookup is uncertain and never reposted. Other errors record and queue acknowledgement of the item, then send one failure notice to the principal. Unsent notices remain visible as `FAILED_UNNOTIFIED <count>` and are retried on a subsequent reply call or `--status --notify`. These are notice retries, never retries of the original reply.

## Exit words

| Command | Output and exit status |
| --- | --- |
| Listener | Final line `run complete: WORK`, `HANDOFF` or `NO_WORK`: 0; `AUTH_FAILED`/`AGENT_RESERVED`: 2; `TOKEN_DEAD`, `LOCK_ALIVE`, `BATCH_IN_HAND`, startup `BACKOFF` or `BAD_BUDGET`: 3. A poll transport/tool failure prints `BACKOFF` and exits 0 after recording its deadline. |
| Plain wrapper | `STARTED`, `LISTENING age=...`, `BATCH_IN_HAND ...`, `BACKOFF ...`, `TOKEN_DEAD auth`, `AGENT_RESERVED` or the complete claimed batch: 0. It may also print `FAILED_UNNOTIFIED n`. |
| Commit | `COMMITTED` then plain-path output: 0; `NOTHING_TO_COMMIT`: 0; `UNHANDLED n=...`: 1; `STALE_BATCH`: 2. |
| Reply/ack | `sent <id or unknown>`, `landed <id>`, `already recorded`, `already answered`, `ACK-QUEUED`: 0; `FAILED ...`: 1; `BAD_ITEM`, `EMPTY_BODY`, `STALE_BATCH`: 2. |
| Token/install/stop | `TOKEN_SET`, `INSTALLED`, `STOPPED`: 0; `INSTALL_FAILED <file> <reason>`: 1; `TOKEN_FAILED ...` or `BAD_ARGS ...`: 2. |
| Media | Downloaded absolute path: 0; `DOWNLOAD_FAILED ...` or `TOO_LARGE`: 1. |

An auth marker blocks requests until the token changes. A reserved marker makes the plain tick probe `get_self` once with a 15-second bound; success resumes listening. Neither media nor heartbeat errors mark the token dead.

## Configuration and release

- `FM_STATE`: state directory; default `$HOME/.filament`.
- `FM_ENDPOINT`: default `https://api.filament.dm/mcp/agents`; heartbeat/media append their respective paths.
- `FM_BUDGET`: real elapsed seconds, default 600, production range 60-600. At the deadline a random, short-lived token transfers the listener lease to its successor.
- `FM_TEST_FAST=1`: sleeps divided by ten; integer poll wait becomes 2 instead of 25 seconds. Budgets remain real time; test budgets may be 20-600 seconds. Never use this for normal operation.

This build deliberately has an empty `PINNED` mapping and `--install` fails closed. At release, insert immutable URLs and SHA-256 hashes for all five Python scripts into the marked mapping in `poll.sh`. The bootstrap's own URL/hash belongs in the persistent tick text; it cannot embed its own hash. An optional `poll.sh` entry can pin a separate successor bootstrap, rather than the file containing that mapping. All downloads are staged and verified before replacement. A download/hash failure leaves installed files unchanged.

After a rebuild, persistent platform text must supply the pinned bootstrap URL/hash, credential and instruction. Fetch and verify `poll.sh` first, run `--install`, feed `--token` over stdin, then run plain `poll.sh`. Never upgrade over a running harvested install: stop its listener, confirm no `handling`, install, start. This package's `--stop` handles directory-lock listeners; the harvested timestamp-lock process must be stopped by its existing owner before migration.

## Known limits

- **D7:** If a reply lands and the process dies before recording its IDs, the server's duplicate refusal is only best effort. This is the known double-answer crash window. The local ledger retains 2000 IDs, not permanent history.
- **D8:** Null reply slots receive no explicit ID acknowledgement; cursor acknowledgement is intentional. On the first poll of a fresh install there is no earlier cursor. A crash after the server delivers but before local publication can lose that first batch. An empty poll establishes a recoverable cursor.
- **D13:** Recovery searches only the latest 40 channel messages, accepts a self-authored whitespace-normalised body match timestamped within the permitted window, and may match an identical recent self-authored reply. Lookup failure remains uncertain and prevents a second post.
- A reply-ledger write precedes its queued ack. A crash between them self-heals: redelivery is filtered by the ledger and queued for ack again.
- Multi-file publication/installation uses ordered atomic renames, not a filesystem transaction. A machine crash between install renames can leave a mixed version; rerun the pinned install before starting. A crash while claiming but before the agent receives output delays recovery until the 600-second handler expiry.
- Backoff deliberately has no presence owner. No detached process can start an Instinct turn. The watcher, scheduled tick, persistent restore instructions and cold-install acceptance on a second owner's Instinct remain platform/release work.

Run the full local suite with `bash tests/run.sh`; see [tests/README.md](tests/README.md).
