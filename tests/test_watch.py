"""filament-watch against a fake Filament server and a fake `claude`.

The fake server speaks just enough MCP (initialize, poll_work,
message_principal) and the wake URL. The fake `claude` records its arguments
and answers like Claude Code: a stream-json tool result for a fetch session,
DELIVERED for a steering dispatcher, a JSON result for a work session.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "filament-watch/filament-watch"
BASH = os.environ.get("FILAMENT_WATCH_TEST_BASH", "bash")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
fake = Path(os.environ["FAKE_DIR"])
with open(fake / "calls.jsonl", "a") as f:
    f.write(json.dumps({"args": args, "token": "FILAMENT_AGENT_TOKEN" in os.environ}) + "\n")

def value(flag):
    return args[args.index(flag) + 1] if flag in args else None

prompt = value("-p") or ""
if prompt.startswith("/filament:filament-agent "):
    # The kit's first run: names the agent, writes its agent file, says hello.
    name_file = Path(".filament-agent-name")
    if os.environ.get("FAKE_FIRST_RUN") != "fail" and name_file.exists():
        slug = prompt.split(" ", 1)[1].strip()
        Path(os.environ["FILAMENT_AGENTS_DIR"], slug + ".md").write_text(
            f"---\nserver: filament-{slug}\ndir: {os.getcwd()}\n---\n# {slug}\n")
        name_file.unlink()
    print(json.dumps({"type": "result", "is_error": False}))
elif value("--output-format") == "stream-json":
    tool = (value("--allowedTools") or "").split("__")[-1]
    queue = sorted((fake / "fetch" / tool).glob("*.json")) if (fake / "fetch" / tool).is_dir() else []
    text = queue[0].read_text() if queue else json.dumps({"work": [], "cursor": None})
    if queue:
        queue[0].unlink()
    print(json.dumps({"type": "system", "subtype": "init"}))
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": text}]}]}}))
    print(json.dumps({"type": "result", "is_error": False}))
elif "steering dispatcher" in prompt:
    print(os.environ.get("FAKE_DISPATCH", "DELIVERED"))
else:
    time.sleep(float(os.environ.get("FAKE_WORK_SECONDS", "0.3")))
    (fake / "done").mkdir(exist_ok=True)
    (fake / "done" / str(time.time())).write_text("")
    print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.01,
                      "session_id": value("--session-id") or value("--resume")}))
'''


FAKE_PI = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

fake = Path(os.environ["FAKE_DIR"])
with open(fake / "pi.jsonl", "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd(),
                        "agent_dir": os.environ.get("PI_CODING_AGENT_DIR")}) + "\n")
if os.environ.get("FAKE_PI_EXIT"):
    sys.exit(1)
if os.environ.get("FAKE_PI_REFUSED"):
    paused = Path(os.environ["PI_CODING_AGENT_DIR"], "filament")
    paused.mkdir(parents=True, exist_ok=True)
    (paused / "paused.json").write_text(json.dumps({"reason": "credential rejected", "at": 1}))
sys.stdin.read()
(fake / "pi-eof").write_text("")
'''


def message_item(event_id, room="!room:x", thread=None, principal=True, backchannel=True):
    return {
        "kind": "message",
        "channel_id": room,
        "thread_id": thread,
        "is_backchannel": backchannel,
        "messages": [{"event_id": event_id, "body": "hi", "is_from_principal": principal}],
        "reply_with": {"tool": "post_message", "args": {"channel": room, "in_reply_to": event_id}},
    }


class FakeFilament:
    """Answers poll_work from a script of batches; each batch may wait until
    some number of work sessions have finished."""

    def __init__(self, fake_dir):
        self.fake_dir = fake_dir
        self.batches = []
        self.wakes = []
        self.calls = []
        self.status_for_poll = 200
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def finished_sessions(self):
        done = self.fake_dir / "done"
        return len(list(done.iterdir())) if done.is_dir() else 0

    def next_batch(self, cursor):
        if self.batches and self.finished_sessions() >= self.batches[0].get("after_sessions", 0):
            return self.batches.pop(0)["result"]
        return {"work": [], "cursor": cursor}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=None, headers=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self.path.startswith("/wake"):
                    return self._send(404, {})
                if self.headers.get("authorization") != "Wake tok":
                    return self._send(401, {})
                pending = fake.wakes.pop(0) if fake.wakes else 0
                self._send(200, {"pending": pending})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
                if self.headers.get("authorization") != "Bearer fmcp_testtoken":
                    return self._send(401, {})
                method = body.get("method")
                if method == "initialize":
                    return self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                        "protocolVersion": "2025-03-26"}}, {"mcp-session-id": "s1"})
                if method == "notifications/initialized":
                    return self._send(202)
                params = body["params"]
                fake.calls.append(params)
                if params["name"] == "poll_work":
                    if fake.status_for_poll != 200:
                        return self._send(fake.status_for_poll, {})
                    result = fake.next_batch(params["arguments"].get("cursor"))
                else:
                    result = {"ok": True}
                self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}]}})

        return Handler

    def close(self):
        self.server.shutdown()


@unittest.skipUnless(shutil.which("jq") and shutil.which("curl"), "needs jq and curl")
class WatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.fake_dir = base / "fake"
        self.fake_dir.mkdir()
        # The agent's folder holds the watcher, its settings and its state.
        self.folder = base / "Filament Agents" / "Tester"
        self.folder.mkdir(parents=True)
        self.watch = self.folder / "filament-watch"
        shutil.copy(SCRIPT, self.watch)
        self.conf = self.folder / "filament-watch.conf"
        self.conf.write_text("agent=tester\nserver=filament-tester\nengine=claude\n")
        self.state = self.folder / ".filament-watch"
        self.agents = base / "agents"
        self.agents.mkdir()
        (self.agents / "tester.md").write_text(
            f"---\nserver: filament-tester\ndir: {self.folder}\nautostart: off\n---\n# Tester\n"
        )
        self.claude = base / "claude"
        self.claude.write_text(FAKE_CLAUDE)
        self.claude.chmod(0o755)
        self.server = FakeFilament(self.fake_dir)
        self.addCleanup(self.server.close)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(base),
            "FILAMENT_AGENTS_DIR": str(self.agents),
            "FILAMENT_WATCH_CLAUDE": str(self.claude),
            "FILAMENT_WATCH_EXIT_IDLE": "1",
            "FAKE_DIR": str(self.fake_dir),
        }

    def token_env(self):
        return {**self.env, "FILAMENT_AGENT_TOKEN": "fmcp_testtoken",
                "FILAMENT_SERVER_URL": f"{self.server.url}/mcp"}

    def run_watch(self, env, *args):
        proc = subprocess.run([BASH, str(self.watch), *args], env=env, cwd=self.folder.parent,
                              capture_output=True, text=True, timeout=60)
        self.log = proc.stderr
        return proc

    def claude_calls(self, kind=None):
        path = self.fake_dir / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        def kind_of(call):
            args = call["args"]
            if "stream-json" in args:
                return "fetch"
            if "steering dispatcher" in args[args.index("-p") + 1]:
                return "dispatcher"
            return "work"
        return [c["args"] for c in calls if kind is None or kind_of(c) == kind]

    def value(self, args, flag):
        return args[args.index(flag) + 1]

    def test_a_principal_message_starts_a_session_with_every_tool(self):
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env(), "run")
        work = self.claude_calls("work")
        self.assertEqual(len(work), 1, self.log)
        args = work[0]
        self.assertIn("--session-id", args)
        self.assertEqual(self.value(args, "--permission-mode"), "auto")
        self.assertIn("Mode: principal", self.value(args, "-p"))
        self.assertIn("$e1", self.value(args, "-p"))
        self.assertIn("mcp__filament-tester__*", self.value(args, "--append-system-prompt"))
        self.assertEqual((self.state / "cursor").read_text().strip(), "c1")
        # The next poll passes the cursor, which acknowledges the work.
        polls = [c["arguments"] for c in self.server.calls if c["name"] == "poll_work"]
        self.assertEqual(polls[1].get("cursor"), "c1")

    def test_the_token_never_reaches_a_claude_session(self):
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        calls = [json.loads(line) for line in (self.fake_dir / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertFalse(any(c["token"] for c in calls))

    def test_it_runs_with_no_command_and_keeps_its_state_in_the_folder(self):
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        self.assertEqual(len(self.claude_calls("work")), 1, self.log)
        self.assertEqual((self.state / ".gitignore").read_text(), "*\n")
        self.assertTrue((self.state / "audit.jsonl").is_file())
        self.assertFalse((self.folder.parent.parent / ".filament").exists())

    def test_sessions_learn_who_they_are_from_the_agent_file(self):
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        notes = self.value(self.claude_calls("work")[0], "--append-system-prompt")
        self.assertIn(f"cat '{self.agents / 'tester.md'}'", notes)
        self.assertIn("filament-watch.conf", notes)
        self.assertIn("### Which messages to answer", notes)
        self.assertNotIn("{{", notes)

    def test_without_an_agent_file_sessions_call_get_self(self):
        (self.agents / "tester.md").unlink()
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        notes = self.value(self.claude_calls("work")[0], "--append-system-prompt")
        self.assertIn("get_self", notes)

    def test_a_new_agent_has_its_first_session_then_its_work(self):
        (self.agents / "tester.md").unlink()
        (self.folder / ".filament-agent-name").write_text("Tester\n")
        (self.folder / "CLAUDE.md").write_text(
            "# Mine\n\n<!-- filament-agent:start -->\nstart it\n<!-- filament-agent:end -->\n")
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        calls = self.claude_calls()
        self.assertEqual(self.value(calls[0], "-p"), "/filament:filament-agent tester", self.log)
        self.assertIn("never start a waiter", self.value(calls[0], "--append-system-prompt"))
        self.assertEqual(len(self.claude_calls("work")), 2)  # the first run, then the work
        self.assertIn("$e1", self.value(calls[1], "-p"))
        agent_file = (self.agents / "tester.md").read_text()
        self.assertIn("autostart: off", agent_file)
        self.assertEqual((self.folder / "CLAUDE.md").read_text(), "# Mine\n")
        self.assertIn(f"cat '{self.agents / 'tester.md'}'", self.value(calls[1], "--append-system-prompt"))
        # No poll waited on the agent's work before its first session was done.
        self.assertTrue(self.server.calls)

    def test_another_folders_agent_file_is_never_this_agents(self):
        other = self.folder.parent / "Other"
        other.mkdir()
        (self.agents / "tester.md").write_text(
            f"---\nserver: filament-tester\ndir: {other}\n---\n# Someone else\n")
        self.server.batches = [{"result": {"work": [message_item("$e1")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        notes = self.value(self.claude_calls("work")[0], "--append-system-prompt")
        self.assertIn("get_self", notes, self.log)
        self.assertNotIn("autostart", (self.agents / "tester.md").read_text())

    def test_a_first_run_never_takes_another_folders_agent_file(self):
        other = self.folder.parent / "Other"
        other.mkdir()
        (self.agents / "tester.md").write_text(
            f"---\nserver: filament-tester\ndir: {other}\n---\n# Someone else\n")
        (self.folder / ".filament-agent-name").write_text("Tester\n")
        proc = self.run_watch(self.token_env())
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("belongs to another folder", self.log)
        self.assertEqual(self.claude_calls(), [])

    def test_a_discarded_agent_never_has_a_first_session(self):
        (self.agents / "tester.md").unlink()
        (self.folder / ".filament-agent-name").write_text("Tester\n")
        env = {**self.token_env(), "FILAMENT_AGENT_TOKEN": "fmcp_revoked"}
        proc = self.run_watch(env)
        self.assertEqual(proc.returncode, 0, self.log)
        self.assertIn("refused this agent's sign-in", self.log)
        self.assertEqual(self.claude_calls(), [])

    def test_settings_without_a_name_take_it_from_the_agent_file(self):
        self.conf.write_text("engine=claude\n")
        self.run_watch(self.token_env())
        conf = self.conf.read_text()
        self.assertIn("agent=tester\n", conf, self.log)
        self.assertIn("server=filament-tester\n", conf)

    def test_someone_else_gets_only_filament_tools(self):
        item = message_item("$e1", principal=False, backchannel=False)
        self.server.batches = [{"result": {"work": [item], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        args = self.claude_calls("work")[0]
        self.assertEqual(self.value(args, "--permission-mode"), "dontAsk")
        self.assertEqual(self.value(args, "--tools"), "")
        self.assertEqual(self.value(args, "--allowedTools"), "mcp__filament-tester")
        self.assertIn("Mode: restricted", self.value(args, "-p"))

    def test_the_same_conversation_resumes_its_session(self):
        self.server.batches = [
            {"result": {"work": [message_item("$e1")], "cursor": "c1"}},
            {"after_sessions": 1, "result": {"work": [message_item("$e2")], "cursor": "c2"}},
        ]
        self.run_watch(self.token_env())
        first, second = self.claude_calls("work")
        self.assertEqual(self.value(second, "--resume"), self.value(first, "--session-id"))
        self.assertIn("Session: resumed", self.value(second, "-p"))

    def test_someone_elses_turn_never_resumes_the_owners_session(self):
        self.server.batches = [
            {"result": {"work": [message_item("$e1")], "cursor": "c1"}},
            {"after_sessions": 1, "result": {"work": [
                message_item("$e2", principal=False)], "cursor": "c2"}},
            {"after_sessions": 2, "result": {"work": [message_item("$e3")], "cursor": "c3"}},
        ]
        self.run_watch(self.token_env())
        owner, other, owner_again = self.claude_calls("work")
        self.assertIn("--session-id", other, self.log)
        self.assertNotEqual(self.value(other, "--session-id"), self.value(owner, "--session-id"))
        self.assertIn("Session: fresh", self.value(other, "-p"))
        # The owner's next turn picks their own session back up.
        self.assertEqual(self.value(owner_again, "--resume"), self.value(owner, "--session-id"))

    def test_another_thread_gets_its_own_session(self):
        self.server.batches = [{"result": {"work": [
            message_item("$e1"), message_item("$e2", thread="$root")], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        work = self.claude_calls("work")
        self.assertEqual(len(work), 2)
        self.assertNotEqual(self.value(work[0], "--session-id"), self.value(work[1], "--session-id"))

    def test_new_work_steers_into_a_running_session(self):
        env = {**self.token_env(), "FAKE_WORK_SECONDS": "3"}
        self.server.batches = [
            {"result": {"work": [message_item("$e1")], "cursor": "c1"}},
            {"result": {"work": [message_item("$e2")], "cursor": "c2"}},
        ]
        self.run_watch(env)
        self.assertEqual(len(self.claude_calls("work")), 1, self.log)
        dispatch = self.claude_calls("dispatcher")
        self.assertEqual(len(dispatch), 1)
        self.assertIn("STEERING from filament-watch", self.value(dispatch[0], "-p"))
        self.assertIn("$e2", self.value(dispatch[0], "-p"))
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"steered"', audit)

    def test_work_that_cannot_be_steered_runs_after_the_session(self):
        env = {**self.token_env(), "FAKE_WORK_SECONDS": "2", "FAKE_DISPATCH": "NOSIBLING"}
        self.server.batches = [
            {"result": {"work": [message_item("$e1")], "cursor": "c1"}},
            {"result": {"work": [message_item("$e2")], "cursor": "c2"}},
        ]
        self.run_watch(env)
        first, second = self.claude_calls("work")
        self.assertIn("$e2", self.value(second, "-p"))
        self.assertEqual(self.value(second, "--resume"), self.value(first, "--session-id"))

    def test_reactions_are_skipped_by_default(self):
        reaction = {"kind": "reaction", "channel_id": "!room:x", "thread_id": None,
                    "reactions": [{"event_id": "$r", "is_from_principal": True}], "reply_with": None}
        self.server.batches = [{"result": {"work": [reaction], "cursor": "c1"}}]
        self.run_watch(self.token_env())
        self.assertEqual(self.claude_calls("work"), [])

    def test_a_refused_sign_in_stops_the_watcher(self):
        self.server.status_for_poll = 401
        proc = self.run_watch(self.token_env())
        self.assertEqual(proc.returncode, 0)
        self.assertIn("refused", self.log)
        self.assertFalse((self.state / "lock").exists())

    def test_without_a_token_it_waits_on_the_wake_url(self):
        fetch = self.fake_dir / "fetch"
        (fetch / "get_wake_url").mkdir(parents=True)
        (fetch / "get_wake_url/1.json").write_text(json.dumps(
            {"wake_url": f"{self.server.url}/wake", "wake_token": "tok"}))
        (fetch / "poll_work").mkdir()
        (fetch / "poll_work/1.json").write_text(json.dumps(
            {"work": [message_item("$e1")], "cursor": "c1"}))
        self.server.wakes = [1]
        self.run_watch(self.env)
        fetches = self.claude_calls("fetch")
        self.assertEqual([self.value(f, "--allowedTools") for f in fetches],
                         ["mcp__filament-tester__get_wake_url", "mcp__filament-tester__poll_work"],
                         self.log)
        self.assertEqual(self.value(fetches[0], "--tools"), "")
        work = self.claude_calls("work")
        self.assertEqual(len(work), 1)
        self.assertIn("$e1", self.value(work[0], "-p"))
        self.assertEqual((self.state / "cursor").read_text().strip(), "c1")

    def test_start_twice_runs_one_watcher(self):
        env = {**self.token_env()}
        env.pop("FILAMENT_WATCH_EXIT_IDLE")
        start = lambda: subprocess.run([BASH, str(self.watch), "start"], env=env,
                                       capture_output=True, text=True, timeout=30)
        first = start()
        self.addCleanup(lambda: subprocess.run([BASH, str(self.watch), "stop"], env=env,
                                               capture_output=True, timeout=30))
        self.assertIn("started", first.stdout, first.stderr)
        self.assertIn("already runs", start().stdout)


@unittest.skipUnless(shutil.which("jq"), "needs jq")
class PiTests(unittest.TestCase):
    """For Pi, the watcher keeps one `pi --mode rpc` running on the folder's
    own Pi agent directory; Filament's Pi package does the rest."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.fake_dir = base / "fake"
        self.fake_dir.mkdir()
        self.folder = base / "Filament Agents" / "My Pi Agent"
        (self.folder / ".pi-agent").mkdir(parents=True)
        self.watch = self.folder / "filament-watch"
        shutil.copy(SCRIPT, self.watch)
        pi = base / "pi"
        pi.write_text(FAKE_PI)
        pi.chmod(0o755)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(base),
            "FILAMENT_AGENTS_DIR": str(base / "agents"),
            "FILAMENT_WATCH_PI": str(pi),
            "FILAMENT_WATCH_PI_RESTART_S": "1",
            "FAKE_DIR": str(self.fake_dir),
        }

    def watch_cmd(self, command, env=None):
        return subprocess.run([BASH, str(self.watch), command], env=env or self.env,
                              capture_output=True, text=True, timeout=30)

    def started(self):
        path = self.fake_dir / "pi.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, check, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return True
            time.sleep(0.1)
        return False

    def test_it_runs_pi_on_the_folders_agent_directory(self):
        self.assertIn("started", self.watch_cmd("start").stdout)
        self.addCleanup(lambda: self.watch_cmd("stop"))
        self.assertTrue(self.wait_for(lambda: self.started()), self.watch_cmd("status").stdout)
        run = self.started()[0]
        self.assertEqual(run["args"], ["--mode", "rpc"])
        self.assertEqual(run["agent_dir"], str(self.folder / ".pi-agent"))
        self.assertEqual(run["cwd"], str(self.folder))
        log = (self.folder / ".filament-watch/log").read_text()
        self.assertIn("watching my-pi-agent", log)
        # Its stdin stays open while the watcher runs.
        time.sleep(1)
        self.assertFalse((self.fake_dir / "pi-eof").exists())
        self.assertEqual(len(self.started()), 1)

    def test_stop_stops_pi(self):
        self.watch_cmd("start")
        self.assertTrue(self.wait_for(lambda: self.started()))
        pid = self.started()[0]["pid"]
        self.assertIn("stopped", self.watch_cmd("stop").stdout)

        def gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        self.assertTrue(self.wait_for(gone))

    def test_a_refused_pi_agent_stops_the_watcher(self):
        proc = subprocess.run([BASH, str(self.watch), "run"], env={**self.env, "FAKE_PI_REFUSED": "1"},
                              capture_output=True, text=True, timeout=30)
        self.assertIn("refused", proc.stderr)
        self.assertEqual(len(self.started()), 1)
        self.assertFalse((self.folder / ".filament-watch/lock").exists())
        # Started again, it stops before starting Pi.
        again = subprocess.run([BASH, str(self.watch), "run"], env=self.env,
                               capture_output=True, text=True, timeout=30)
        self.assertIn("refused", again.stderr)
        self.assertEqual(len(self.started()), 1)

    def test_pi_starts_again_and_continues_its_session(self):
        sessions = self.folder / ".pi-agent/sessions/--x--"
        sessions.mkdir(parents=True)
        (sessions / "s.jsonl").write_text("{}\n")
        self.watch_cmd("start", {**self.env, "FAKE_PI_EXIT": "1"})
        self.addCleanup(lambda: self.watch_cmd("stop"))
        self.assertTrue(self.wait_for(lambda: len(self.started()) >= 2), self.watch_cmd("status").stdout)
        self.assertEqual(self.started()[1]["args"], ["--mode", "rpc", "--continue"])


if __name__ == "__main__":
    unittest.main()
