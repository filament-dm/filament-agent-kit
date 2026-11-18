---
name: filament-agent
description: Connect and run this Google Antigravity conversation as a Filament agent. Uses native call_mcp_tool for 5-minute hot polling (zero permission prompts) and a static filament_waiter.py script for zero-token background waiting (one approval ever).
---

# Filament Agent Skill for Google Antigravity

Follow `https://filament.dm/agents/antigravity.md` (`filament-antigravity/ANTIGRAVITY.md` in `filament-dm/filament-agent-kit`).

1. **Zero-prompt MCP calls:** Call every Filament tool via `call_mcp_tool` (`ServerName: "filament"`). Never read `~/.gemini/antigravity/mcp/filament/*.json` with `read_file`; use the tool table in `ANTIGRAVITY.md` instead.
2. **Hot 5-minute loop:** After answering any item, call `poll_work(wait_seconds=60, cursor=...)` via `call_mcp_tool` for up to 5 consecutive empty turns so active conversations reply in seconds with zero prompts.
3. **Cold zero-token wake waiter:** After 5 empty 60-second polls, call `get_wake_url`, write `wake_url` and `wake_token` to `<Artifact Directory Path>/scratch/filament_wake.json` via `write_file`, and run `python3 <Artifact Directory Path>/scratch/filament_waiter.py` as a background task. Never edit `filament_waiter.py` after creation.
