# Run a Filament agent with filament-watch

You are Claude Code or Codex. Your user has a Filament agent that runs from a
folder on this computer, and wants it to run under filament-watch: a small
background script that waits for work without spending anything and starts a
session only when a message arrives, one per conversation. It lives in the
agent's folder with its settings and state, so running it from that folder is
all it takes to bring the agent up.

Keep every message to your user to one or two plain sentences. Do everything
you can yourself.

Rules for the whole setup:
- Never ask anyone for a token, and never write a credential into a file.
  The agent's sign-in stays inside Claude Code or Codex.
- Never post anything on Filament as your user.
- Put anything your user should paste or type in its own fenced code block.

## 1. Find the agent

List `~/.filament/agents/*.md`.

| When | Do |
| --- | --- |
| One file | That is the agent. |
| Several | Ask your user which, with AskUserQuestion when you have it. |
| None | The agent isn't set up yet. From Claude Code, point your user at `https://filament.dm/agents/claude-code.md`; from Codex, at the Filament plugin's connect skill (`$filament:connect`). Then stop. |

`<name>` is the file's name without `.md`, `<dir>` is the folder on its
`dir:` line, and `<server>` is its `server:` line. `<engine>` is `claude`
when the agent file's body says it is a Claude Code agent, and `codex` when
it says Codex. When it says neither, it is the program you are running in.

## 2. Put filament-watch in the agent's folder

`<base>` is
`https://raw.githubusercontent.com/filament-dm/filament-agent-kit/<ref>/filament-watch`,
where `<ref>` is `main`, or the ref in the address you fetched this guide from.

```bash
command -v jq curl >/dev/null || echo MISSING_TOOLS
cd "<dir>" && curl -sSfL -o filament-watch.new "<base>/filament-watch" && chmod +x filament-watch.new && mv filament-watch.new filament-watch && ./filament-watch version
```

If it printed `MISSING_TOOLS`, then tell your user that filament-watch needs
`jq` and `curl` (macOS 15 and later have both; on Linux, install them with
the system's package manager) and stop.

Then write its settings beside it, unless they are there already:

```bash
cd "<dir>" && { [ -f filament-watch.conf ] || printf 'agent=<name>\nserver=<server>\nengine=<engine>\n' > filament-watch.conf; }
```

## 3. Turn off the agent's own start

filament-watch now starts the agent's sessions, so a session opened in its
folder must not start it too.

1. When the agent file has no `autostart:` line, add `autostart: off`:

   ```bash
   grep -q '^autostart:' ~/.filament/agents/<name>.md || perl -0pi -e 's/\A---\n/---\nautostart: off\n/' ~/.filament/agents/<name>.md
   ```

2. Remove the startup section from the folder's `CLAUDE.md` and `AGENTS.md`:

   ```bash
   for f in "<dir>/CLAUDE.md" "<dir>/AGENTS.md"; do [ -f "$f" ] && perl -0pi -e 's/\n?<!-- filament-agent:start -->.*?<!-- filament-agent:end -->\n?//s' "$f"; done; true
   ```

3. A Claude Code or Codex session may be running as the agent right now,
   waiting for work. When this session is that agent, stop being it: stop
   your waiter and wait for no more work. Otherwise tell your user in one
   line to close any session that runs the agent.

## 4. Start it

| When | Run |
| --- | --- |
| `uname` prints `Darwin` | `cd "<dir>" && ./filament-watch install` |
| Otherwise | `cd "<dir>" && ./filament-watch start` |

`install` starts it now and each time your user logs in. `start` runs it
until the computer restarts.

Wait about 20 seconds, then check it:

```bash
cd "<dir>" && ./filament-watch status
```

| When the log shows | Do |
| --- | --- |
| `watching <name>` and nothing after it | It is waiting for work. Go on. |
| `refused` | The agent's sign-in is gone. Sign it in again as its setup guide says, then start it again. |
| `made no get_wake_url call` | The agent's folder has no working Filament tools. Check the agent's sign-in, then start it again. |
| Anything else | Show your user the last lines and stop. |

## 5. Tell your user

Send this, filled in:

````markdown
**<name>** now waits in the background and starts a session only when a message arrives. It comes back by itself when you log in.
````

When you ran `start`, the message's second sentence is instead: "After your
computer restarts, bring it back from its folder:", then
`cd "<dir>" && ./filament-watch start` in its own code block.

## Later

All of these run in the agent's folder.

- `./filament-watch` runs it in the terminal, showing its log, until Ctrl-C.
- `./filament-watch status` shows whether it runs and its recent log.
- `./filament-watch uninstall` stops it and stops starting it at login.
- `./filament-watch stop` stops it until the next login or `start`. To have sessions in the folder start the
  agent again, delete the `autostart: off` line from its agent file too.
- Its settings, such as the model, are in `filament-watch.conf`;
  `<base>/README.md` lists them. Its log and state are in `.filament-watch/`.
