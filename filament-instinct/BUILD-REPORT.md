# Build report

Built the Instinct connector package on `fable/instinct-connector`, using SPEC.md with D1-D21 taking precedence. No release, external service call or live Instinct operation was performed.

## What I built

- `scripts/fm_lib.py`, `fm_poll.py`, `fm_reply.py`, `fm_hb.py`, `fm_media.py` and `poll.sh`: shared transport/state helpers, exclusive listener leases with nonce handoff, atomic batch publication and claims, checked commit, serialised bounded reply recovery, durable failure notices, presence, media and verified bootstrap installation.
- `tests/run.sh`, `suite.py`, `fake_server.py` and `README.md`: real-process unittest coverage of all 18 original scenarios as amended by D20/D21, plus auth, concurrency, recovery and release-pin regressions. Queue scenarios check server unread state as well as local files.
- Package `README.md`: file/state reference, command table, exit words, configuration, bootstrap/release procedure and documented D7/D8/D13 limits.
- This report. No implementation file outside this package was changed. An independently modified parent `../README.md` and a concurrently present `INSTINCT.md` were left alone.

The nine files listed in `harvest-reference/HASHES.txt` were checked with SHA-256: **9/9 unchanged**. Nothing under `harvest-reference/` was edited. The harvested module and state names are retained except for the directory leases and additional claim/delivery state mandated by D1-D4. No old `seen_events` migration or timestamp-only lock is used, per D16.

## Verification

Final command: `/usr/bin/env bash tests/run.sh` (complete package suite, exit 0).

```text
Ran 32 tests in 130.893s

OK
```

Duration: **130.893 seconds**. **32 passed, zero failures/errors, zero skips.**

Environment: macOS, `/usr/bin/env bash` resolving to Bash 3.2.57, Python 3.9.6. The suite invokes `bash -n`, `python3 -m py_compile` for every installed Python script and the already-installed `shellcheck`. Both shell scripts were also checked directly with `bash -n` and `shellcheck`.

The sandbox denies loopback socket binding with `PermissionError: [Errno 1] Operation not permitted`. All functional runs here therefore used the documented offline HTTP-byte transport through the same `BaseHTTPRequestHandler`, with real child processes, filesystem state, locks, signals, server threads and elapsed time. No external network, installations or elevated permissions were used. Native TCP behaviour, Linux and live Filament were not verified here.

The previous complete pass was `Ran 30 tests in 130.129s`, followed by `OK`. The final pass includes the two additional release/redaction regressions and the final changes. The test assertions were not relaxed or removed.

Failures encountered and resolved:

- Initial run: all 30 then-existing tests errored in `setUp` because loopback `bind()` is denied; connector code was never reached. Added the isolated offline test transport, retaining the normal TCP fixture for unrestricted machines.
- `test_18_rebuild_live_listener_and_transactional_install_queue`: reproduced a listener returning from I/O into rebuilt state with no owned lease. The listener now verifies ownership before applying a poll response and stops cleanly on loss.
- `test_17_rotation_syntax_and_optional_shellcheck`: `py_compile` tried to write to macOS's user cache outside the sandbox. Its test environment now sets `PYTHONPYCACHEPREFIX` inside that test's state directory. No assertion change.
- `test_22_reserved_probe_rotation_and_inflight_old_token`: reproduced the previous stopped listener's still-running server request consuming the next scenario's delivery before its fresh-cursor poll. The test now waits for that previous server request to finish before beginning the independent token-rotation scenario. Its assertions are unchanged.

Constraints checked: Python 3.9 standard library only; Bash 3.2 syntax and env shebang; state-local runtime files and sibling discovery; per-request credential reads; no credential argv/log/output; at most one reply retry after a successful negative lookup; no poll cursor advancement while work is held; no harvested-file edits; D1-D21 precedence; no external requests or installs. Cursor changes during checked commit are the explicit D5 exception to the pending-batch write prohibition. There was no separate block titled “Global Constraints” in the supplied SPEC.md.

## Rulings I made

Each line gives the choice, why and the cost if wrong.

