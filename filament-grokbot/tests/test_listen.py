"""Offline tests for listen.py against a fake MCP server.

Run: python3 tests/test_listen.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'listen.py')
TOKEN = 'fmcp_test_token'


def item(event):
    return {'kind': 'message', 'channel_id': '!room:test', 'is_backchannel': False,
            'messages': [{'event_id': event, 'body': 'hi', 'is_mention': True}],
            'reply_with': {'tool': 'post_message', 'args': {'channel': '!room:test', 'in_reply_to': event}}}


class Fake:
    """Answers poll_work from a queue; an empty queue means empty work."""

    def __init__(self):
        self.queue = []
        self.calls = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                args = body['params']['arguments']
                with fake.lock:
                    fake.calls.append({'auth': self.headers.get('Authorization'), 'args': args})
                    step = fake.queue.pop(0) if fake.queue else {}
                if step.get('status'):
                    self.send_response(step['status'])
                    self.end_headers()
                    return
                if step.get('rpc_error'):
                    reply = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': 'paused'}}
                else:
                    work = step.get('work', [])
                    if not work:
                        time.sleep(min(args.get('wait_seconds', 0), 0.3))
                    result = {'work': work, 'cursor': 'c%d' % len(fake.calls), 'next_poll_ms': 0,
                              'acknowledged': len(args.get('ack') or [])}
                    reply = {'jsonrpc': '2.0', 'id': 1,
                             'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}}
                data = json.dumps(reply)
                if step.get('sse'):
                    data = 'event: message\ndata: %s\n\n' % data
                    ctype = 'text/event-stream'
                else:
                    ctype = 'application/json'
                raw = data.encode()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/mcp/agents' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ListenTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.fake = Fake()
        self.env = dict(os.environ, FM_STATE=self.state)
        self.run_script('--token', '--endpoint', self.fake.url, stdin=TOKEN + '\n')

    def tearDown(self):
        self.fake.stop()

    def run_script(self, *args, stdin=None, env=None):
        out = subprocess.run([sys.executable, SCRIPT] + list(args), input=stdin,
                             capture_output=True, text=True, env=env or self.env, timeout=60)
        self.assertEqual(out.stderr, '', out.stderr)
        return out.stdout.splitlines()

    def state_file(self, name):
        p = os.path.join(self.state, name)
        return open(p).read() if os.path.exists(p) else None

    def test_token_is_private(self):
        self.assertEqual(oct(os.stat(os.path.join(self.state, 'token')).st_mode & 0o777), '0o600')
        self.assertEqual(self.state_file('token'), TOKEN)

    def test_rejects_non_token(self):
        self.assertEqual(self.run_script('--token', stdin='nope\n')[0], 'ERROR the token must start with fmcp_')

    def test_returns_work_after_empty_polls(self):
        self.fake.queue = [{}, {}, {'work': [item('e1')]}]
        out = self.run_script('--budget', '30')
        self.assertEqual(out[0], 'WORK 1')
        self.assertEqual(json.loads('\n'.join(out[1:]))[0]['messages'][0]['event_id'], 'e1')
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(self.fake.calls[0]['auth'], 'Bearer ' + TOKEN)
        self.assertNotIn('cursor', self.fake.calls[0]['args'])
        self.assertEqual(self.fake.calls[1]['args']['cursor'], 'c1')
        self.assertEqual(self.state_file('cursor'), 'c3')
        self.assertIsNotNone(self.state_file('handling'))

    def test_busy_while_handling_then_done_sends_acks(self):
        self.fake.queue = [{'work': [item('e1'), item('e2')]}]
        self.assertEqual(self.run_script()[0], 'WORK 2')
        # A routine tick while the turn is still replying must not poll:
        # a new poll_work would acknowledge the items being answered.
        self.assertEqual(self.run_script()[0], 'BUSY')
        self.assertEqual(len(self.fake.calls), 1)
        out = self.run_script('--done', '--ack', 'e2', '--budget', '1')
        self.assertEqual(out[0], 'AGAIN')
        self.assertEqual(self.fake.calls[1]['args']['ack'], ['e2'])
        self.assertEqual(self.fake.calls[1]['args']['cursor'], 'c1')
        self.assertIsNone(self.state_file('acks'))
        self.assertIsNone(self.state_file('handling'))

    def test_session_end(self):
        out = self.run_script('--budget', '30', '--session', '1')
        self.assertEqual(out, ['END'])
        self.assertIsNone(self.state_file('session'))

    def test_again_keeps_session_until_it_ends(self):
        self.assertEqual(self.run_script('--budget', '1', '--session', '3'), ['AGAIN'])
        session = self.state_file('session')
        self.assertIsNotNone(session)
        self.assertEqual(self.run_script('--again', '--budget', '30', '--session', '3'), ['END'])

    def test_done_after_session_end_still_flushes_acks(self):
        self.fake.queue = [{'work': [item('e1')]}]
        self.assertEqual(self.run_script('--session', '1')[0], 'WORK 1')
        time.sleep(1.2)
        out = self.run_script('--done', '--ack', 'e1', '--session', '1')
        self.assertEqual(out, ['END'])
        self.assertEqual(self.fake.calls[-1]['args']['ack'], ['e1'])
        self.assertEqual(self.fake.calls[-1]['args']['wait_seconds'], 0)

    def test_second_listener_sees_running(self):
        first = subprocess.Popen([sys.executable, SCRIPT, '--budget', '3'], env=self.env,
                                 stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        self.assertEqual(self.run_script(), ['RUNNING'])
        self.assertEqual(first.communicate(timeout=30)[0].splitlines(), ['AGAIN'])

    def test_stale_lock_is_taken_over(self):
        os.mkdir(os.path.join(self.state, 'listen.lock'))
        with open(os.path.join(self.state, 'listen.lock', 'pid'), 'w') as f:
            f.write('999999')
        self.fake.queue = [{'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')

    def test_auth_failed(self):
        self.fake.queue = [{'status': 401}]
        self.assertEqual(self.run_script(), ['AUTH_FAILED', 'HTTP 401'])

    def test_rpc_error_keeps_polling(self):
        self.fake.queue = [{'rpc_error': True}, {'work': [item('e1')]}]
        self.assertEqual(self.run_script('--budget', '30')[0], 'WORK 1')
        self.assertIsNone(self.state_file('fail_since'))

    def test_sse_body(self):
        self.fake.queue = [{'sse': True, 'work': [item('e1')]}]
        self.assertEqual(self.run_script()[0], 'WORK 1')

    def test_unreachable_then_disconnected_once(self):
        self.fake.stop()
        env = dict(self.env, FM_DISCONNECT_AFTER='2')
        self.assertEqual(self.run_script('--budget', '1', env=env), ['AGAIN'])
        time.sleep(2)
        self.assertEqual(self.run_script('--again', '--budget', '5', env=env)[0], 'DISCONNECTED')
        # Told once: later runs just end quietly while it stays down.
        self.assertEqual(self.run_script('--budget', '1', '--session', '1', env=env), ['END'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
