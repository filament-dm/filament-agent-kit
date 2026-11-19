import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "filament-claude-code/hooks/session-start.sh"


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

    def run_hook(self, cwd):
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        result = subprocess.run(
            ["sh", str(HOOK)],
            cwd=cwd,
            env=env,
            input='{"hook_event_name": "SessionStart", "source": "startup"}',
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_hooks_json_runs_the_script_on_session_start(self):
        path = ROOT / "filament-claude-code/hooks/hooks.json"
        (group,) = json.loads(path.read_text())["hooks"]["SessionStart"]
        self.assertEqual(group["matcher"], "startup|resume|clear")
        (hook,) = group["hooks"]
        self.assertEqual(
            hook["command"], 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"'
        )

    def test_silent_outside_an_agent_folder(self):
        agent_file(self.home, "sprocket", "filament-sprocket", self.folder)
        self.assertEqual(self.run_hook(self.other), "")

    def test_silent_without_any_agents(self):
        self.assertEqual(self.run_hook(self.folder), "")

    def test_claude_code_starts_the_folder_agent(self):
        agent_file(self.home, "sprocket", "filament-sprocket", self.folder)
        out = self.run_hook(self.folder)
        self.assertIn("`/filament:filament-agent sprocket`", out)
        self.assertIn("MCP server `filament-sprocket`", out)

    def test_quoted_dir_and_symlinked_cwd_match(self):
        agent_file(self.home, "sprocket", "filament-sprocket", f'"{self.folder}"')
        link = self.other / "link"
        link.symlink_to(self.folder)
        out = self.run_hook(link)
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
            self.assertEqual(self.run_hook(self.folder), "", value)

    def test_first_run_uses_the_name_file(self):
        (self.folder / ".filament-agent-name").write_text("Irena's agent\n")
        out = self.run_hook(self.folder)
        self.assertIn("sent `/filament:filament-agent`.", out)
        self.assertIn("has not had its first session", out)


if __name__ == "__main__":
    unittest.main()
