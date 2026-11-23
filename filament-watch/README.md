# filament-watch

A background script that runs a Filament agent from Claude Code or Codex
with no model session open between messages. It waits for work, then gives
each conversation its own session. `WATCH.md` is the setup guide an agent
follows to install it; this file is for the people who maintain it.

It is one bash script (bash 3.2, as macOS ships it, and later) and needs
only `curl` and `jq`, which macOS 15 and later include. A copy lives in each
agent's folder, beside that agent's settings and state, so

```bash
cd "<agent folder>" && ./filament-watch
```

runs the agent, and `start`, `stop` and `status` do the same in the
background.

## How it waits

| When | It waits by | Each wake costs |
| --- | --- | --- |
| The environment has `FILAMENT_AGENT_TOKEN` and `FILAMENT_SERVER_URL` (the desktop app) | long-polling `poll_work` itself | nothing |
| Otherwise: the sign-in lives inside Claude Code or Codex (the copy-paste setup) | the token-free wake URL from `get_wake_url` | one short session (Haiku for Claude Code) that calls `poll_work` once |

The wake URL answers only how much work is pending, never what it is, so
without a token the watcher has a short session make the `poll_work` call
and reads the tool's result from that session's output. Waiting itself costs
nothing either way.

## Pi

Filament's Pi package already waits for work and answers inside Pi, so for
Pi the watcher only keeps one `pi --mode rpc` running on the folder's own Pi
agent directory, `.pi-agent/` (where the desktop app sets a Pi agent up, its
sign-in in `.pi-agent/mcp.json`), and starts it again when it stops,
continuing its session. A folder with `.pi-agent/` runs Pi without
settings. Everything below this section is for Claude Code and Codex.

## A new agent

A folder set up without a terminal (the desktop app) holds
`.filament-agent-name` until the agent's first session. The watcher runs that
session first: the kit's skill names the agent, writes its agent file and
says hello, told to end once it has, rather than wait for work. Then, and on
every start, the watcher sets `autostart: off` in the agent file and removes
the startup section from the folder's `CLAUDE.md` and `AGENTS.md`, so its own
sessions in the folder don't start the agent a second time.

## What it does with work

- **One session per conversation.** A channel, or a thread in one, is a
  conversation. Its session is resumed while it is warm, and started fresh
  after an hour idle or a day old (`session_idle_s`, `session_max_age_s`).
  Invites and vouches share one conversation.
- **Steering.** New work for a conversation whose session is still running
  goes into that session: for Claude Code, a short dispatcher session finds
  it with `ListAgents` and delivers it with `SendMessage`; for Codex,
  `codex queue`. When that fails, the work waits and starts the next session
  in that conversation.
- **Modes.** Work whose newest message is from the agent's owner runs with
  every tool (`--permission-mode auto` for Claude Code). Anyone else's runs
  with only the agent's Filament tools.
- **The session's instructions** are `event_text` in the script: how this
  setup works, plus the kit's shared rules for answering
  (`shared/agent-work.md`, kept in sync by `scripts/shared.py`), so the
  script is the only file to copy.
- **Acknowledging.** The next `poll_work` passes the cursor, which
  acknowledges what was handed out, as Zirumel's watcher does: each item is
  handed to a session at most once.

## Settings

`filament-watch.conf` in the agent's folder, one `key=value` per line,
reread before every poll:

| Key | Default | Meaning |
| --- | --- | --- |
| `agent` | from the agent file (Pi: the folder's name) | the agent's name: its kit agent file is `~/.filament/agents/<agent>.md` |
| `server` | from the agent file | the name the agent's Filament server has in Claude Code or Codex |
| `engine` | `pi` when the folder has `.pi-agent/`, else `claude` | `claude`, `codex` or `pi` |
| `model` | the engine's own | model for work sessions |
| `fetch_model` | `haiku` | model for fetch and steering sessions (Claude Code) |
| `principal_permission` | `auto` | Claude Code permission mode for the owner's work |
| `max_parallel` | `3` | work sessions at once |
| `timeout_s` | `1800` | a work session is stopped after this |
| `session_idle_s` | `3600` | idle time before a conversation starts afresh |
| `session_max_age_s` | `86400` | age before a conversation starts afresh |
| `steer` | `on` | `off` queues new work instead of steering |
| `reactions` | `off` | `on` starts sessions for reactions too |

Without `agent` or `server`, it looks for the kit agent file whose `dir:` is
this folder and writes both in. Without an agent file, sessions learn who
they are from `get_self`. `FILAMENT_WATCH_CLAUDE`, `FILAMENT_WATCH_CODEX`
and `FILAMENT_WATCH_PI` name the engine commands when they aren't on the path.

## Files

In the agent's folder:

| Path | What |
| --- | --- |
| `filament-watch` | this script |
| `filament-watch.conf` | its settings |
| `.filament-watch/log` | its log, when it runs with `start` or in a terminal |
| `.filament-watch/audit.jsonl` | one line per session and steer |
| `.filament-watch/cursor` | where `poll_work` left off |
| `.filament-watch/tasks/` | each conversation's session |
| `.filament-watch/queue/`, `runs/` | work waiting for its conversation, and running sessions |

`.filament-watch/` ignores itself in git. Nothing there holds a credential:
in wake mode the wake token stays in the watcher's memory. The agent's
sign-in stays where its engine keeps it (Claude Code's or Codex's own
settings), and its kit agent file stays in `~/.filament/agents`.

## Not yet

- Nothing restarts it after a reboot; a login service (launchd, systemd) or
  the desktop app could.
- Without a token it can't write in Filament itself, so a failed session is
  only logged; with one, it tells the owner in their backchannel.
- Steering into Codex needs Codex's app-server for `codex queue`.
- Codex sessions use the agent's server as Codex's config registers it (the
  desktop app and the connect skill both register it there).

## Tests

`tests/test_watch.py` runs the script against a fake Filament server and a
fake `claude`. Set `FILAMENT_WATCH_TEST_BASH=/bin/bash` on a Mac to run it
under bash 3.2.
