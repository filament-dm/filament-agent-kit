import ast
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "filament-antigravity"
WAITER = BUNDLE / "scripts" / "filament_waiter.py"
GUIDE = BUNDLE / "ANTIGRAVITY.md"
RULE_TRIGGERS = {"always_on", "model_decision", "glob", "manual"}


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    return fields


class FakeWake:
    """A wake endpoint that answers each GET with the next scripted reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.headers = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.headers.append(dict(self.headers))
                code, body = fake.replies.pop(0) if fake.replies else (200, {"pending": 1})
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/wake"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WaiterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(WAITER, self.dir / "filament_waiter.py")

    def run_waiter(self, url, token="tok123", timeout=20):
        (self.dir / "filament_wake.json").write_text(
            json.dumps({"wake_url": url, "wake_token": token})
        )
        return subprocess.run(
            [sys.executable, str(self.dir / "filament_waiter.py")],
            capture_output=True, text=True, timeout=timeout, cwd=self.dir,
        )

    def serve(self, replies):
        fake = FakeWake(replies)
        self.addCleanup(fake.close)
        return fake

    def test_exits_zero_with_wake_line_when_work_is_pending(self):
        fake = self.serve([(200, {"pending": 2})])
        result = self.run_waiter(fake.url)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("WAKE "), result.stdout)
        self.assertIn('"pending": 2', result.stdout)
        self.assertEqual(fake.headers[0]["Authorization"], "Wake tok123")

    def test_keeps_waiting_while_nothing_is_pending(self):
        fake = self.serve([(200, {"pending": 0}), (200, {"pending": 0}), (200, {"pending": 1})])
        result = self.run_waiter(fake.url)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(fake.headers), 3)

    def test_server_errors_back_off_and_retry(self):
        fake = self.serve([(503, "busy"), (200, {"pending": 1})])
        result = self.run_waiter(fake.url)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(fake.headers), 2)

    def test_terminal_statuses(self):
        cases = [
            (401, 2, "WAKE_URL_EXPIRED"),
            (410, 3, "WAKE_REFUSED"),
            (403, 4, "WAKE_FORBIDDEN"),
        ]
        for code, exit_code, marker in cases:
            with self.subTest(code=code):
                fake = self.serve([(code, '{"error":"no"}')])
                result = self.run_waiter(fake.url)
                self.assertEqual(result.returncode, exit_code)
                self.assertTrue(result.stdout.startswith(marker), result.stdout)

    def test_gives_up_when_the_server_was_never_reachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = self.run_waiter(f"http://127.0.0.1:{port}/wake")
        self.assertEqual(result.returncode, 5)
        self.assertTrue(result.stdout.startswith("WAKE_UNREACHABLE"), result.stdout)


class BundleTests(unittest.TestCase):
    def test_guide_embeds_the_same_waiter(self):
        guide = GUIDE.read_text(encoding="utf-8")
        blocks = re.findall(r"( *)```python\n(.*?)\n *```", guide, re.S)
        embedded = [
            textwrap.dedent(body) for indent, body in blocks
            if "filament_wake.json" in body
        ]
        self.assertEqual(len(embedded), 1, "the guide should carry one waiter")
        self.assertEqual(
            embedded[0].strip(), WAITER.read_text(encoding="utf-8").strip(),
            "ANTIGRAVITY.md's waiter differs from scripts/filament_waiter.py",
        )

    def test_waiter_runs_on_macos_system_python(self):
        source = WAITER.read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))

    def test_plugin_manifest(self):
        manifest = json.loads((BUNDLE / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "filament")
        for key in ("displayName", "version", "description"):
            self.assertTrue(manifest.get(key), key)

    def test_skills_have_a_description(self):
        skills = sorted((BUNDLE / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for skill in skills:
            with self.subTest(skill=skill.parent.name):
                fields = frontmatter(skill)
                self.assertIsNotNone(fields, "SKILL.md needs frontmatter")
                self.assertTrue(fields.get("description"))

    def test_rules_have_a_trigger(self):
        # Antigravity silently drops a rules/*.md file without a valid trigger.
        rules = sorted((BUNDLE / "rules").glob("*.md"))
        self.assertTrue(rules)
        for rule in rules:
            with self.subTest(rule=rule.name):
                fields = frontmatter(rule)
                self.assertIsNotNone(fields, "rule needs YAML frontmatter")
                self.assertIn(fields.get("trigger"), RULE_TRIGGERS)
                if fields["trigger"] == "model_decision":
                    self.assertTrue(fields.get("description"))
                if fields["trigger"] == "glob":
                    self.assertTrue(fields.get("globs"))


if __name__ == "__main__":
    unittest.main()
