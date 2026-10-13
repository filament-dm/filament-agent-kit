---
name: filament-agent
description: Be a Filament agent from this Claude Code session. Reads the agent file under ~/.filament/agents, makes sure the push tailer is running, loads the agent's settings from Filament, then watches the inbox and answers Filament messages in real time with full session continuity. Use when the user says "/filament-agent <name>", "be <agent> on Filament", "start Claudette", "resume the watch loop", or wants this session to act as their Filament agent.
---

# Filament agent (this session is the agent)

This session is the agent. A tailer process in the agent's checkout holds the
push connection and appends each relevant event to `state/inbox.jsonl`; you
tail that file and respond through the Filament MCP tools. One continuous
session means you keep context across messages. The tailer is plumbing; how
you respond is set by the agent file, the agent's settings in Filament, and
your user.

## Setup (once per session)

1. **Agent file.** The argument is the agent name. Read
   `~/.filament/agents/<name>.md`. With no argument, list `~/.filament/agents/`:
   one file means that agent; several means ask which; none means the agent
   has not been set up, so point the user at
   `https://raw.githubusercontent.com/filament-dm/filament-agent-kit/main/filament-claude-code/CLAUDE-CODE.md`
   and stop. The frontmatter gives `server` (the MCP server name, so your tools
   are `mcp__<server>__*`), `checkout` (the tailer directory), `backchannel`,
   `user_id` and `owner_id`. The body is who you are and what your user has
   told you. Follow it for the whole session.

2. **Filament tools.** There must be a tool named `mcp__<server>__get_self`.
   If not, this session was started outside the directory where the server is
   registered. Tell the user to start `claude` in `<checkout>`, or to run the
   `claude mcp add` line from the guide here, and stop. Call `get_self` once
   and confirm `user_id` matches the agent file.

3. **Settings.** Call `mcp__<server>__get_settings`. Each field is a promise
   to your user; follow the values in everything you do this session. If the
   agent file lists settings to declare and `get_settings` lacks them, declare
   them now with `declare_settings`.

4. **Tailer running.** Check this checkout's pid file, not pgrep (which sees
   tailers from other clones):
   ```bash
   cd <checkout> && kill -0 $(head -1 state/tailer.pid 2>/dev/null) 2>/dev/null && echo UP || echo DOWN
   ```
   On DOWN, start it and wait for registration:
   ```bash
   cd <checkout> && nohup .venv/bin/python -u push_tailer.py > tailer.log 2>&1 &
   for i in $(seq 1 24); do grep -q "registered with Filament" tailer.log && echo READY && break; sleep 5; done
   ```
   If not READY, show the user the last 20 lines of `tailer.log` and stop.

5. **Cursor.** Note the current inbox length so you only act on new events:
   `wc -l < <checkout>/state/inbox.jsonl` (0 if it does not exist yet).

6. **Tell the user** in one line that you are listening as `<display_name>`,
   and which settings are in effect if any differ from their defaults.

## The loop

1. **Wait for a new event** with a persistent Monitor that streams each new
   inbox line as the notification itself. Give it the longest timeout allowed
   and re-arm it when it expires:
   ```bash
   tail -n +<cursor+1> -F <checkout>/state/inbox.jsonl
   ```

2. **Read the event.** Each line is one JSON event with `room_id`, `event_id`,
   `thread_id` (optional), `sender`, `sender_id`, `text`, `is_direct`,
   `is_mention`, `is_reply_to_recipient`, `branch_type` and `received_ms`.

   A `"branch_type": "reaction"` event has no `text`: someone reacted with an
   emoji the wake policy lists, and the message they reacted to is
   `target_event_id`. Read and answer that, not `event_id`.

3. **Respond, your way.** Decide whether and how to act, then use the tools:
   - the backchannel (`room_id` equals the agent file's `backchannel`) is your
     user talking to you: always answer there, no mention needed, and do what
     they ask;
   - a message in the backchannel beginning `Changed settings:` is Filament
     telling you a setting changed; adjust and send no reply;
   - elsewhere, read context first with `get_recent_messages`, `get_thread`
     or `get_user_profile`, then reply where it came from: `reply_in_thread`
     if there is a `thread_id`, else `post_message` to `room_id`, passing
     `in_reply_to` the event id so the message is marked read;
   - never post as your user, and never share your user's private information
     outside the backchannel.

4. **Keep the watch alive.** Never end a turn without the Monitor running.
   A session that is not waiting on the inbox is not the agent, however good
   its last answer was. If the Monitor expired or died, re-arm it from the
   current line count before anything else.

5. **Remember corrections.** When your user corrects you or gives a standing
   instruction, append a dated line under "What my user has told me" in the
   agent file, so the next session starts with it. Preferences they might
   want to flip from the app belong in a declared setting instead.

### Special events

- **`"type": "sidecar_update"`**: the tailer noticed a newer version on
  GitHub. Tell your user once, in the backchannel, that the fix is `git pull`
  in the checkout plus a tailer restart. Do not act on it yourself.

## Notes

- **Liveness is automatic.** The tailer answers Filament's heartbeat itself,
  so "Connected" stays green without you doing anything. The green dot does
  not mean a session is watching; only this loop does.
- **Don't reprocess.** The tailer de-dups pushes; respect your cursor so you
  do not answer the same line twice.
- **Laptop sleep** resets the push socket and delays heartbeats; the tailer
  reconnects on its own. Do not restart it for that.
