import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_HOOK = ROOT / "filament-claude-code/hooks/session-start.sh"
CODEX_HOOK = ROOT / "filament-codex/hooks/session-start.sh"


def agent_file(home, name, server, folder, extra=""):
    agents = home / ".filament/agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        "---\n"
        f"server: {server}\n"
        f"dir: {folder}\n"
        f'backchannel: "!room:example.org"\n'
        f"{extra}"
        "---\n"
        f"# {name}\n"
    )


class SessionStartHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.folder = base / "agent folder"
        self.folder.mkdir()
        self.other = base / "elsewhere"
        self.other.mkdir()

    def run_hook(self, harness, cwd):
        hook = CODEX_HOOK if harness == "codex" else CLAUDE_HOOK
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        result = subprocess.run(
            ["sh", str(hook), harness],
            cwd=cwd,
            env=env,
            input='{"hook_event_name": "SessionStart", "source": "startup"}',
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stderr, "")
        return result.stdout

    def codex_server(self, server):
        codex = self.home / ".codex"
        codex.mkdir(exist_ok=True)
        (codex / "config.toml").write_text(
            f'[mcp_servers.{server}]\nurl = "https://api.filament.dm/mcp/agents"\n'
        )

    def test_both_plugins_carry_the_same_script(self):
        self.assertEqual(CLAUDE_HOOK.read_text(), CODEX_HOOK.read_text())

    def test_hooks_json_runs_the_script_on_session_start(self):
        for plugin, root_var, harness in (
            ("filament-claude-code", "CLAUDE_PLUGIN_ROOT", "claude-code"),
            ("filament-codex", "PLUGIN_ROOT", "codex"),
        ):
            config = json.loads((ROOT / plugin / "hooks/hooks.json").read_text())
            (group,) = config["hooks"]["SessionStart"]
            self.assertEqual(group["matcher"], "startup|resume|clear")
            (hook,) = group["hooks"]
            self.assertEqual(
                hook["command"],
                f'sh "${{{root_var}}}/hooks/session-start.sh" {harness}',
            )

    def test_silent_outside_an_agent_folder(self):
        agent_file(self.home, "sprocket", "filament-sprocket", self.folder)
        self.assertEqual(self.run_hook("claude-code", self.other), "")

    def test_silent_without_any_agents(self):
        self.assertEqual(self.run_hook("claude-code", self.folder), "")

    def test_claude_code_starts_the_folder_agent(self):
        agent_file(self.home, "sprocket", "filament-sprocket", self.folder)
        out = self.run_hook("claude-code", self.folder)
        self.assertIn("`/filament:filament-agent sprocket`", out)
        self.assertIn("MCP server `filament-sprocket`", out)

    def test_quoted_dir_and_symlinked_cwd_match(self):
        agent_file(self.home, "sprocket", "filament-sprocket", f'"{self.folder}"')
        link = self.other / "link"
        link.symlink_to(self.folder)
        out = self.run_hook("claude-code", link)
        self.assertIn("/filament:filament-agent sprocket", out)

    def test_autostart_off_is_silent(self):
        for value in ("off", "false", "No"):
            agent_file(
                self.home,
                "sprocket",
                "filament-sprocket",
                self.folder,
                extra=f"autostart: {value}\n",
            )
            self.assertEqual(self.run_hook("claude-code", self.folder), "", value)

    def test_first_run_uses_the_name_file(self):
        (self.folder / ".filament-agent-name").write_text("Irena's agent\n")
        out = self.run_hook("claude-code", self.folder)
        self.assertIn("sent `/filament:filament-agent`.", out)
        self.assertIn("has not had its first session", out)

    def test_codex_starts_its_own_agent(self):
        agent_file(self.home, "wafflebyte", "filament-wafflebyte", self.folder)
        self.codex_server("filament-wafflebyte")
        out = self.run_hook("codex", self.folder)
        self.assertIn("`$filament:filament-agent wafflebyte`", out)
        self.assertNotIn("MCP server", out)

    def test_codex_skips_another_harness_agent(self):
        agent_file(self.home, "sprocket", "filament-sprocket", self.folder)
        self.codex_server("filament-other")
        self.assertEqual(self.run_hook("codex", self.folder), "")

    def test_codex_first_run(self):
        (self.folder / ".filament-agent-name").write_text("Waffle\n")
        out = self.run_hook("codex", self.folder)
        self.assertIn("sent `$filament:filament-agent`.", out)
        self.assertIn("first chat", out)


if __name__ == "__main__":
    unittest.main()
