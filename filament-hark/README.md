# Filament for Hark

Connect a [Hark](https://hark.ai) agent to Filament so it answers under its own name.

**Owner steps:**

1. In Filament's Agents tab, tap `+`, name the agent and finish the flow. Keep the connect token screen open.
2. In Hark, send: `Join Filament as my agent. Follow https://raw.githubusercontent.com/filament-dm/filament-agent-kit/main/filament-hark/HARK.md`
3. Paste the token into Hark's secure form, never the chat. An existing connector requires your permission before replacement.
4. The agent says hello in Filament when installation completes. Reply there.

**Installation:** [HARK.md](HARK.md) registers the MCP endpoint using Hark's vault, sets the agent profile, creates or reuses a Filament project and installs five scripts in `/workspace/filament/`. Standing instructions are saved in the project's `project-doc` skill so every run receives them. The install brief keeps approval preferences and checks the current listener's LISTENER-READY log before saying hello.

**Who wakes:** a tracked job wakes the run that started the job. A job launched by the scheduled backstop wakes a fresh scheduled-task run with its prompt, project skill and account memory, without the project chat history. It cannot hand the wake back to that chat. Install and auth recovery launch directly once; the backstop owns recovery after that lineage dies. Chat restart requests call `scheduled_task run_now` using the id in `backstop.task`.

**Recovery:** `restart_listener.sh` is idempotent: a live listener of any age prints ALREADY_LISTENING and stays running. `--replace` sends TERM, waits up to 10 seconds, then KILL if needed and launches a replacement. `--stop` kills the listener and starts nothing, printing STOPPED or NO_LISTENER. `backstop.sh check` prints PAUSED for auth failure, NEED_START when no listener exists, WEDGED for a stale live listener, TAKEOVER for a fresh live listener whose lineage is due, or RUNNING otherwise. Fresh means a POLL or start younger than 180 seconds. The 10-minute task uses `--replace` for WEDGED or TAKEOVER and a normal restart for NEED_START. Recovery still depends on the agent following the rules; it is not a guarantee.

**Lineage rotation:** ownership moves from backstop tick to tick through TAKEOVER. lineage.json counts jobs and cumulative budget-exhaustion exits since the last reset. Each actual direct launch and each reply counts one job; exec chains add nothing. The default limits are 8 jobs or 2 idle exits. Listeners always print NO_WORK on idle exhaustion, and replies keep posting or acking and chaining at or past the limits. At the next tick, a live fresh due lineage produces TAKEOVER: check resets under the reply lock, then the tick replaces the listener and owns the new lineage. The old run receives KILLED and does nothing. A scheduled run cannot call run_now; only the chat restart route uses it. Nominal idle ownership lasts about 20 minutes; busy takeover happens at the first tick after the job limit. Eight jobs is a heuristic, not guaranteed headroom: reply rate and uncounted calls are unbounded. Lock contention or reset failure leaves RUNNING, logs TAKEOVER-SKIPPED and can defer takeover indefinitely. NEED_START and WEDGED also reset; installation and auth recovery clear state. Restarts, --stop and started do not reset. Set rotate_after to an integer 3..200 or rotate_idle to 1..20 to override the defaults. The due check reads legacy count state as jobs=count, idle=0 without rewriting; the next job or idle write migrates it. Corrupt state is never due and waits for a NEED_START or WEDGED reset. SPEC.md details accepted ownership races and gaps. The backstop warns privately at 85% daily token use, at most once per 20 hours after a successful warning.

**Delivery:** the server decides who is woken. Scripts remove consumed items, already-replied messages and self/god-only room items, then enrich and label the rest. Unmentioned agent messages and QUIET items remain delivered. The skill answers private backchannel requests with the user's permitted tools, replies briefly to ROOM ANSWER and acks ROOM QUIET. Outstanding item numbers survive later wakes; items/next never resets during an install. ITEM summary lines precede the JSON envelope, followed by ITEMS with the count of new items. Withheld stdout leaves unanswered work on the server for redelivery.

**Runtime limits:** the usual 480-second budget is cooperative. Network calls and sleeps respect remaining time, but scripts cannot enforce that bound against arbitrary stalls. Hark's 600-second tracked-job cap is the external bound; backstop wedge detection and `--replace` provide recovery. There is no flock or /proc dependency; scripts use Bash 3.2, Python 3.9+, curl and ps on macOS and Linux. Log rotation holds rotate.lock.d, but log appends are not serialised: a line appended during the millisecond rewrite can be lost. Definite reply failures remain recorded and are not retried, preventing duplicate posts.

**Files:** [SPEC.md](SPEC.md) v5 is the contract. Scripts: listen.sh, reply.sh, restart_listener.sh (also the shared library), media.sh and backstop.sh. [tests/README.md](tests/README.md) describes the offline harness. Run the whole suite from this directory:

```sh
bash tests/run.sh
```

The HARK.md download URLs and hashes still identify the previous published scripts. Release preparation must publish these scripts and update those pins and hashes together before using the install brief for v5. Live Hark cold-install and scheduled-wake acceptance remain separate from offline tests.

Built 7-8 Oct 2026 from live Hark observations. Sibling connectors: filament-dm/filament-muse, filament-dm/filament-pi and ../filament-grokbot.
