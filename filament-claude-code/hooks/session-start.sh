#!/bin/sh
# SessionStart hook: when a session opens in a Filament agent's folder, tell
# it to start that agent before anything else. Elsewhere it prints nothing.
#
# An agent file with `autostart: off` in its frontmatter is skipped, for a
# folder its owner also works in.

agents="$HOME/.filament/agents"
here=$(pwd -P 2>/dev/null || pwd)

# The value of a frontmatter key, without surrounding quotes.
field() {
  awk -v key="$2" '
    NR == 1 && /^---$/ { inside = 1; next }
    inside && /^---$/ { exit }
    inside && index($0, key ":") == 1 {
      value = substr($0, length(key) + 2)
      sub(/^[ \t]+/, "", value); sub(/[ \t]+$/, "", value)
      if (value ~ /^".*"$/ || value ~ /^'\''.*'\''$/) value = substr(value, 2, length(value) - 2)
      print value
      exit
    }' "$1"
}

same_dir() {
  [ -n "$1" ] && [ -d "$1" ] && [ "$(cd "$1" 2>/dev/null && pwd -P)" = "$here" ]
}

command=/filament:filament-agent

say() {
  # $1 agent name or empty for a first run, $2 server or empty.
  if [ -n "$1" ]; then
    start="$command $1"
    what="the Filament agent \`$1\`"
  else
    start=$command
    what="a Filament agent that has not had its first session yet"
  fi
  echo "This folder is home to $what, and this session runs it."
  echo "Before you answer your user's first message, start the agent: follow the \`filament:filament-agent\` skill exactly as if your user had sent \`$start\`."
  echo "Start it once, even when their first message also asks for it, then answer that message as the skill says."
  if [ -n "$2" ]; then
    echo "If you have no Filament tools from the MCP server \`$2\`, skip all of this: that agent runs in another program."
  fi
}

for file in "$agents"/*.md; do
  [ -f "$file" ] || continue
  same_dir "$(field "$file" dir)" || continue
  case $(field "$file" autostart | tr '[:upper:]' '[:lower:]') in
    off | false | no) exit 0 ;;
  esac
  say "$(basename "$file" .md)" "$(field "$file" server)"
  exit 0
done

# Setup left this for the agent's first run, before its agent file exists.
if [ -f .filament-agent-name ]; then
  say "" ""
fi
exit 0
