# Connector tests

From this package directory on macOS or Linux:

```bash
/usr/bin/env bash tests/run.sh
```

On the Instinct computer, use the same files and command:

```bash
FM_STATE=/tmp/fm-test bash tests/run.sh
```

Each test overrides `FM_STATE` with its own private temporary directory, copies the scripts there and removes it afterwards. An existing installation is never used. Only Python 3.9+ standard library and Bash are required. `shellcheck` runs if already installed; there are no installs or external requests. Expect roughly 2-3 minutes, including the real 20-second handoff and 60-90-second unscaled check. Individual tests can be selected, for example `bash tests/run.sh Connector.test_10_reply_verbatim_idempotent_ack_and_concurrent`.

`fake_server.py` normally runs `ThreadingHTTPServer` bound only to `127.0.0.1`. Each request has its own thread. The fixture supports queued canned delays, HTTP statuses, JSON-RPC/tool errors, malformed JSON, SSE and disconnects before/after application. Its queue tracks stream order, saved position, explicit/cursor acknowledgements, duplicate replies and posted message history. `calls.jsonl` records tool arguments/timestamps and a credential hash, never an Authorization header. Production Filament is never contacted.

Some managed macOS sandboxes prohibit even loopback `bind()`. On that specific `PermissionError`, the harness runs the same `BaseHTTPRequestHandler` over HTTP bytes carried through temporary fixture files. A test-only `sitecustomize` urllib handler connects child processes to that fixture; production code is unchanged. All processes, files, locks, signals and timing remain real. This fallback validates HTTP envelopes and connector behaviour, but does **not** validate operating-system TCP/socket timeout behaviour. The build report names which transport actually ran. No permission escalation or external connection is attempted. The transport directory is fixture infrastructure, separate from the connector's STATE.

## Coverage map

Test method numbers 01-18 correspond to the original spec scenarios, with the following overrides: handoff uses a nonce, batch ownership uses `handling`, and a replacement keeper terminates its predecessor. Scenario 11 has three methods for landed/not-found/lookup-failed outcomes, so there are 32 methods in total, more than the scenario numbers.

| Scenarios | Assertions |
| --- | --- |
| 01-05 | Real 20-second budget, handoff gap under two seconds, successor ownership, second-poll delivery, full raw response, context ordering/caps, queue unread state, classification, explicit acks, retargeting, truncation and capped sleep |
| 06-09 | Auth/reserved routing, whole status codes, backoff doubling/reset, competing listeners with no overlapping polls, stale/dead leases, invalid and valid handoff tokens, stale-handler replay with server unread checks |
| 10-12 | Body bytes, concurrent reply serialisation, idempotence, ack, queue-backed landed/missing/uncertain replies, thread lookup channel, duplicate refusal, failure notice and eventual server acknowledgement |
| 13-15 | Checked commit, status without requests, token permissions, exactly one claim, keeper cadence/replacement/handover |
| 16-18 | Media MIME/size/errors/expiry, rotation and syntax, optional shellcheck, deleting STATE during a live poll, bootstrap-only verified install and hash failure leaving files unchanged, queue-backed reply after rebuild |
| 19-23 | Unscaled 60-second budget, concurrent ack append, persistent unnotified failures, reserved probes, in-flight old-token rejection, startup/enrichment/lookup/notice auth routing |
| 24-28 | SSE, context failure, newest-50 thread cap, success without event ID, duplicate refusal on second attempt, no third post, unclaimed work guard, stale/bad/empty reply, force commit, bad budget, expired handoff, unpublished pending recovery, empty release pin and 2000-ID cap |
| 29-30 | Auth classification rejects ID/number substrings, request-token redaction, five-file release manifests and existing-install preservation on hash failure |

The complete suite also cleans up spawned listeners/keepers. Tests do not alter assertions to accommodate failures. Python compilation puts its cache in the test state directory to avoid macOS's user cache outside the sandbox.

The manual second-owner Instinct checklist in SPEC.md is not automated here. Linux/TCP execution and real Filament acceptance must be run separately before release.
