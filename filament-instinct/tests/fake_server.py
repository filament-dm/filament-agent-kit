"""Thread-per-connection MCP fixture: canned faults plus a cursor/read queue.

Scenarios are deques keyed by tool (or heartbeat/media). A step supports delay,
status, error, is_error, result, raw, sse, disconnect and apply (apply the queue
operation before responding/disconnecting). Requests, without credentials, are
recorded to calls.jsonl. All state is protected by a condition/RLock.
"""
from collections import defaultdict, deque
import copy
import hashlib
import io
import os
import tempfile
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
import urllib.parse


def message(event='e1', **changes):
    row = {'event_id': event, 'sender': '@person:test', 'body': 'Hello',
           'timestamp': int(time.time() * 1000), 'media': [], 'is_from_self': False,
           'is_from_principal': False, 'is_from_agent': False, 'is_system': False,
           'is_mention': True}
    row.update(changes)
    return row


def item(event='e1', **changes):
    row = {'kind': 'message', 'channel_id': '!room:test', 'thread_id': None,
           'is_backchannel': False, 'messages': [message(event)],
           'reply_with': {'tool': 'post_message', 'args': {'channel': '!room:test', 'in_reply_to': event}}}
    row.update(changes)
    return row


class FakeServer:
    def __init__(self, state, queue=True):
        self.state = Path(state)
        self.queue = queue
        self.scenarios = defaultdict(deque)
        self.condition = threading.Condition(threading.RLock())
        self.calls = []
        self.deliveries = []
        self.saved_position = 0
        self.acknowledged = set()
        self.answered = set()
        self.posted = []
        self.context = []
        self.active_polls = 0
        self.max_active_polls = 0
        self.assets = {}
        self.settings = {'test': True}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path.startswith('/assets/'):
                    name = urllib.parse.unquote(self.path.split('/assets/', 1)[1])
                    data = outer.assets.get(name)
                    self.send_response(200 if data is not None else 404)
                    self.end_headers()
                    if data is not None:
                        self.wfile.write(data)
                    return
                self.handle_call('media', dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query)))

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path.endswith('/heartbeat'):
                    self.handle_call('heartbeat', {})
                    return
                request = json.loads(data)
                self.handle_call(request['params']['name'], request['params']['arguments'])

            def handle_call(self, tool, args):
                with outer.condition:
                    entry = {'tool': tool, 'args': copy.deepcopy(args), 'at': time.time(),
                             'token_hash': hashlib.sha256(self.headers.get('Authorization', '').encode()).hexdigest()}
                    outer.calls.append(entry)
                    if outer.state.exists():
                        with (outer.state / 'calls.jsonl').open('a') as f:
                            f.write(json.dumps(entry) + '\n')
                    step = outer.scenarios[tool].popleft() if outer.scenarios[tool] else {}
                    if tool == 'poll_work':
                        outer.active_polls += 1
                        outer.max_active_polls = max(outer.max_active_polls, outer.active_polls)
                    outer.condition.notify_all()
                try:
                    if step.get('delay'):
                        time.sleep(step['delay'])
                    result = None
                    if step.get('apply') or not any(k in step for k in ('result', 'error', 'raw', 'is_error', 'status', 'disconnect')):
                        result = outer.model(tool, args)
                    if step.get('disconnect'):
                        with context_suppress():
                            self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if tool == 'media':
                        self.send_response(step.get('status', 200))
                        self.send_header('Content-Type', step.get('content_type', 'image/png'))
                        if 'length' in step:
                            self.send_header('Content-Length', str(step['length']))
                        self.end_headers()
                        self.wfile.write(step.get('bytes', b'\x89PNG\r\nfixture'))
                        return
                    if tool == 'heartbeat':
                        self.send_response(step.get('status', 200))
                        self.end_headers()
                        return
                    if 'error' in step:
                        response = {'jsonrpc': '2.0', 'id': 1, 'error': step['error']}
                    elif isinstance(result, dict) and '_error' in result:
                        response = {'jsonrpc': '2.0', 'id': 1, 'error': result['_error']}
                    else:
                        response = {'jsonrpc': '2.0', 'id': 1, 'result': {
                            'content': [{'type': 'text', 'text': json.dumps(step.get('result', result))}],
                            'isError': step.get('is_error', False)}}
                    data = step.get('raw', json.dumps(response)).encode()
                    if step.get('sse'):
                        data = b': ping\n\nevent: message\ndata: ' + data + b'\n\ndata: [DONE]\n\n'
                    self.send_response(step.get('status', 200))
                    self.send_header('Content-Type', 'text/event-stream' if step.get('sse') else 'application/json')
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with outer.condition:
                        entry['end'] = time.time()
                        if tool == 'poll_work':
                            outer.active_polls -= 1
                        outer.condition.notify_all()

        self.handler = Handler
        self.transport_env = {}
        try:
            self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError:
            self._offline_start()
            return
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = 'http://127.0.0.1:{}/mcp/agents'.format(self.http.server_port)
        self.base = 'http://127.0.0.1:{}'.format(self.http.server_port)

    def _offline_start(self):
        """No sockets: run the SAME HTTP handler over bytes via fixture files.

        A test-only urllib handler is loaded through sitecustomize in children.
        It changes no connector source. Outside restricted sandboxes TCP is used.
        """
        self.offline = tempfile.TemporaryDirectory(prefix='fm-http-')
        folder = Path(self.offline.name)
        (folder / 'sitecustomize.py').write_text(OFFLINE_CLIENT)
        self.transport_env = {'PYTHONPATH': str(folder), 'FM_FIXTURE_HTTP': str(folder)}
        self.endpoint = 'http://fixture.invalid/mcp/agents'
        self.base = 'http://fixture.invalid'
        self.halt = threading.Event()
        self.workers = []
        def dispatch():
            while not self.halt.is_set():
                for path in folder.glob('*.request'):
                    busy = path.with_suffix('.busy')
                    try:
                        path.rename(busy)
                    except FileNotFoundError:
                        continue
                    worker = threading.Thread(target=self._offline_request, args=(busy,), daemon=True)
                    self.workers.append(worker)
                    worker.start()
                self.halt.wait(0.005)
        self.thread = threading.Thread(target=dispatch, daemon=True)
        self.thread.start()

    def _offline_request(self, path):
        request = json.loads(path.read_text())
        import base64
        data = base64.b64decode(request['body'])
        target = urllib.parse.urlsplit(request['url'])
        raw = '{} {} HTTP/1.0\r\n'.format(request['method'], target.path + ('?' + target.query if target.query else ''))
        headers = dict(request['headers'])
        headers['Content-Length'] = str(len(data))
        raw += ''.join('{}: {}\r\n'.format(k, v) for k, v in headers.items()) + '\r\n'
        class Wire:
            def __init__(self):
                self.output = bytearray()
                self.disconnected = False
            def makefile(self, *_):
                return io.BytesIO(raw.encode() + data)
            def sendall(self, chunk):
                self.output.extend(chunk)
            def shutdown(self, *_):
                self.disconnected = True
            def close(self):
                pass
        wire = Wire()
        self.handler(wire, ('offline', 0), types.SimpleNamespace())
        response = {'disconnect': wire.disconnected,
                    'bytes': base64.b64encode(wire.output).decode()}
        tmp = path.with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps(response))
            tmp.rename(path.with_suffix('.response'))
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass  # Test terminated a client and removed its transport fixture.

    def close(self):
        if hasattr(self, 'offline'):
            self.halt.set()
            self.thread.join()
            # Wake long polls before removing transport files.
            with self.condition:
                self.deliveries.append(item('teardown'))
                self.condition.notify_all()
            for worker in self.workers:
                worker.join(timeout=1)
            self.offline.cleanup()
        else:
            self.http.shutdown()
            self.http.server_close()
            self.thread.join()

    def add(self, *items):
        with self.condition:
            self.deliveries.extend(copy.deepcopy(items))
            self.condition.notify_all()

    def plan(self, tool, *steps):
        with self.condition:
            self.scenarios[tool].extend(steps)

    def tool_calls(self, tool):
        with self.condition:
            return [copy.deepcopy(c) for c in self.calls if c['tool'] == tool]

    def wait_calls(self, tool, count=1, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while len([c for c in self.calls if c['tool'] == tool]) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError('Timed out waiting for {} call {}'.format(tool, count))
                self.condition.wait(remaining)

    @property
    def unacknowledged(self):
        with self.condition:
            return {m['event_id'] for i in self.deliveries for m in i.get('messages', [])} - self.acknowledged

    def model(self, tool, args):
        with self.condition:
            if tool == 'get_self':
                return {'user_id': '@agent:test', 'owner_id': '@person:test', 'display_name': 'Fixture'}
            if tool == 'list_pending_invites':
                return {'invites': []}
            if tool == 'list_vouches':
                return {'vouches': []}
            if tool == 'get_recent_messages':
                return {'messages': copy.deepcopy((self.context + self.posted)[-args.get('limit', 40):])}
            if tool == 'get_thread':
                return {'messages': copy.deepcopy(self.context)}
            if tool in ('post_message', 'reply_in_thread', 'message_principal'):
                parent = args.get('in_reply_to') or args.get('message_id')
                if parent in self.answered:
                    return {'_error': {'code': -32602, 'message': 'already answered'}}
                if parent:
                    self.answered.add(parent)
                    self.acknowledged.add(parent)
                row = message('posted-{}'.format(len(self.posted) + 1), body=args['markdown_body'],
                              is_from_self=True, is_mention=False)
                self.posted.append(row)
                return {'event_id': row['event_id']}
            if tool != 'poll_work':
                return {}
            self.acknowledged.update(args.get('ack', []))
            position = int(args.get('cursor', self.saved_position))
            if args.get('max_items') != 0 and 'replay_from' not in args:
                for it in self.deliveries[:position]:
                    self.acknowledged.update(m['event_id'] for m in it.get('messages', []))
            if 'replay_from' in args:
                position = int(args['replay_from'])
            if args.get('max_items') == 0:
                return {'work': [], 'cursor': str(self.saved_position), 'acknowledged': args.get('ack', []),
                        'next_poll_ms': 0, 'server_time': int(time.time() * 1000), 'truncated': False}
            def available():
                return [(n, i) for n, i in enumerate(self.deliveries[position:], position + 1)
                        if i.get('kind') != 'message' or any(m['event_id'] not in self.acknowledged for m in i.get('messages', []))]
            rows = available()
            if not rows:
                self.condition.wait_for(lambda: bool(available()), timeout=args.get('wait_seconds', 0))
                rows = available()
            selected = rows[:args.get('max_items', 10)]
            end = selected[-1][0] if selected else max(position, len(self.deliveries))
            self.saved_position = end
            for _, it in selected:
                if it.get('kind') == 'message' and it.get('reply_with') is None:
                    self.acknowledged.update(m['event_id'] for m in it.get('messages', []))
            return {'work': [copy.deepcopy(i) for _, i in selected], 'cursor': str(end),
                    'truncated': len(rows) > len(selected), 'next_poll_ms': 1000,
                    'acknowledged': args.get('ack', []), 'server_time': int(time.time() * 1000),
                    'settings': self.settings}


# Socket teardown should never conceal errors elsewhere in the fake.
class context_suppress:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, OSError)


