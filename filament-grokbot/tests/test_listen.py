"""Finite localhost-only integration tests for the listener."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parents[1] / 'listen.py')
TOKEN = 'fmcp_test_token'


def item(event):
    return {'kind': 'message', 'channel_id': '!room:test', 'is_backchannel': False,
            'messages': [{'event_id': event, 'body': 'private task', 'is_mention': True}],
            'reply_with': {'tool': 'post_message', 'args': {'channel': '!room:test', 'in_reply_to': event}}}


class Fake:
    def __init__(self):
        self.queue, self.calls = [], []
        self.lock = threading.Lock()
        self.stopped = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                args = body['params']['arguments']
                with fake.lock:
                    fake.calls.append({'auth': self.headers.get('Authorization'), 'args': args})
                    number = len(fake.calls)
                    step = fake.queue.pop(0) if fake.queue else {}
                if step.get('status'):
                    self.send_response(step['status'])
                    self.end_headers()
                    return
                if step.get('rpc_error'):
                    reply = {'jsonrpc': '2.0', 'id': 1,
                             'error': {'code': -32000, 'message': 'private server detail'}}
                else:
                    work = step.get('work', [])
                    if not work:
                        time.sleep(min(args.get('wait_seconds', 0), 0.15))
                    result = {'work': work, 'cursor': 'c%d' % number, 'next_poll_ms': 0,
                              'acknowledged': len(args.get('ack') or [])}
                    reply = {'jsonrpc': '2.0', 'id': 1,
                             'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}}
                data = json.dumps(reply)
                if step.get('sse'):
                    data = 'event: message\ndata: %s\n\n' % data
                raw = data.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if step.get('sse') else 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/mcp/agents' % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)


class ListenTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.fake = Fake()
        self.processes = []
        self.env = dict(os.environ, FM_STATE=str(self.state), FM_LOCK_WAIT='0')
        self.run_script('--token', '--endpoint', self.fake.url, stdin=TOKEN + '\n')

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.fake.stop()
        self.temporary.cleanup()

    def run_script(self, *args, stdin=None, env=None):
        result = subprocess.run([sys.executable, SCRIPT] + list(args), input=stdin,
                                capture_output=True, text=True, env=env or self.env, timeout=20)
        self.assertEqual(result.stderr, '', result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout.splitlines()

    def spawn(self, *args):
        process = subprocess.Popen([sys.executable, SCRIPT] + list(args), env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def wait_for_calls(self, count=1):
        deadline = time.monotonic() + 5
        while len(self.fake.calls) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.fake.calls), count)

    def state_data(self):
        return json.loads((self.state / 'state.json').read_text())

    def save_state(self, state):
        (self.state / 'state.json').write_text(json.dumps(state))

    def body(self, output):
        return json.loads('\n'.join(output[1:]))

    def work(self, *events):
        self.fake.queue = [{'work': [item(event) for event in events]}]
        output = self.run_script('--check', '1')
        self.assertEqual(output[0], 'WORK %d' % len(events))
        return self.body(output)

    def test_token_is_private(self):
        self.assertEqual((self.state / 'token').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / 'token').read_text(), TOKEN)

    def test_state_defaults_to_script_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / 'listen.py'
            copy.write_text(Path(SCRIPT).read_text())
            env = {key: value for key, value in os.environ.items() if key != 'FM_STATE'}
            result = subprocess.run([sys.executable, str(copy), '--token'], input=TOKEN + '\n',
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.stdout.strip(), 'TOKEN_SET')
            self.assertEqual((Path(directory) / 'token').read_text(), TOKEN)

    def test_rejects_non_token(self):
        self.assertEqual(self.run_script('--token', stdin='nope\n')[0], 'ERROR the token must start with fmcp_')
        self.assertEqual((self.state / 'token').read_text(), TOKEN)

    def test_empty_polls_to_work_persist_payload_and_cursor_together(self):
        self.fake.queue = [{}, {}, {'work': [item('e1')]}]
        output = self.run_script()
        batch = self.body(output)
        self.assertEqual(output[0], 'WORK 1')
        self.assertEqual(batch['work'][0]['messages'][0]['event_id'], 'e1')
        state = self.state_data()
        self.assertEqual(state['cursor'], 'c3')
        self.assertEqual(state['pending']['batch_id'], batch['batch_id'])
        self.assertEqual(state['pending']['work'], batch['work'])
        self.assertEqual(state['counts'], {'attempts': 3, 'empty': 2, 'work_batches': 1, 'work_items': 1, 'errors': 0})
        self.assertEqual(self.fake.calls[0]['auth'], 'Bearer ' + TOKEN)
        self.assertNotIn('cursor', self.fake.calls[0]['args'])
        self.assertEqual(self.fake.calls[1]['args']['cursor'], 'c1')
        self.assertEqual((self.state / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_young_pending_batch_is_left_to_its_handler(self):
        batch = self.work('e1')
        output = self.run_script()
        self.assertEqual(output[0], 'PENDING')
        self.assertEqual(self.body(output)['batch_id'], batch['batch_id'])
        self.assertEqual(len(self.fake.calls), 1)

    def test_abandoned_pending_batch_is_recovered_without_polling(self):
        batch = self.work('e1')
        state = self.state_data()
        state['pending']['created_at'] = time.time() - 3600
        self.save_state(state)
        output = self.run_script()
        recovered = self.body(output)
        self.assertEqual(output[0], 'RECOVER 1')
        self.assertEqual(recovered['work'], batch['work'])
        self.assertEqual(recovered['received_at'], state['pending']['created_at'])
        self.assertNotEqual(recovered['batch_id'], batch['batch_id'])
        self.assertEqual(len(self.fake.calls), 1)
        # The abandoned handler cannot complete the recovered batch.
        self.assertEqual(self.run_script('--done', batch['batch_id']), ['STALE'])
        self.assertEqual(self.state_data()['pending']['batch_id'], recovered['batch_id'])
        self.assertEqual(self.run_script('--done', recovered['batch_id'], '--budget', '1')[0], 'AGAIN')
        self.assertIsNone(self.state_data()['pending'])

    def test_recovery_timeout_is_configurable(self):
        self.work('e1')
        env = dict(self.env, FM_HANDLER_TIMEOUT='0')
        self.assertEqual(self.run_script(env=env)[0], 'RECOVER 1')

    def test_done_flushes_skipped_acks(self):
        batch = self.work('e1', 'e2')
        output = self.run_script('--done', batch['batch_id'], '--ack', 'e2', '--budget', '1')
        self.assertEqual(output[0], 'AGAIN')
        self.assertEqual(self.fake.calls[1]['args']['ack'], ['e2'])
        self.assertEqual(self.fake.calls[1]['args']['cursor'], 'c1')
        self.assertIsNone(self.state_data()['pending'])
        self.assertEqual(self.state_data()['acks'], [])

    def test_wrong_done_does_not_ack_or_poll(self):
        batch = self.work('e1')
        before = (self.state / 'state.json').read_bytes()
        output = self.run_script('--done', 'wrong', '--ack', 'e1')
        self.assertEqual(output, ['STALE'])
        self.assertEqual(before, (self.state / 'state.json').read_bytes())
        self.assertEqual(self.state_data()['pending']['batch_id'], batch['batch_id'])
        self.assertEqual(len(self.fake.calls), 1)

    def test_ack_outside_pending_batch_is_rejected(self):
        batch = self.work('e1')
        before = (self.state / 'state.json').read_bytes()
        self.assertEqual(self.run_script('--done', batch['batch_id'], '--ack', 'unrelated')[0], 'ERROR')
        self.assertEqual(before, (self.state / 'state.json').read_bytes())
        self.assertEqual(len(self.fake.calls), 1)

    def test_resume_returns_saved_work_without_polling(self):
        batch = self.work('e1')
        output = self.run_script('--resume', batch['batch_id'])
        self.assertEqual(output[0], 'WORK 1')
        self.assertEqual(self.body(output)['work'], batch['work'])
        self.assertNotEqual(self.body(output)['session_id'], batch['session_id'])
        self.assertNotEqual(self.body(output)['batch_id'], batch['batch_id'])
        self.assertEqual(len(self.fake.calls), 1)

    def test_prior_owner_cannot_complete_a_resumed_batch(self):
        batch = self.work('e1')
        resumed = self.body(self.run_script('--resume', batch['batch_id']))
        self.assertEqual(self.run_script('--done', batch['batch_id']), ['STALE'])
        self.assertEqual(self.state_data()['pending']['batch_id'], resumed['batch_id'])
        self.assertEqual(len(self.fake.calls), 1)

    def test_wrong_resume_preserves_pending_work(self):
        self.work('e1')
        before = (self.state / 'state.json').read_bytes()
        self.assertEqual(self.run_script('--resume', 'wrong')[0], 'ERROR')
        self.assertEqual(before, (self.state / 'state.json').read_bytes())
        self.assertEqual(len(self.fake.calls), 1)

    def test_old_completion_cannot_clear_a_new_batch(self):
        first = self.work('e1')
        self.fake.queue = [{'work': [item('e2')]}]
        second = self.body(self.run_script('--done', first['batch_id']))
        self.assertEqual(self.run_script('--done', first['batch_id']), ['STALE'])
        self.assertEqual(self.state_data()['pending']['batch_id'], second['batch_id'])
        self.assertEqual(len(self.fake.calls), 2)

    def test_scheduled_start_is_a_short_check(self):
        started = time.monotonic()
        self.assertEqual(self.run_script('--check', '1'), ['END'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertGreaterEqual(len(self.fake.calls), 1)
        self.assertIsNone(self.state_data()['session'])

    def test_handled_work_opens_follow_up_window(self):
        self.fake.queue = [{'work': [item('e1')]}]
        batch = self.body(self.run_script('--check', '1'))
        self.fake.queue = [{}, {'work': [item('e2')]}]
        output = self.run_script('--done', batch['batch_id'], '--follow', '30')
        self.assertEqual(output[0], 'WORK 1')
        self.assertEqual(self.body(output)['work'][0]['messages'][0]['event_id'], 'e2')
        self.assertEqual(self.body(output)['session_id'], batch['session_id'])

    def test_follow_up_window_ends_the_session(self):
        batch = self.work('e1')
        started = time.monotonic()
        self.assertEqual(self.run_script('--done', batch['batch_id'], '--follow', '1'), ['END'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(self.state_data()['session'])

    def test_follow_up_cannot_outlast_session_limit(self):
        batch = self.work('e1')
        state = self.state_data()
        state['session']['started_at'] = time.time() - 1799
        self.save_state(state)
        started = time.monotonic()
        self.assertEqual(self.run_script('--done', batch['batch_id'], '--follow', '600'), ['END'])
        self.assertLess(time.monotonic() - started, 5)

    def test_previous_revision_session_is_accepted(self):
        batch = self.work('e1')
        state = self.state_data()
        del state['session']['listen_until']
        self.save_state(state)
        self.assertEqual(self.run_script('--done', batch['batch_id'], '--follow', '1'), ['END'])

    def test_done_waits_for_a_brief_lock(self):
        batch = self.work('e1')
        import fcntl
        fd = os.open(self.state / 'listener.lock', os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        timer = threading.Timer(0.5, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        timer.start()
        try:
            output = self.run_script('--done', batch['batch_id'], '--follow', '1', env=dict(self.env, FM_LOCK_WAIT='5'))
        finally:
            timer.join()
        self.assertEqual(output, ['END'])
        self.assertIsNone(self.state_data()['pending'])

    def test_session_end(self):
        self.assertEqual(self.run_script('--budget', '30', '--session', '1'), ['END'])
        self.assertIsNone(self.state_data()['session'])

    def test_again_preserves_session_until_end(self):
        output = self.run_script('--budget', '1', '--session', '2')
        self.assertEqual(output[0], 'AGAIN')
        session = self.body(output)['session_id']
        self.assertEqual(self.run_script('--again', session, '--budget', '30', '--session', '2'), ['END'])

    def test_stale_again_cannot_join_a_new_session(self):
        first = self.body(self.run_script('--budget', '1'))['session_id']
        second = self.body(self.run_script('--budget', '1'))['session_id']
        count = len(self.fake.calls)
        self.assertNotEqual(first, second)
        self.assertEqual(self.run_script('--again', first), ['END'])
        self.assertEqual(len(self.fake.calls), count)

    def test_done_after_session_end_flushes_ack(self):
        batch = self.work('e1')
        state = self.state_data()
        state['session']['started_at'] = time.time() - 1800
        self.save_state(state)
        self.assertEqual(self.run_script('--done', batch['batch_id'], '--ack', 'e1'), ['END'])
        self.assertEqual(self.fake.calls[-1]['args']['wait_seconds'], 0)
        self.assertEqual(self.fake.calls[-1]['args']['ack'], ['e1'])

    def test_ack_flush_new_work_is_persisted_even_after_session_end(self):
        batch = self.work('e1')
        state = self.state_data()
        state['session']['started_at'] = time.time() - 1800
        self.save_state(state)
        self.fake.queue = [{'work': [item('e2')]}]
        output = self.run_script('--done', batch['batch_id'], '--ack', 'e1')
        self.assertEqual(output[0], 'WORK 1')
        self.assertEqual(self.body(output)['work'][0]['messages'][0]['event_id'], 'e2')
        self.assertEqual(self.run_script()[0], 'PENDING')

    def test_live_lock_not_reclaimed_due_to_old_mtime(self):
        process = self.spawn('--budget', '3')
        self.wait_for_calls()
        os.utime(self.state / 'listener.lock', (1, 1))
        inode = (self.state / 'listener.lock').stat().st_ino
        self.assertEqual(self.run_script(), ['RUNNING'])
        self.assertEqual((self.state / 'listener.lock').stat().st_ino, inode)
        self.assertEqual(process.communicate(timeout=10)[0].splitlines()[0], 'AGAIN')

    def test_lock_released_when_process_is_killed(self):
        process = self.spawn('--budget', '10')
        self.wait_for_calls()
        process.kill()
        process.communicate(timeout=10)
        self.fake.queue = [{'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')

    def test_concurrent_starts_deliver_only_one_batch(self):
        self.fake.queue = [{'work': [item('e1')]}]
        first, second = self.spawn('--budget', '2'), self.spawn('--budget', '2')
        outputs = [first.communicate(timeout=10)[0].splitlines()[0], second.communicate(timeout=10)[0].splitlines()[0]]
        self.assertEqual(outputs.count('WORK 1'), 1)
        self.assertTrue('RUNNING' in outputs or 'PENDING' in outputs)
        self.assertEqual(len(self.fake.calls), 1)

    def test_backoff_does_not_release_lock(self):
        self.fake.queue = [{'rpc_error': True}] * 10
        process = self.spawn('--budget', '4')
        self.wait_for_calls(2)
        self.assertEqual(self.run_script(), ['RUNNING'])
        process.communicate(timeout=10)

    def test_auth_failed_is_remembered_without_more_requests(self):
        self.fake.queue = [{'status': 401}]
        self.assertEqual(self.run_script(), ['AUTH_FAILED', 'HTTP 401'])
        self.assertEqual(self.run_script()[0], 'AUTH_FAILED')
        self.assertEqual(len(self.fake.calls), 1)

    def test_rpc_error_keeps_polling_without_logging_server_body(self):
        self.fake.queue = [{'rpc_error': True}, {'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')
        self.assertEqual(self.state_data()['counts']['errors'], 1)
        self.assertIsNone(self.state_data()['failed_since'])
        self.assertNotIn('private server detail', (self.state / 'listen.jsonl').read_text())

    def test_sse_body(self):
        self.fake.queue = [{'sse': True, 'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')

    def test_unreachable_then_disconnected_once(self):
        self.fake.stop()
        env = dict(self.env, FM_DISCONNECT_AFTER='2')
        first = self.run_script('--budget', '1', env=env)
        self.assertEqual(first[0], 'AGAIN')
        time.sleep(1.1)
        self.assertEqual(self.run_script('--again', self.body(first)['session_id'], '--budget', '2', env=env)[0], 'DISCONNECTED')
        self.assertEqual(self.run_script('--budget', '1', '--session', '1', env=env), ['END'])

    def test_status_is_read_only_and_redacts_payload(self):
        self.work('e1')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.state.iterdir()}
        calls = len(self.fake.calls)
        output = '\n'.join(self.run_script('--status'))
        result = json.loads(output)
        self.assertEqual(result['pending']['items'], 1)
        self.assertEqual(result['poll_counts']['attempts'], 1)
        self.assertIsNone(result['model_tokens'])
        self.assertNotIn(TOKEN, output)
        self.assertNotIn('private task', output)
        self.assertNotIn('!room:test', output)
        self.assertEqual(calls, len(self.fake.calls))
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.state.iterdir()})

    def test_status_does_not_create_a_missing_state_directory(self):
        missing = self.state / 'missing'
        result = json.loads('\n'.join(self.run_script('--status', env=dict(self.env, FM_STATE=str(missing)))))
        self.assertFalse(result['lock_held'])
        self.assertFalse(missing.exists())
        self.assertEqual(self.fake.calls, [])

    def test_status_observes_live_lock_without_polling(self):
        process = self.spawn('--budget', '2')
        self.wait_for_calls()
        result = json.loads('\n'.join(self.run_script('--status')))
        self.assertTrue(result['lock_held'])
        process.communicate(timeout=10)

    def test_call_metrics_count_empty_polls_separately_from_calls(self):
        self.fake.queue = [{}, {}, {'work': [item('e1')]}]
        self.run_script()
        records = [json.loads(line) for line in (self.state / 'listen.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['CALL_START', 'CALL_END'])
        self.assertEqual(records[1]['empty_polls'], 2)
        self.assertEqual(records[1]['poll_attempts'], 3)
        self.assertEqual(records[1]['work_items'], 1)
        self.assertEqual(records[0]['call_id'], records[1]['call_id'])
        self.assertEqual((self.state / 'listen.jsonl').stat().st_mode & 0o777, 0o600)

    def test_legacy_pending_never_expires_implicitly(self):
        (self.state / 'handling').write_text('1')
        os.utime(self.state / 'handling', (1, 1))
        self.assertEqual(self.run_script()[0], 'ERROR')
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.state / 'state.json').exists())

    def test_live_legacy_listener_prevents_upgrade_start(self):
        directory = self.state / 'listen.lock'
        directory.mkdir()
        (directory / 'pid').write_text(str(os.getpid()))
        (directory / 'beat').write_text('1')
        self.assertEqual(self.run_script(), ['RUNNING'])
        self.assertEqual(self.fake.calls, [])

    def test_legacy_cursor_and_acks_are_imported(self):
        (self.state / 'cursor').write_text('legacy-cursor')
        (self.state / 'acks').write_text('["old-event"]')
        self.fake.queue = [{'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')
        self.assertEqual(self.fake.calls[0]['args']['cursor'], 'legacy-cursor')
        self.assertEqual(self.fake.calls[0]['args']['ack'], ['old-event'])

    def test_corrupt_state_does_not_reset_cursor_or_poll(self):
        (self.state / 'state.json').write_text('{broken')
        self.assertEqual(self.run_script()[0], 'ERROR')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.state / 'state.json').read_text(), '{broken')

    def test_incomplete_or_invalid_state_fails_closed_including_status(self):
        self.work('e1')
        complete = self.state_data()
        cases = [
            {'format': 2, 'acks': [], 'counts': {}, 'pending': None},
            dict(complete, session=None),
            dict(complete, last_poll_at='yesterday'),
            dict(complete, counts={'attempts': -1}),
        ]
        for state in cases:
            with self.subTest(state=state):
                self.save_state(state)
                before = (self.state / 'state.json').read_bytes()
                self.assertEqual(self.run_script()[0], 'ERROR')
                self.assertIsNotNone(json.loads('\n'.join(self.run_script('--status')))['state_error'])
                self.assertEqual(before, (self.state / 'state.json').read_bytes())
                self.assertEqual(len(self.fake.calls), 1)

    def test_invalid_budget_rejected_before_network(self):
        result = subprocess.run([sys.executable, SCRIPT, '--budget', '780'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fake.calls, [])


class RevisionTest(unittest.TestCase):
    def test_revision_matches_guide_version(self):
        guide = (Path(SCRIPT).parent / 'GROKBOT.md').read_text()
        version = re.search(r'^version: "([^"]+)"$', guide, re.M).group(1)
        revision = re.search(r"^REVISION = '([^']+)'$", Path(SCRIPT).read_text(), re.M).group(1)
        self.assertEqual(revision, version)


if __name__ == '__main__':
    unittest.main(verbosity=2)
