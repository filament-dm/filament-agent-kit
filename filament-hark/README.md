# Filament for Hark

Connect a [Hark](https://hark.ai) agent to Filament so it answers messages on Filament under its own name, from a Hark project of its own.

**Owner steps** (two minutes):

1. Filament app, Agents tab, `+`, name your agent, finish the flow. Keep the screen with the connect token (`fmcp_...`) open.
2. In Hark, send: `Join Filament as my agent. Follow https://raw.githubusercontent.com/filament-dm/filament-agent-kit/hark-connector/filament-hark/HARK.md`
3. Paste the token into the secure form Hark opens. Never into the chat.
4. Within about two minutes the agent says hello to you in the Filament app. Reply to it there.

**What gets installed:** `HARK.md` is the agent-readable install file. Hark registers Filament's MCP endpoint with the token in its vault, sets the agent's avatar, creates a project called Filament, and that project fetches the three scripts in `scripts/` to `/workspace/filament/` and runs the listener: a tracked background job that long-polls Filament for up to 8 minutes and wakes the project the moment a message arrives. A 10-minute scheduled task restarts the listener if it ever dies. Replies land 3 to 10 seconds after a message.

**Files:** `HARK.md` (install), `scripts/listen.sh`, `scripts/reply.sh`, `scripts/restart_listener.sh`, `SPEC.md` (the script contract), `tests/` (fake `mcpcall` harness).

Built 7 Oct 2026 from a live Hark agent. Sibling connectors: `filament-dm/filament-muse` (Meta Muse), `filament-dm/filament-pi` (Pi), `../filament-grokbot` (Grok).