OFFLINE_CLIENT = r'''# Test-only transport. Never included in the installed package.
import base64
import email.parser
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import urllib.response
import uuid

class FixtureHTTP(urllib.request.BaseHandler):
    handler_order = 100
    def http_open(self, req):
        if req.host != 'fixture.invalid':
            raise urllib.error.URLError('offline fixture refuses external hosts')
        folder = Path(os.environ['FM_FIXTURE_HTTP'])
        stem = folder / uuid.uuid4().hex
        data = {'url': req.full_url, 'method': req.get_method(),
                'headers': dict(req.header_items()),
                'body': base64.b64encode(req.data or b'').decode()}
        temporary = stem.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        temporary.rename(stem.with_suffix('.request'))
        response = stem.with_suffix('.response')
        deadline = time.monotonic() + (req.timeout or 30)
        while not response.exists():
            if time.monotonic() >= deadline:
                raise urllib.error.URLError('fixture timeout')
            time.sleep(0.005)
        result = json.loads(response.read_text())
        response.unlink()
        if result['disconnect']:
            raise urllib.error.URLError('fixture disconnected')
        wire = base64.b64decode(result['bytes'])
        head, body = wire.split(b'\r\n\r\n', 1)
        first, rest = head.split(b'\r\n', 1)
        code = int(first.split()[1])
        headers = email.parser.Parser().parsestr(rest.decode() + '\r\n')
        reply = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, code)
        reply.msg = first.decode()
        return reply
urllib.request.install_opener(urllib.request.build_opener(FixtureHTTP()))
'''