- `--token` reads stdin and rejects a positional value: the explicit no-secrets-in-command-lines rule wins over the earlier syntax; callers following that old syntax must change their invocation.
- The Bash wrapper embeds Python for bootstrap and state operations: Bash 3.2 has no portable atomic JSON/filesystem primitives; Python must be available even to bootstrap, as the spec already requires.
- The release mapping requires the five Python files, with an optional separately pinned successor `poll.sh`: a bootstrap cannot embed its own SHA-256; release engineering must pin the bootstrap externally in persistent text.
- An empty or incomplete release pin fails closed: reporting `INSTALLED` without restorable scripts would be misleading; this unreleased build cannot fetch itself until the pin is filled.
- Downloads have a 5 MiB per-script cap: bound bootstrap memory use; a future unusually large script needs that cap reviewed.
- Directory leases use a short shared `flock` guard around takeover, state transactions and ledger updates: `mkdir` alone cannot safely serialise competing stale removers/read-modify-write updates; this assumes POSIX local filesystem locking, available on the specified targets.
- Keeper replacement has its own short coordination guard: the old keeper must be able to clean up its PID while its replacement waits; one extra private guard file is created.
- A keeper ignores the delivering listener's PID during enrichment: D10 starts it while that listener still owns a fresh lock; a wrongly supplied internal predecessor PID could delay presence handover.
- Published but unclaimed work blocks new polls without expiring: D4 defines handler expiry but leaves unclaimed publication open; an unattended batch waits indefinitely instead of risking acknowledgement/loss.
- A pending cursor without a complete matching publication blocks for 600 seconds, then is abandoned without advancing the committed cursor: publication/enrichment can die between files; recovery may be delayed ten minutes, and the first-install D8 window remains.
- Expired handling invalidates its delivery markers before redelivery, and commit removes the completed `run.out`: otherwise a tick could claim stale text again; historical text remains in JSON history instead.
- Claim and commit verify the batch UUID under the transaction guard; reply refuses an already-expired handler instead of reviving it: safer against stale answers; a handler idle for ten minutes must claim redelivery before replying.
- IDs removed from an otherwise delivered item are queued for acknowledgement too: filters must not leave self/replied/system IDs cycling; these IDs may appear in the explicit ack list earlier than a cursor-only interpretation.
- “Newest” means greatest message timestamp, breaking ties by source order: the spec does not define ordering; a server with incorrect timestamps could select a different reply target.
- Missing context/thread values are empty arrays, and independent context failures accumulate in `context_error`: D17 names those fields but does not define absent values; consumers should treat empty context plus an error as unavailable.
- Malformed poll data or a missing cursor uses backoff instead of delivering an uncommittable batch: acknowledgement safety wins; a server returning an otherwise usable batch without a cursor requires correction.
- Failure records are JSONL with a `notified` value of `yes` or `no`: notices/errors can contain line breaks and must remain recoverable; readers expecting an undocumented free-form line layout must parse JSON instead.
- Trailing LF characters are stripped, while internal LF/CRLF and UTF-8 bytes are preserved: follows the harvested reply helper's trailing-newline stripping; an intentional trailing LF is removed as before.
- The D21 floor of one applies to the integer `wait_seconds`, while sleeps are divided by ten without rounding: this preserves the stated tenfold sleep acceleration and meaningful subsecond waits; an interpretation imposing a one-second floor on every sleep would lengthen tests.
- Test-mode budgets accept 20-600 seconds, production 60-600: D21 explicitly needs a real 20-second handoff; test mode remains unsuitable for deployment.
- TERM and lease loss end a listener with `run complete: NO_WORK`: the reason list has no stop/rebuild word; diagnostics alone do not distinguish those two clean exits.
- Installation verifies every download before replacement and rolls back ordinary replacement errors: D18 prescribes per-file renames rather than a directory swap; a machine crash between renames still requires rerunning the pinned install.
- Retired harvested test switches and timestamp-lock migration are not carried forward: D16 and the specified CLI replace them; private callers relying on `FM_CHAIN`, `FM_TEST`, `FM_TEST_NET`, `FM_NOHB` or old `fm_poll.py --commit` must use the documented interface.
- On loopback `bind()` denial only, the tests use file-carried HTTP bytes through the same handler: fulfil no-network/sandbox constraints while testing real processes; native socket behaviour remains a release-stage check.

## Remaining stages

No requested package script or automated spec scenario is intentionally omitted. Release/runtime stages still needed:

- Fill immutable release URLs and hashes in the bootstrap and supply its external pin in the persistent tick instruction. The empty mapping is deliberate for this build.
- Run the existing full suite with real loopback TCP on an unrestricted macOS/Linux environment, then run the manual second-owner cold-install acceptance checklist from SPEC.md. This environment cannot validate real Filament delivery latency, application creation, picture understanding or chat silence.
- Configure Instinct's watcher, scheduled wake and persistent rebuild instructions; implement OAuth, avatar cropping or search only if separately commissioned. These are explicitly out of scope in SPEC.md.
- Stop any live harvested listener with its existing owner before migration. D16 forbids reading its old timestamp lock, so the new directory-lock `--stop` cannot discover that legacy process.

Decision for the reviewer: review the recorded rulings, then fill the release pins and perform cold-install acceptance before publishing. No approval is needed to inspect or run the local package.
