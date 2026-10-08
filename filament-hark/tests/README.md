# Filament connector tests

From `wt/filament-hark/` on a laptop:

```sh
bash tests/run.sh
```

The five production scripts use Bash 3.2-compatible syntax and
`#!/usr/bin/env bash`; Linux Bash 5 is supported. Python 3.9+, curl, ps and the
usual POSIX command-line utilities are required. The harness needs Python 3.9+
and Bash, with no third-party Python packages. It substitutes both network
commands, so no credentials, installation or real network access are needed.
ShellCheck is run when available on PATH and otherwise explicitly reported as
skipped. Every production script is checked individually with `bash -n`.

To select a particular Bash:

```sh
FILAMENT_TEST_BASH=/bin/bash bash tests/run.sh
```

Copy the complete `filament-hark/` directory to `/workspace/filament-test/` in Hark,
then run the same suite there:

```sh
cd /workspace/filament-test
FILAMENT_STATE=/workspace/filament-test/state bash tests/run.sh
```

Allow roughly two minutes, or run it as a tracked background job. The harness
creates a private temporary directory for each test, under `FILAMENT_STATE`
when supplied, and removes it afterwards. It copies the scripts into each
directory: production scripts always resolve siblings through that state
directory. It does not modify a live connector. Each scenario writes a
`scenario.json` of canned responses into its private directory. The original test numbering is retained; tests 25-26 cover media and tests 27-38 cover tags, stable numbers, lineage rotation, lock reclaim and the backstop.
Run a single group with, for example:

```sh
bash tests/run.sh Scripts.test_13_deadlines_and_endless_truncated
```

## What is real and what is fake

`fakes/mcpcall` and `fakes/curl` call `fake_network.py`. Each scenario maps a
tool name to a list of responses; its last response repeats. `ack` is the
`poll_work` route with `max_items: 0`. Responses support `out` (JSON), `raw`
(malformed text), `rc`, `err`, `delay` and `assert_recorded`. The last field
asserts IDs are already persisted and the reply lock is still held when the
request begins. A delay of `poll_wait` uses the requested poll wait. Calls,
arguments, PIDs, timestamps and timeouts are recorded in `calls.jsonl` under
an atomic directory lock.

Processes, signals, locks, disk writes, stdin, exec PIDs and epoch time are real.
`FILAMENT_TEST_FAST=1` scales script sleeps, including lock/restart waits, by
ten; it does not change the budget arithmetic or network delays. The empty
poll test uses a real 60-second budget (the listener stops polling when less
than 20 seconds remain). Inherited-start tests exercise a short real remaining
budget, including a slow reply followed by the real restart and listener.
Some reply tests substitute a tiny listener solely to observe the exec chain.

The harness probes native `ps`. If a restricted execution sandbox blocks it,
the harness explicitly reports `native ps unverified` and installs a
test-only process catalogue in that scenario's PATH. `process_tools.py`
records actual test-owned PIDs, parent PIDs and command lines at process launch
and Bash exec, and answers only the two ps queries the scripts use. It never
enumerates unrelated host processes. Ordinary laptop and Hark runs use native
ps automatically. A pass with this fallback verifies lifecycle logic against
the catalogue; it does **not** verify the host's real ps output or permissions.
Production scripts always use ps; no production fallback is installed.

## Contract choices and limits

- Success stdout is ITEM summary lines, one compact JSON document, then
  `ITEMS <count>`. Count is newly delivered items, not the highest number.
- The delivered full envelope retains all top-level poll fields, with `work`
  replaced by the filtered items, both on stdout and in `items/wake.json`.
  Otherwise stripped old messages would still be shown to the agent. Delivery
  JSON is not truncated to 2,000 characters; that limit applies to log lines.
- A single shared `classify_error` and the other shared helpers live in
  `restart_listener.sh`, which is safe to source. Install all five scripts in
  STATE together.
- A short-lived `restart.pending` handover file closes the gap between
  releasing the restart lock and exec'ing the listener. It carries the PID
  preserved across exec; live process identity is checked before waiting.
  Per-run `.listen.*` and `.reply.*` directories isolate request files and are
  cleaned on normal exit and handled signals. No separate cursor is saved or
  ever sent, although the required full wake envelope can contain its cursor.
- Markdown is UTF-8. Exactly one terminal LF byte is stripped, preserving
  additional blank lines, CRLF bytes, backslashes and all other content. A
  malformed reply target or non-UTF-8 body is `BAD_ITEM`. Corrupt replied state
  fails closed, with a diagnostic and no request, rather than risking a second
  post. Reply and restart lock timeouts follow the spec. Stale-owner locks are reclaimed by rename; live shell owners and missing-pid directories younger than five seconds are protected.
- Startup invitation and vouch results accept a bare list, `invites` (also
  `pending_invites`) or `vouches` envelopes. The spec does not supply their
  exact response schemas; live Hark acceptance must confirm these shapes.
- Without any usable self cache, self-dependent filter rules are skipped;
  the god-sender rule and replied-message stripping still apply. Heartbeat
  and listener filtering-ack errors are logged and ignored as their explicit
  steps require. Other MCP failures use the one recogniser, including reply
  acknowledgements. A paused restart is still the required exec continuation
  after a reply authentication failure.

No live Hark cold install, real Filament API call or publication is performed
by this suite. The manual cold-install checklist in SPEC.md remains the release
gate. The scripts deliberately retain the specified record-before-send policy:
even a definite send failure is recorded and is not retried.

In a multi-item wake, later deliveries retain old outstanding numbered files.
An old number still addresses its original reply target. Redelivered work gets
a new number; after one posts, the other acks through the replied-ids guard.
Overlapping message sets can both post if either contains new content. Pruning
removes fully-replied or 24-hour-old numbered files without acking; items/next
survives. Rotation leaves its item file outstanding for the next lineage.

Backstop tests exercise PAUSED/RUNNING/WEDGED/NEED_START, the 200-line truncation
before appending (201 afterwards), started, quota-body escaping and the 20-hour
throttle. Lock fixtures use live .sh owners, as production identity checking
requires. The media rotation test holds rotate.lock.d while downloading and
verifies rotation is skipped, then succeeds after the owner dies. All network
requests remain fake, including quota warnings. Hark's real job cap, withheld
stdout and repeated-call limit need live acceptance; they are not simulated
as guarantees by this suite.
