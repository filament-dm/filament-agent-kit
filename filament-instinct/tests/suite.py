#!/usr/bin/env python3
"""Black-box, real-process tests. Only the HTTP peer is fake."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fake_server import FakeServer, item, message

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


class Connector(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fm-test-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir()
        for p in SCRIPTS.iterdir():
            if p.is_file():
                shutil.copy2(p, self.state / p.name)
        self.write('token', 'fixture-credential')
        self.server = FakeServer(self.state)
        self.env = dict(os.environ, FM_STATE=str(self.state), FM_ENDPOINT=self.server.endpoint,
                        FM_TEST_FAST='1', FM_BUDGET='60', PYTHONDONTWRITEBYTECODE='1')
        self.env.update(self.server.transport_env)
        for key in ('FM_HANDOFF', 'FM_HB_PREDECESSOR'):
            self.env.pop(key, None)
        self.children = []
        self.detached = set()

    def tearDown(self):
        # Discover all keeper/successor pids from the test's own state only.
        self.collect_pids()
        for p in self.children:
            if p.poll() is None:
                p.terminate()
        for pid in self.detached:
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in self.children:
            try:
                p.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
        time.sleep(0.12)
        for pid in self.detached:
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.server.close()
        self.temp.cleanup()

    def collect_pids(self):
        for pattern in (r'listener start pid=(\d+)', r'hb keeper start pid=(\d+)'):
            self.detached.update(int(x) for x in re.findall(pattern, self.read('listener.log')))
        for name in ('lock.d/owner', 'hb.pid'):
            text = self.read(name).split()
            if text:
                self.detached.add(int(text[0]))

    def write(self, name, value):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def read(self, name):
        try:
            return (self.state / name).read_text().strip()
        except FileNotFoundError:
            return ''

    def data(self, name='last_work.json'):
        return json.loads(self.read(name))

    def run_cmd(self, *args, body=None, timeout=15, env=None):
        cmd = ['/usr/bin/env', 'bash', str(self.state / 'poll.sh')] if args and args[0] == 'sh' else [sys.executable, '-B', str(self.state / args[0])]
        result = subprocess.run(cmd + list(args[1:]), input=body, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env or self.env, timeout=timeout)
        result.out = result.stdout.decode()
        self.assertEqual(result.stderr, b'', result.stderr.decode())
        return result

    def start(self, script='fm_poll.py', env=None, args=(), body=None):
        p = subprocess.Popen([sys.executable, '-B', str(self.state / script)] + list(args),
                             env=env or self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        if body is not None:
            p.stdin.write(body)
            p.stdin.close()
            p.stdin = None
        self.children.append(p)
        return p

    def finish(self, p, timeout=12):
        out, err = p.communicate(timeout=timeout)
        self.assertEqual(err, b'', err.decode())
        return out.decode()

    def wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('Timed out waiting for state; log=' + self.read('listener.log')[-3000:])

    def deliver(self, *items, claim=True):
        self.write('cursor', '0')
        self.server.add(*(items or (item(),)))
        r = self.run_cmd('fm_poll.py')
        self.assertEqual((r.returncode, r.out), (0, 'run complete: WORK\n'))
        if claim:
            self.assertIn('ANSWER n=1', self.run_cmd('sh').out)
        return self.data()

    def dead(self, kind='auth', credential='fixture-credential'):
        self.write('token_dead', json.dumps({'kind': kind, 'sha256': hashlib.sha256(credential.encode()).hexdigest(), 'error': 'test', 'at': 'test'}))

    def test_01_handoff_twenty_seconds_real_budget(self):
        p = self.start(env=dict(self.env, FM_BUDGET='20'))
        out = self.finish(p, timeout=27)
        self.assertIn('run complete: HANDOFF', out)
        self.wait(lambda: len(re.findall('listener start pid=', self.read('listener.log'))) >= 2)
        self.wait(lambda: len(self.server.tool_calls('poll_work')) >= 11)
        log = self.read('listener.log')
        stamp = float(re.search(r'handoff spawn t=([\d.]+)', log)[1])
        successor = int(self.read('lock.d/owner').split()[0])
        self.assertNotEqual(successor, p.pid)
        first = float(re.search(r'poll_work call pid={} t=([\d.]+)'.format(successor), log)[1])
        self.assertLess(first - stamp, 2)
        self.assertFalse((self.state / 'lock.d/handoff').exists())
        self.assertEqual(self.server.max_active_polls, 1)
        # No test shortcut changed the successor's budget or handoff token rules.
        self.assertGreaterEqual(stamp - self.server.calls[0]['at'], 20)

    def test_02_second_poll_atomic_delivery_queue_state(self):
        self.write('cursor', '0')
        self.server.context = [message('new', timestamp=3, body='x' * 2200), message('old', timestamp=1)]
        p = self.start()
        self.server.wait_calls('poll_work', 2)
        original = item()
        self.server.add(original)
        self.assertIn('run complete: WORK', self.finish(p))
        batch = self.data()
        for key, value in original.items():
            self.assertEqual(batch['items'][0][key], value)
        self.assertEqual([m['event_id'] for m in batch['items'][0]['context']], ['old', 'new'])
        self.assertEqual(len(batch['items'][0]['context'][1]['body']), 2000)
        self.assertEqual(batch['response']['work'], [original])
        self.assertEqual(batch['items'][0]['n'], 1)
        self.assertEqual(self.read('cursor'), '0')
        self.assertEqual(self.read('pending_cursor'), '1')
        self.assertTrue((self.state / 'work.ready').exists())
        self.assertFalse((self.state / 'lock.d').exists())
        self.assertTrue(self.read('run.out').startswith('BATCH ' + batch['batch_id']))
        self.assertEqual(self.server.unacknowledged, {'e1'})
        self.wait(lambda: bool(self.read('hb.pid')))

    def test_03_classification_and_next_poll_ack_snapshot(self):
        rows = [item('self', messages=[message('self', is_from_self=True)]),
                item('system', messages=[message('system', is_system=True)]),
                item('agent', messages=[message('agent', is_from_agent=True, is_mention=False)]),
                item('implicit', messages=[message('implicit', is_from_agent=True, is_mention=False,
                                                  is_implicitly_mentioned=True, reply_expected=True)]),
                item('null', reply_with=None), item('back', is_backchannel=True,
                        messages=[message('back', is_mention=False)]), item('mention'),
                {'kind': 'reaction'}, {'kind': 'invite', 'loop_id': 'loop1',
                    'reply_with': {'tool': 'accept_invite', 'args': {'loop_id': 'loop1'}}}]
        batch = self.deliver(*rows)
        self.assertEqual([it['messages'][0]['event_id'] for it in batch['items']], ['implicit', 'back', 'mention'])
        self.assertEqual(self.read('run.out').count('\nANSWER '), 3)
        self.assertEqual(set(self.read('pending_acks').split()), {'self', 'system', 'agent'})
        self.assertEqual(self.server.tool_calls('accept_invite')[0]['args'], {'loop_id': 'loop1'})
        for n in (1, 2, 3):
            self.assertEqual(self.run_cmd('fm_reply.py', '--ack', str(n)).returncode, 0)
        self.assertIn('COMMITTED', self.run_cmd('sh', '--commit').out)
        self.server.wait_calls('poll_work', 2)
        self.assertNotIn('null', self.server.tool_calls('poll_work')[-1]['args']['ack'])
        self.assertEqual(self.server.unacknowledged, set())

    def test_04_replied_filter_retargets_newest_queue(self):
        self.write('replied_ids', 'old\nall')
        batch = self.deliver(item('old', messages=[message('old', timestamp=4), message('fresh', timestamp=3)]), item('all'))
        self.assertEqual(len(batch['items']), 1)
        it = batch['items'][0]
        self.assertEqual([m['event_id'] for m in it['messages']], ['fresh'])
        self.assertEqual(it['reply_with']['args']['in_reply_to'], 'fresh')
        self.assertEqual(set(self.read('pending_acks').split()), {'old', 'all'})
        self.assertIn('sent', self.run_cmd('fm_reply.py', '1', body=b'answer').out)
        self.run_cmd('sh', '--commit')
        self.server.wait_calls('poll_work', 2)
        self.assertEqual(self.server.unacknowledged, set())

    def test_05_truncated_immediate_sleep_capped(self):
        empty = {'work': [], 'cursor': '0', 'next_poll_ms': 8000}
        self.server.plan('poll_work', {'result': dict(empty, truncated=True)},
                         {'result': empty}, {'result': dict(empty, work=[item()])})
        self.run_cmd('fm_poll.py')
        calls = self.server.tool_calls('poll_work')
        self.assertLess(calls[1]['at'] - calls[0]['end'], 0.2)
        self.assertGreaterEqual(calls[2]['at'] - calls[1]['end'], 0.29)
        self.assertLess(calls[2]['at'] - calls[1]['end'], 0.8)
        self.assertEqual(calls[0]['args']['wait_seconds'], 2)

    def test_06_auth_errors_reserved_and_whole_status(self):
        for code, word in ((-32001, 'AUTH_FAILED'), (-32002, 'AGENT_RESERVED')):
            with self.subTest(code=code):
                (self.state / 'token_dead').unlink(missing_ok=True)
                self.server.plan('poll_work', {'error': {'code': code, 'message': 'bad credential'}})
                r = self.run_cmd('fm_poll.py')
                self.assertEqual(r.returncode, 2)
                self.assertIn(word, r.out)
                self.assertFalse((self.state / 'lock.d').exists())
                before = len(self.server.calls)
                self.assertIn('TOKEN_DEAD', self.run_cmd('fm_poll.py').out)
                self.assertEqual(len(self.server.calls), before)
        (self.state / 'token_dead').unlink()
        self.server.plan('poll_work', {'error': {'code': 4013, 'message': 'id401xx'}})
        self.assertIn('BACKOFF', self.run_cmd('fm_poll.py').out)
        self.assertFalse((self.state / 'token_dead').exists())
        self.write('next_try', '0')
        self.server.plan('poll_work', {'status': 403})
        self.assertIn('AUTH_FAILED', self.run_cmd('fm_poll.py').out)

    def test_07_backoff_doubles_resets(self):
        for expected in (1, 2, 4):
            self.write('next_try', '0')
            self.server.plan('poll_work', {'raw': 'not JSON'})
            self.assertIn('BACKOFF', self.run_cmd('fm_poll.py').out)
            self.assertEqual(float(self.read('backoff')), expected)
            before = len(self.server.calls)
            self.assertIn('BACKOFF', self.run_cmd('fm_poll.py').out)
            self.assertEqual(len(self.server.calls), before)
            self.assertFalse((self.state / 'lock.d').exists())
        self.write('next_try', '0')
        self.server.add(item())
        self.run_cmd('fm_poll.py')
        self.assertEqual(self.read('backoff'), '0')
        self.assertEqual(self.read('next_try'), '0')

    def test_08_concurrent_listeners_stale_and_handoff_tokens(self):
        first, second = self.start(), self.start()
        self.server.wait_calls('poll_work')
        self.wait(lambda: first.poll() is not None or second.poll() is not None)
        loser, winner = (first, second) if first.poll() is not None else (second, first)
        self.assertIn('LOCK_ALIVE', self.finish(loser))
        self.server.add(item())
        self.assertIn('WORK', self.finish(winner))
        self.assertEqual(self.server.max_active_polls, 1)
        self.assertEqual(len(self.server.tool_calls('poll_work')), 1)
        for name in ('pending_cursor', 'run.out'):
            (self.state / name).unlink()
        self.write('lock.d/owner', '99999999 0')
        self.server.add(item('next'))
        self.assertIn('WORK', self.run_cmd('fm_poll.py').out)
        self.assertIn('STALE-LOCK', self.read('listener.log'))
        for name in ('pending_cursor', 'run.out'):
            (self.state / name).unlink()
        self.write('lock.d/owner', '{} {}'.format(os.getpid(), time.time()))
        self.assertIn('LOCK_ALIVE', self.run_cmd('fm_poll.py', env=dict(self.env, FM_HANDOFF='1')).out)
        self.write('lock.d/handoff', 'valid {}'.format(time.time()))
        self.server.add(item('third'))
        self.assertIn('WORK', self.run_cmd('fm_poll.py', env=dict(self.env, FM_HANDOFF='valid')).out)
        self.assertFalse((self.state / 'lock.d').exists())

    def test_09_handler_age_redelivers_unanswered_queue(self):
        self.deliver(item('first'), item('second'))
        self.run_cmd('fm_reply.py', '1', body=b'one')
        self.assertEqual(self.server.unacknowledged, {'second'})
        self.assertIn('BATCH_IN_HAND', self.run_cmd('fm_poll.py').out)
        old = time.time() - 601
        os.utime(self.state / 'handling', (old, old))
        r = self.run_cmd('fm_poll.py')
        self.assertIn('WORK', r.out)
        self.assertIn('STALE-BATCH', self.read('listener.log'))
        self.assertEqual([i['messages'][0]['event_id'] for i in self.data()['items']], ['second'])
        self.assertEqual(self.read('cursor'), '0')
        self.assertEqual(self.server.unacknowledged, {'second'})

    def test_10_reply_verbatim_idempotent_ack_and_concurrent(self):
        self.deliver(item('one'), item('two'))
        body = "apostrophe's `literal` $(touch nope) \\path 😀\r\nsecond\n".encode()
        self.server.plan('post_message', {'apply': True, 'delay': 0.3})
        one = self.start('fm_reply.py', args=('1',), body=body)
        two = self.start('fm_reply.py', args=('1',), body=body)
        outcomes = [self.finish(one), self.finish(two)]
        self.assertTrue(any('sent' in s for s in outcomes))
        self.assertTrue(any('already recorded' in s for s in outcomes))
        posts = self.server.tool_calls('post_message')
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['args']['markdown_body'].encode(), body.rstrip(b'\n'))
        self.assertIn('ACK-QUEUED', self.run_cmd('fm_reply.py', '--ack', '2').out)
        self.assertEqual(set(self.read('replied_ids').split()), {'one', 'two'})
        self.assertIn('two', self.read('pending_acks'))

    def test_11_network_landed_queue_thread(self):
        self.deliver(item('root', thread_id='root', reply_with={
            'tool': 'reply_in_thread', 'args': {'message_id': 'root'}}))
        self.server.plan('reply_in_thread', {'apply': True, 'disconnect': True})
        result = self.run_cmd('fm_reply.py', '1', body=b'landed reply')
        self.assertIn('landed', result.out)
        self.assertEqual(len(self.server.tool_calls('reply_in_thread')), 1)
        self.assertEqual(self.server.tool_calls('get_recent_messages')[-1]['args'], {'channel': '!room:test', 'limit': 40})
        self.assertEqual(self.server.unacknowledged, set())

    def test_11_network_not_found_retries_once_queue(self):
        self.deliver()
        self.server.plan('post_message', {'disconnect': True})
        self.assertIn('sent', self.run_cmd('fm_reply.py', '1', body=b'retry me').out)
        self.assertEqual(len(self.server.tool_calls('post_message')), 2)
        self.assertEqual(self.server.unacknowledged, set())

    def test_11_lookup_failure_never_reposts(self):
        self.deliver()
        self.server.plan('post_message', {'disconnect': True})
        self.server.plan('get_recent_messages', {'raw': 'broken'})
        r = self.run_cmd('fm_reply.py', '1', body=b'uncertain')
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAILED uncertain send', r.out)
        self.assertEqual(len(self.server.tool_calls('post_message')), 1)
        self.assertEqual(len(self.server.tool_calls('message_principal')), 1)
        self.assertEqual(self.server.unacknowledged, {'e1'})
        self.run_cmd('sh', '--commit')
        self.server.wait_calls('poll_work', 2)
        self.assertEqual(self.server.unacknowledged, set())

    def test_12_already_answered_and_failure_notice_queue(self):
        self.deliver(item('one'), item('two', messages=[message('two', body='original ' * 100)]))
        self.server.answered.add('one')
        self.assertIn('already answered', self.run_cmd('fm_reply.py', '1', body=b'answer').out)
        self.server.plan('post_message', {'error': {'code': -32602, 'message': 'no slot'}})
        r = self.run_cmd('fm_reply.py', '2', body=b'nope')
        self.assertEqual(r.returncode, 1)
        self.assertIn('FAILED', r.out)
        self.assertIn(('original ' * 100)[:300], self.server.tool_calls('message_principal')[0]['args']['markdown_body'])
        self.assertIn('two', self.read('pending_acks'))
        self.assertEqual(json.loads(self.read('failed_replies'))['notified'], 'yes')
        self.run_cmd('sh', '--commit')
        self.server.wait_calls('poll_work', 2)
        self.assertEqual(self.server.unacknowledged, set())

    def test_13_checked_commit_status_token(self):
        self.deliver()
        self.assertIn('UNHANDLED n=1', self.run_cmd('sh', '--commit').out)
        self.assertEqual(self.read('cursor'), '0')
        self.run_cmd('fm_reply.py', '--ack', '1')
        self.assertIn('COMMITTED\nSTARTED', self.run_cmd('sh', '--commit').out)
        self.assertEqual(self.read('cursor'), '1')
        self.assertFalse((self.state / 'handling').exists())
        self.assertEqual(self.read('pending_cursor'), '')
        before = len(self.server.calls)
        status = self.run_cmd('sh', '--status').out
        self.assertRegex(status, r'token=ok lock=.* pending=none backoff=.* last_poll=.* last_work=.* hb=')
        self.assertEqual(len(self.server.calls), before)
        self.dead()
        r = self.run_cmd('sh', '--token', body=b'replacement-secret\n')
        self.assertEqual(r.out, 'TOKEN_SET\n')
        self.assertEqual((self.state / 'token').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / 'token_dead').exists())
        self.assertEqual(self.read('token'), 'replacement-secret')
        self.assertIn('NOTHING_TO_COMMIT', self.run_cmd('sh', '--commit').out)

    def test_14_exactly_one_claim_and_start_only(self):
        self.deliver(claim=False)
        command = ['/usr/bin/env', 'bash', str(self.state / 'poll.sh')]
        children = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        outputs = [p.communicate(timeout=10)[0].decode() for p in children]
        self.assertEqual(sum('ANSWER n=1' in s for s in outputs), 1)
        self.assertTrue(any('BATCH_IN_HAND' in s for s in outputs))
        self.assertEqual(self.read('handling'), self.data()['batch_id'])
        self.assertEqual(self.read('run.out.shown'), self.read('handling'))
        self.assertFalse((self.state / 'work.ready').exists())
        self.run_cmd('fm_reply.py', '--ack', '1')
        self.run_cmd('sh', '--commit')
        self.server.wait_calls('poll_work', 2)
        self.assertIn('LISTENING', self.run_cmd('sh').out)
        self.assertEqual(self.server.max_active_polls, 1)

    def test_15_keeper_interval_replacement_and_handover(self):
        first = self.start('fm_hb.py')
        self.server.wait_calls('heartbeat', 2, timeout=5)
        beats = self.server.tool_calls('heartbeat')
        self.assertGreaterEqual(beats[1]['at'] - beats[0]['at'], 1.9)
        second = self.start('fm_hb.py')
        self.finish(first, timeout=4)
        self.wait(lambda: self.read('hb.pid') == str(second.pid))
        self.write('lock.d/owner', '{} {}'.format(os.getpid(), time.time()))
        self.finish(second, timeout=3)
        # Do not let teardown treat the synthetic lease as a child.
        shutil.rmtree(self.state / 'lock.d')
        self.assertIn('listener live', self.read('listener.log'))

    def test_16_media_size_type_http_cleanup(self):
        r = self.run_cmd('fm_media.py', 'mxc://test/a')
        self.assertEqual(r.returncode, 0)
        path = Path(r.out.strip())
        self.assertEqual(path.suffix, '.png')
        self.assertEqual(path.read_bytes(), b'\x89PNG\r\nfixture')
        self.server.plan('media', {'length': 25 * 1024 * 1024})
        self.assertEqual(self.run_cmd('fm_media.py', 'mxc://test/huge').out, 'TOO_LARGE\n')
        for code in (401, 403, 404):
            self.server.plan('media', {'status': code})
            self.assertIn('DOWNLOAD_FAILED', self.run_cmd('fm_media.py', 'mxc://test/no').out)
            self.assertFalse((self.state / 'token_dead').exists())
        old = time.time() - 3601
        os.utime(path, (old, old))
        self.server.add(item())
        self.run_cmd('fm_poll.py')
        self.assertFalse(path.exists())

    def test_17_rotation_syntax_and_optional_shellcheck(self):
        self.write('listener.log', 'full line\n' * 70000)
        self.run_cmd('sh', '--status')
        data = (self.state / 'listener.log').read_bytes()
        self.assertLess(len(data), 64 * 1024)
        self.assertTrue(data.startswith(b'full line\n') and data.endswith(b'full line\n'))
        r = subprocess.run(['/usr/bin/env', 'bash', '-n', str(self.state / 'poll.sh')], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, '-m', 'py_compile'] + [str(p) for p in self.state.glob('*.py')], capture_output=True,
                           env=dict(self.env, PYTHONPYCACHEPREFIX=str(self.state / 'pycache')))
        self.assertEqual(r.returncode, 0, r.stderr)
        if shutil.which('shellcheck'):
            r = subprocess.run(['shellcheck', str(self.state / 'poll.sh')], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout.decode())

    def pinned_bootstrap(self, wrong=False):
        pinned = {}
        for p in SCRIPTS.iterdir():
            if p.is_file():
                self.server.assets[p.name] = p.read_bytes()
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                if wrong and p.name == 'fm_reply.py':
                    digest = '0' * 64
                pinned[p.name] = [self.server.base + '/assets/' + p.name, digest]
        wrapper = (SCRIPTS / 'poll.sh').read_text().replace('PINNED = {}', 'PINNED = ' + repr(pinned))
        self.write('poll.sh', wrapper)

    def test_18_rebuild_live_listener_and_transactional_install_queue(self):
        self.write('cursor', '0')
        p = self.start()
        self.server.wait_calls('poll_work')
        self.collect_pids()
        shutil.rmtree(self.state)
        self.state.mkdir()
        self.pinned_bootstrap(wrong=True)
        baseline = (self.state / 'poll.sh').read_bytes()
        r = self.run_cmd('sh', '--install')
        self.assertIn('INSTALL_FAILED fm_reply.py sha256 mismatch', r.out)
        self.assertEqual((self.state / 'poll.sh').read_bytes(), baseline)
        self.assertFalse((self.state / 'fm_lib.py').exists())
        self.assertFalse((self.state / 'install.tmp').exists())
        self.finish(p, timeout=5)
        self.pinned_bootstrap()
        self.assertEqual(self.run_cmd('sh', '--install').out, 'INSTALLED\n')
        for source in SCRIPTS.iterdir():
            if source.is_file():
                self.assertEqual((self.state / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(self.run_cmd('sh', '--token', body=b'new-token\n').out, 'TOKEN_SET\n')
        self.server.add(item('rebuilt'))
        self.assertEqual(self.run_cmd('sh').out, 'STARTED\n')
        self.wait(lambda: bool(self.read('run.out')))
        self.assertIn('ANSWER n=1', self.run_cmd('sh').out)
        self.assertEqual(self.server.unacknowledged, {'rebuilt'})
        self.run_cmd('fm_reply.py', '1', body=b'back online')
        self.run_cmd('sh', '--commit')
        self.server.wait_calls('poll_work', 3)
        self.assertEqual(self.server.unacknowledged, set())

    def test_19_unscaled_budget_sixty_to_ninety_seconds(self):
        env = dict(self.env, FM_BUDGET='60')
        env.pop('FM_TEST_FAST')
        start = time.monotonic()
        p = self.start(env=env)
        self.assertIn('HANDOFF', self.finish(p, timeout=91))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 60)
        self.assertLessEqual(elapsed, 90)
        self.wait(lambda: len(re.findall('listener start pid=', self.read('listener.log'))) >= 2)
        self.assertEqual(self.server.max_active_polls, 1)

    def test_20_ack_snapshot_preserves_concurrent_append(self):
        self.write('cursor', '0')
        self.write('pending_acks', 'before')
        self.server.plan('poll_work', {'delay': 0.6, 'result': {'cursor': '1', 'work': [item()], 'next_poll_ms': 0}})
        p = self.start()
        self.server.wait_calls('poll_work')
        # Simulate the atomic writer protocol used by fm_reply while poll is in flight.
        self.write('pending_acks', 'before during')
        self.finish(p)
        self.assertEqual(self.read('pending_acks'), 'during')
        self.assertEqual(self.server.tool_calls('poll_work')[0]['args']['ack'], ['before'])

    def test_21_unnotified_visible_and_retry_only_notice(self):
        self.deliver()
        self.server.plan('post_message', {'error': {'code': -32602, 'message': 'no'}})
        self.server.plan('message_principal', {'status': 503})
        self.run_cmd('fm_reply.py', '1', body=b'answer')
        self.assertIn('FAILED_UNNOTIFIED 1', self.run_cmd('sh', '--status').out)
        self.assertIn('FAILED_UNNOTIFIED 1', self.run_cmd('sh').out)
        self.assertNotIn('FAILED_UNNOTIFIED', self.run_cmd('sh', '--status', '--notify').out)
        self.assertEqual(len(self.server.tool_calls('post_message')), 1)
        self.assertEqual(len(self.server.tool_calls('message_principal')), 2)
        self.assertEqual(json.loads(self.read('failed_replies'))['notified'], 'yes')

    def test_22_reserved_probe_rotation_and_inflight_old_token(self):
        self.dead('reserved')
        self.server.plan('get_self', {'error': {'code': -32002, 'message': 'reserved'}})
        self.assertIn('AGENT_RESERVED', self.run_cmd('sh').out)
        self.assertIn('STARTED', self.run_cmd('sh').out)
        self.server.wait_calls('poll_work')
        self.assertFalse((self.state / 'token_dead').exists())
        self.run_cmd('sh', '--stop')
        # Finish the previous server-side long poll before the next independent
        # token-rotation scenario; a dead client does not cancel server work.
        self.wait(lambda: self.server.active_polls == 0)
        self.server.plan('poll_work', {'delay': 0.5, 'error': {'code': -32001, 'message': 'fixture-credential leaked'}})
        old_count = len(self.server.tool_calls('poll_work'))
        p = self.start()
        self.server.wait_calls('poll_work', old_count + 1)
        self.run_cmd('sh', '--token', body=b'rotated\n')
        self.finish(p)
        self.assertFalse((self.state / 'token_dead').exists())
        self.assertNotIn('fixture-credential', self.read('listener.log'))
        self.server.add(item())
        self.run_cmd('fm_poll.py')
        digest = hashlib.sha256(b'Bearer rotated').hexdigest()
        self.assertEqual(self.server.tool_calls('poll_work')[-1]['token_hash'], digest)

    def test_23_auth_routes_startup_enrichment_reply_lookup_notice(self):
        for tool in ('get_self', 'list_pending_invites', 'list_vouches', 'accept_invite', 'get_recent_messages'):
            with self.subTest(tool=tool):
                for name in ('token_dead', 'self.json', 'pending_cursor'):
                    (self.state / name).unlink(missing_ok=True)
                if tool == 'accept_invite':
                    self.server.plan('list_pending_invites', {'result': {'invites': [{'loop_id': 'l'}]}})
                self.server.plan(tool, {'error': {'code': -32001, 'message': 'auth'}})
                if tool == 'get_recent_messages':
                    self.server.add(item())
                r = self.run_cmd('fm_poll.py')
                self.assertEqual(r.returncode, 2)
                self.assertIn('AUTH_FAILED', r.out)
        for name in ('token_dead', 'pending_cursor'):
            (self.state / name).unlink(missing_ok=True)
        self.deliver(item('reply'))
        self.server.plan('post_message', {'disconnect': True})
        self.server.plan('get_recent_messages', {'status': 401})
        self.server.plan('message_principal', {'error': {'code': -32002, 'message': 'reserved'}})
        r = self.run_cmd('fm_reply.py', '1', body=b'answer')
        self.assertIn('FAILED uncertain send', r.out)
        self.assertEqual(self.data('token_dead')['kind'], 'reserved')
        self.assertEqual(len(self.server.tool_calls('post_message')), 1)

    def test_24_sse_context_failure_thread_bounds_and_no_event_success(self):
        self.server.plan('get_self', {'sse': True})
        self.server.plan('get_recent_messages', {'error': {'code': -32602, 'message': 'not available'}})
        self.server.context = [message(str(n), timestamp=n) for n in range(70, 0, -1)]
        batch = self.deliver(item('root', thread_id='root', is_backchannel=True,
                                  reply_with={'tool': 'reply_in_thread', 'args': {'message_id': 'root'}}))
        it = batch['items'][0]
        self.assertIn('context_error', it)
        self.assertEqual(len(it['thread']), 50)
        self.assertEqual(it['thread'][0]['timestamp'], 21)
        self.assertEqual(it['reply_with']['args'], {'message_id': 'root'})
        self.assertEqual(self.server.tool_calls('get_recent_messages')[0]['args']['limit'], 100)
        self.server.plan('reply_in_thread', {'result': {}, 'sse': True})
        self.assertIn('sent unknown', self.run_cmd('fm_reply.py', '1', body=b'answer').out)

    def test_25_second_attempt_already_answered_and_no_third(self):
        self.deliver(item('one'), item('two'))
        self.server.plan('post_message', {'disconnect': True}, {'error': {'code': -32602, 'message': 'Already Answered'}})
        self.assertIn('already answered', self.run_cmd('fm_reply.py', '1', body=b'answer').out)
        self.server.plan('post_message', {'disconnect': True}, {'disconnect': True})
        self.assertIn('FAILED', self.run_cmd('fm_reply.py', '2', body=b'other').out)
        self.assertEqual(len(self.server.tool_calls('post_message')), 4)

    def test_26_unclaimed_batch_stale_reply_force_commit_bad_budget(self):
        self.deliver(claim=False)
        self.assertEqual(self.run_cmd('fm_reply.py', '1', body=b'no claim').out, 'STALE_BATCH\n')
        self.assertIn('BATCH_IN_HAND', self.run_cmd('fm_poll.py').out)
        self.run_cmd('sh')
        self.assertEqual(self.run_cmd('fm_reply.py', '999', body=b'bad').out, 'BAD_ITEM\n')
        self.assertEqual(self.run_cmd('fm_reply.py', '1', body=b'').out, 'EMPTY_BODY\n')
        self.assertIn('COMMITTED', self.run_cmd('sh', '--commit', '--force').out)
        self.assertIn('BAD_BUDGET', self.run_cmd('fm_poll.py', env=dict(self.env, FM_BUDGET='oops')).out)

    def test_27_expired_handoff_and_orphan_pending(self):
        self.write('lock.d/owner', '{} {}'.format(os.getpid(), time.time()))
        self.write('lock.d/handoff', 'expired {}'.format(time.time() - 11))
        self.assertIn('LOCK_ALIVE', self.run_cmd('fm_poll.py', env=dict(self.env, FM_HANDOFF='expired')).out)
        self.write('pending_cursor', '123')
        old = time.time() - 601
        os.utime(self.state / 'pending_cursor', (old, old))
        self.server.add(item())
        self.assertIn('WORK', self.run_cmd('fm_poll.py').out)
        self.assertIn('STALE-BATCH unpublished', self.read('listener.log'))
        self.assertIn('STALE-LOCK', self.read('listener.log'))

    def test_28_install_empty_pin_fails_closed_and_ledger_cap(self):
        baseline = {p.name: p.read_bytes() for p in self.state.glob('*.py')}
        self.assertIn('INSTALL_FAILED manifest', self.run_cmd('sh', '--install').out)
        self.assertEqual(baseline, {p.name: p.read_bytes() for p in self.state.glob('*.py')})
        self.write('replied_ids', '\n'.join('prior' + str(n) for n in range(2000)))
        self.deliver()
        self.run_cmd('fm_reply.py', '--ack', '1')
        ids = self.read('replied_ids').split()
        self.assertEqual(len(ids), 2000)
        self.assertEqual(ids[0], 'prior1')
        self.assertEqual(ids[-1], 'e1')

    def test_29_whole_auth_values_and_request_secret_redaction(self):
        code = "import sys;sys.path[0]=sys.argv[1];from fm_lib import classify_auth; " \
               "print([classify_auth(s) for s in ['HTTP 401','RPC -32002: reserved','4013','id401xx','id401','-320010']])"
        r = subprocess.run([sys.executable, '-B', '-c', code, str(self.state)],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "['auth', 'reserved', None, None, None, None]")
        self.deliver()
        self.server.plan('post_message', {'error': {'code': -32602, 'message': 'fixture-credential'}})
        r = self.run_cmd('fm_reply.py', '1', body=b'answer')
        self.assertNotIn('fixture-credential', r.out)
        for name in ('reply.log', 'listener.log', 'failed_replies'):
            self.assertNotIn('fixture-credential', self.read(name))

    def test_30_five_file_release_pin_and_existing_install_rollback(self):
        import ast
        self.pinned_bootstrap(wrong=True)
        before = {p.name: p.read_bytes() for p in self.state.glob('*.py')}
        self.assertIn('INSTALL_FAILED', self.run_cmd('sh', '--install').out)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.glob('*.py')})
        self.pinned_bootstrap()
        source = self.read('poll.sh')
        manifest_line = next(line for line in source.splitlines() if line.startswith('PINNED = '))
        manifest = ast.literal_eval(manifest_line.split(' = ', 1)[1].split('  #', 1)[0])
        manifest.pop('poll.sh')
        source = source.replace(manifest_line, 'PINNED = ' + repr(manifest))
        self.write('poll.sh', source)
        self.assertEqual(self.run_cmd('sh', '--install').out, 'INSTALLED\n')
        self.assertEqual(self.read('poll.sh'), source)
        self.assertFalse((self.state / 'install.lock.d').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
