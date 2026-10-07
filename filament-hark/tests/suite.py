#!/usr/bin/env python3
"""Real Bash processes, real epoch deadlines, entirely fake network calls."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / 'scripts'
BASH = os.environ.get('FILAMENT_TEST_BASH', shutil.which('bash'))
try:
    NATIVE_PS = subprocess.run(['ps', '-o', 'args=', '-p', str(os.getpid())], capture_output=True).returncode == 0
except OSError:
    NATIVE_PS = False

def message(event='event-1', sender='@human:test', **kw):
    return dict(event_id=event, sender=sender, body='Hello', timestamp=1791374400000, **kw)

def item(event='event-1', sender='@human:test', **kw):
    return dict(channel_id='!room:test', thread_id='thread', is_backchannel=False,
                messages=[message(event, sender)], reply_with={'tool': 'send_reply', 'args': {'in_reply_to': event}}, **kw)

def work(*items, **kw):
    return {'out': dict(work=list(items), cursor='never-persist-this', next_poll_ms=0, **kw)}

class Scripts(unittest.TestCase):
    def setUp(self):
        base = os.environ.get('FILAMENT_STATE')
        if base: Path(base).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='filament-test-', dir=base)
        self.state = Path(self.temp.name)
        for p in SCRIPTS.glob('*.sh'): shutil.copy2(p, self.state / p.name)
        self.env = dict(os.environ, FILAMENT_STATE=str(self.state), FILAMENT_TEST_FAST='1',
                        PATH=str(HERE / 'fakes') + os.pathsep + os.environ['PATH'])
        self.env.pop('FILAMENT_JOB_START', None)
        self.bash = BASH
        if not NATIVE_PS:
            fallback = self.state / 'process-bin'; fallback.mkdir()
            for name in ('bash', 'ps'):
                shutil.copy2(HERE / 'process_tools.py', fallback / name)
                (fallback / name).chmod(0o755)
            self.bash = str(fallback / 'bash')
            self.env.update(FILAMENT_TEST_REAL_BASH=BASH, FILAMENT_TEST_PS_CATALOGUE='1',
                            PATH=str(fallback) + os.pathsep + self.env['PATH'])
        self.processes = []
        self.scenario()

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try: p.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL); p.communicate()
        self.temp.cleanup()

    def scenario(self, **rules):
        (self.state / 'scenario.json').write_text(json.dumps(rules))

    def calls(self, key=None):
        p = self.state / 'calls.jsonl'
        calls = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if key is None or c['key'] == key]

    def start(self, script='listen.sh', args=(), age=None, body=None):
        env = dict(self.env)
        if age is not None: env['FILAMENT_JOB_START'] = str(int(time.time()) - age)
        p = subprocess.Popen([self.bash, str(self.state / script), *args], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
        self.processes.append(p)
        if body is not None: p.stdin.write(body); p.stdin.close(); p.stdin = None
        return p

    def finish(self, p, rc, last=None, timeout=12):
        out, err = p.communicate(timeout=timeout)
        text = out.decode()
        self.assertEqual(p.returncode, rc, (text, err.decode()))
        if last: self.assertEqual(text.splitlines()[-1], last, (text, err.decode()))
        self.assertNotIn('Traceback', err.decode())
        return text

    def run_script(self, script='listen.sh', args=(), age=None, body=None, rc=3, last='NO_WORK', timeout=12):
        return self.finish(self.start(script, args, age, body), rc, last, timeout)

    def wait_for(self, predicate, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.01)
        self.fail('timed out waiting for condition')

    def log(self):
        return (self.state / 'timing.log').read_text()

    def write_item(self, value=None, number=1):
        (self.state / 'items').mkdir(exist_ok=True)
        (self.state / 'items' / f'{number}.json').write_text(json.dumps(value if value is not None else item()))

    def fake_listener(self):
        (self.state / 'listen.sh').write_text('''#!/usr/bin/env bash
printf '%s %s\\n' "$$" "${FILAMENT_JOB_START:-missing}" >> "$FILAMENT_STATE/chained"
rm -f "$FILAMENT_STATE/restart.pending"
echo NO_WORK
exit 3
''')

    def assert_chained(self):
        lines = (self.state / 'chained').read_text().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.split()[1].isdigit() for line in lines))

    def test_01_empty_budget_and_cleanup(self):
        start = time.monotonic()
        self.run_script(args=['--budget', '60'], timeout=65)
        self.assertLess(time.monotonic() - start, 65)
        self.assertGreater(len(self.calls('poll_work')), 1)
        self.assertFalse((self.state / 'listener.lock.d').exists())
        self.assertFalse((self.state / 'listener.pid').exists())
        self.assertFalse((self.state / 'cursor').exists())

    def test_02_second_poll_delivery(self):
        target = item()
        self.scenario(poll_work=[work(), work(target)])
        output = self.run_script(rc=0, last='ITEMS 1')
        delivered = json.loads(output.splitlines()[0])
        self.assertEqual(delivered['work'], [target])
        self.assertEqual(json.loads((self.state / 'items/1.json').read_text()), target)
        self.assertEqual(json.loads((self.state / 'items/wake.json').read_text()), delivered)
        self.assertIn('WORK 1791374400000 items=1', self.log())
        self.assertIn('WAKE-DELIVERED', self.log())
        self.assertEqual(len(self.calls('poll_work')), 2)

    def test_03_filters_and_replied_message_stripping(self):
        god = item('god', '@filament_god:test')
        agent = item('agent', '@agent:test'); agent['messages'][0]['sender_is_agent'] = True
        own = item('self', '@self:test')
        back = item('back', '@self:test'); back['is_backchannel'] = True
        null = item('null'); null['reply_with'] = None
        human = item('human'); human['messages'][0]['is_mention'] = True
        self.scenario(poll_work=[work(god, agent, own, back, null, human)])
        output = self.run_script(rc=0, last='ITEMS 2')
        self.assertEqual(json.loads(output.splitlines()[0])['work'], [back, human])
        self.assertEqual(self.calls('ack')[0]['args']['ack'], ['god', 'agent', 'self'])
        self.assertIn('FILTERED 3', self.log())
        mixed = item('old'); mixed['messages'].append(message('new'))
        (self.state / 'replied.json').write_text(json.dumps({'ids': ['old', 'all-old']}))
        self.scenario(poll_work=[work(mixed, item('all-old'))])
        output = self.run_script(rc=0, last='ITEMS 1')
        delivered = json.loads(output.splitlines()[0])['work'][0]
        self.assertEqual(delivered['messages'], [message('new')])
        self.assertEqual(delivered['reply_with'], mixed['reply_with'])
        self.assertEqual(self.calls('ack')[-1]['args']['ack'], ['all-old'])
        self.assertFalse((self.state / 'items/2.json').exists())

    def test_04_truncated_and_busy_waits(self):
        self.scenario(poll_work=[{'out': {'work': [], 'truncated': True, 'next_poll_ms': 60000}},
                                 {'out': {'work': [], 'busy': True, 'next_poll_ms': 5000}}, work(item())])
        self.run_script(rc=0, last='ITEMS 1')
        calls = self.calls('poll_work')
        self.assertLess(calls[1]['at'] - calls[0]['at'], 1)
        self.assertGreaterEqual(calls[2]['at'] - calls[1]['at'], .48)

    def test_05_auth_reserved_and_false_positives(self):
        for error, word, stored in [('-32001', 'AUTH_FAILED', 'auth'), ('-32002', 'AGENT_RESERVED', 'reserved')]:
            with self.subTest(error=error):
                self.scenario(poll_work=[{'rc': 1, 'err': error}])
                self.run_script(rc=2, last=word)
                self.assertEqual((self.state / 'auth_failed').read_text().strip(), stored)
                before = len(self.calls())
                self.run_script(rc=2, last=word)
                self.assertEqual(len(self.calls()), before)
                (self.state / 'auth_failed').unlink()
        benign = item('event401'); benign['messages'][0]['body'] = '4013'
        self.scenario(poll_work=[{'rc': 1, 'err': 'body 4013 event401 -320010'}, work(benign)])
        # Reset counters so the first error is exercised.
        (self.state / 'counts.json').unlink()
        self.run_script(rc=0, last='ITEMS 1')
        self.assertFalse((self.state / 'auth_failed').exists())

    def test_06_simultaneous_listeners(self):
        self.scenario(poll_work=[dict(work(item()), delay=1.5)])
        p1, p2 = self.start(), self.start()
        self.wait_for(lambda: p1.poll() is not None or p2.poll() is not None)
        loser, winner = (p1, p2) if p1.poll() is not None else (p2, p1)
        self.finish(loser, 3, 'DUPLICATE')
        self.assertEqual((self.state / 'listener.lock.d/pid').read_text().strip(), str(winner.pid))
        self.finish(winner, 0, 'ITEMS 1')
        self.assertEqual(len(self.calls('poll_work')), 1)

    def test_07_stale_lock_and_wrong_process_identity(self):
        lock = self.state / 'listener.lock.d'; lock.mkdir()
        (lock / 'pid').write_text('99999999')
        self.scenario(poll_work=[work(item())])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertIn('LISTENER-STALE-LOCK', self.log())
        lock.mkdir(); (lock / 'pid').write_text(str(os.getpid()))
        self.run_script(rc=0, last='ITEMS 1')
        self.assertTrue(os.kill(os.getpid(), 0) is None)

    def test_08_term_mid_poll_reaps_child(self):
        self.scenario(poll_work=[dict(work(), delay=50)])
        p = self.start()
        self.wait_for(lambda: len(self.calls('poll_work')) == 1)
        child = self.calls('poll_work')[0]['pid']
        p.send_signal(signal.SIGTERM)
        self.finish(p, 3, 'KILLED')
        with self.assertRaises(ProcessLookupError): os.kill(child, 0)
        self.assertFalse((self.state / 'listener.lock.d').exists())
        self.assertFalse((self.state / 'listener.pid').exists())
        self.assertIn('LISTENER-KILLED-SELF', self.log())

    def test_09_reply_verbatim_record_before_send_and_exec(self):
        self.fake_listener(); self.write_item()
        body = "Here's `literal` $(touch SHOULD_NOT_EXIST) \\ backslash 😀\nsecond line\r\n\n".encode()
        self.scenario(send_reply=[{'out': {'ok': True}, 'assert_recorded': ['event-1']}])
        p = self.start('reply.sh', ['1'], body=body)
        self.finish(p, 3, 'NO_WORK')
        call = self.calls('send_reply')[0]
        self.assertEqual(call['args'], {'in_reply_to': 'event-1', 'markdown_body': body[:-1].decode()})
        self.assertEqual(call['timeout'], 60)
        self.assertEqual((self.state / 'chained').read_text().split()[0], str(p.pid))
        self.assert_chained()
        self.assertFalse((self.state / 'items/1.json').exists())
        self.assertFalse((self.state / 'reply.lock.d').exists())
        self.assertFalse((Path.cwd() / 'SHOULD_NOT_EXIST').exists())

    def test_10_repeat_ack_and_invalid_inputs_continue(self):
        self.fake_listener(); self.write_item()
        self.run_script('reply.sh', ['1'], body=b'hello\n')
        self.write_item()
        self.run_script('reply.sh', ['1'], body=b'duplicate\n')
        self.write_item(item('explicit'))
        self.run_script('reply.sh', ['--ack', '1'])
        self.assertEqual(len(self.calls('send_reply')), 1)
        self.assertEqual([c['args']['ack'] for c in self.calls('ack')], [['event-1'], ['explicit']])
        for args in (['1'], [], ['0'], ['../1'], ['abc']):
            with self.subTest(args=args):
                output = self.run_script('reply.sh', args, body=b'body')
                self.assertEqual(output.splitlines(), ['BAD_ITEM', 'NO_WORK'])
        self.write_item(item('empty'))
        output = self.run_script('reply.sh', ['1'], body=b'\n')
        self.assertEqual(output.splitlines(), ['EMPTY_BODY', 'NO_WORK'])
        noids = item(); noids['messages'] = [message('')]
        self.write_item(noids)
        self.run_script('reply.sh', ['1'], body=b'body')
        self.assertIn('NO-IDS', self.log())
        self.assert_chained()

    def test_11_reply_failure_no_retry_and_auth(self):
        self.fake_listener(); self.write_item()
        self.scenario(send_reply=[{'rc': 1, 'err': 'temporary failure', 'assert_recorded': ['event-1']}])
        self.run_script('reply.sh', ['1'], body=b'hello')
        self.assertIn('REPLIED rc=1', self.log())
        self.assertEqual(json.loads((self.state / 'replied.json').read_text())['ids'], ['event-1'])
        self.assertEqual(len(self.calls('send_reply')), 1)
        self.assert_chained()
        self.write_item(item('auth'))
        self.scenario(send_reply=[{'rc': 1, 'err': '-32001'}])
        self.run_script('reply.sh', ['1'], body=b'hello', rc=2, last='AUTH_FAILED')
        self.assertEqual((self.state / 'auth_failed').read_text().strip(), 'auth')
        self.assertIn('RELAUNCHED', self.log())

    def test_12_restart_young_old_and_concurrent(self):
        self.scenario(poll_work=[dict(work(), delay=50)])
        original = self.start()
        self.wait_for(lambda: len(self.calls('poll_work')) == 1)
        self.run_script('restart_listener.sh', last='ALREADY_LISTENING')
        self.assertIsNone(original.poll())
        (self.state / 'listener.lock.d/started').write_text(str(int(time.time()) - 20))
        replacement = self.start('restart_listener.sh')
        self.finish(original, 3, 'KILLED')
        self.wait_for(lambda: len(self.calls('poll_work')) == 2)
        self.assertEqual((self.state / 'listener.lock.d/pid').read_text().strip(), str(replacement.pid))
        self.assertIn(f'LISTENER-KILLED {original.pid}', self.log())
        replacement.terminate(); self.finish(replacement, 3, 'KILLED')
        p1, p2 = self.start('restart_listener.sh'), self.start('restart_listener.sh')
        self.wait_for(lambda: p1.poll() is not None or p2.poll() is not None)
        loser, winner = (p1, p2) if p1.poll() is not None else (p2, p1)
        text = self.finish(loser, 3)
        self.assertIn(text.strip(), ['RESTART_BUSY', 'ALREADY_LISTENING'])
        self.wait_for(lambda: len(self.calls('poll_work')) == 3)
        self.assertEqual((self.state / 'listener.lock.d/pid').read_text().strip(), str(winner.pid))
        winner.terminate(); self.finish(winner, 3, 'KILLED')

    def test_13_deadlines_and_endless_truncated(self):
        self.scenario(poll_work=[dict(work(), delay='poll_wait')])
        started = time.monotonic()
        self.run_script(age=450, timeout=32)
        self.assertLess(time.monotonic() - started, 31)
        polls = self.calls('poll_work')
        self.assertEqual(len(polls), 1)
        self.assertLessEqual(polls[0]['args']['wait_seconds'], 15)
        self.assertLessEqual(polls[0]['timeout'], 30)
        self.scenario(poll_work=[{'out': {'work': [], 'truncated': True}}])
        started = time.monotonic()
        self.run_script(age=458, timeout=5)
        self.assertLess(time.monotonic() - started, 5)
        self.write_item(item('slow'))
        self.scenario(send_reply=[{'delay': 11, 'out': {}, 'assert_recorded': ['slow']}])
        started = time.monotonic()
        self.run_script('reply.sh', ['1'], age=450, body=b'slow', timeout=32)
        self.assertLess(time.monotonic() - started, 31)
        call = self.calls('send_reply')[0]
        self.assertLessEqual(call['timeout'], 30)
        self.assertEqual(len(self.calls('send_reply')), 1)
        for call in self.calls():
            if call['job_start']:
                self.assertLessEqual(call['timeout'], int(call['job_start']) + 480 - int(call['at']) + 1)

    def test_14_log_rotation_and_line_caps(self):
        for name in ('timing.log', 'listen.log', 'reply.log', 'backstop.log'):
            (self.state / name).write_bytes(b'x' * 999 + b'\n' + (b'entry ' + b'z' * 992 + b'\n') * 620)
        # Paused start rotates, without adding timing lines.
        (self.state / 'auth_failed').write_text('auth')
        self.run_script(rc=2, last='AUTH_FAILED')
        for name in ('timing.log', 'listen.log', 'reply.log', 'backstop.log'):
            data = (self.state / name).read_bytes()
            self.assertLessEqual(len(data), 65536)
            self.assertTrue(data.endswith(b'\n'))
            self.assertTrue(data.startswith(b'entry '))
        (self.state / 'auth_failed').unlink()
        self.fake_listener(); self.write_item()
        self.scenario(send_reply=[{'out': {'large': 'x' * 8000}}])
        self.run_script('reply.sh', ['1'], body=b'hello')
        self.assertEqual(json.loads((self.state / 'last_result.json').read_text())['large'], 'x' * 8000)
        self.assertTrue(all(len(line) <= 2000 for line in (self.state / 'reply.log').read_text().splitlines()))

    def test_25_media_enrichment_download_and_cleanup(self):
        import hashlib
        target = item()
        attachment = [{'mxc_url': 'mxc://server/image'}]
        self.scenario(poll_work=[work(target, item('second'))], get_recent_messages=[
            {'out': {'messages': [dict(event_id='unrelated', media=[]),
                                  dict(event_id='event-1', media=attachment, msgtype='m.image')]}},
            {'rc': 1, 'err': 'HTTP 401 enrichment failed'}])
        media = self.state / 'media'; media.mkdir()
        old = media / 'old.png'; old.write_bytes(b'old'); os.utime(old, (time.time()-3700,)*2)
        fresh = media / 'fresh.png'; fresh.write_bytes(b'fresh')
        output = self.run_script(rc=0, last='ITEMS 2')
        delivered = json.loads(output.splitlines()[0])
        expected = dict(target['messages'][0], media=attachment, msgtype='m.image')
        self.assertEqual(delivered['work'][0]['messages'][0], expected)
        self.assertEqual(json.loads((self.state / 'items/1.json').read_text())['messages'][0], expected)
        self.assertEqual(json.loads((self.state / 'items/wake.json').read_text()), delivered)
        self.assertEqual(delivered['work'][1], item('second'))
        self.assertEqual([c['args'] for c in self.calls('get_recent_messages')],
                         [{'channel': '!room:test', 'limit': 25}] * 2)
        self.assertTrue(all(c['timeout'] <= 15 for c in self.calls('get_recent_messages')))
        self.assertIn('MEDIA-ENRICH-FAILED', self.log())
        self.assertFalse((self.state / 'auth_failed').exists())
        self.assertFalse(old.exists()); self.assertTrue(fresh.exists())
        self.scenario(media=[{}])
        url = 'mxc://server/image?x=a&b=c'
        output = self.run_script('media.sh', [url], rc=0, last=None)
        path = Path(output.strip())
        self.assertEqual(path, media / (hashlib.sha1(url.encode()).hexdigest() + '.png'))
        self.assertEqual(path.read_bytes(), bytes.fromhex('89504e470d0a1a0a'))
        self.assertIn('mxc_url=mxc%3A%2F%2Fserver%2Fimage%3Fx%3Da%26b%3Dc', self.calls('media')[-1]['args']['url'])
        self.scenario(media=[{'headers': 'HTTP/1.1 200 OK\r\nContent-Length: 26214400\r\n'}])
        before = set(media.iterdir())
        self.run_script('media.sh', ['mxc://large'], rc=1, last='TOO_LARGE')
        self.assertEqual(set(media.iterdir()), before)
        for existing in (False, True):
            if existing: (self.state / 'auth_failed').write_text('reserved')
            self.scenario(media=[{'rc': 22, 'err': 'HTTP 401 download failed'}])
            output = self.run_script('media.sh', ['mxc://failed'], rc=1, last=None)
            self.assertTrue(output.startswith('DOWNLOAD_FAILED '))
            self.assertEqual((self.state / 'auth_failed').exists(), existing)
            if existing: self.assertEqual((self.state / 'auth_failed').read_text(), 'reserved')
            self.assertEqual(set(media.iterdir()), before)

    def test_15_syntax_and_shellcheck(self):
        files = [str(p) for p in SCRIPTS.glob('*.sh')]
        for script in files:
            subprocess.run([BASH, '-n', script], check=True, capture_output=True)
        shellcheck = shutil.which('shellcheck')
        if shellcheck:
            result = subprocess.run([shellcheck, *files, str(HERE / 'run.sh'), *map(str, (HERE / 'fakes').iterdir())], cwd=HERE.parent.parent, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_16_classifier_whole_tokens_and_status_forms(self):
        samples = {'HTTP 401': 'auth', 'http 403': 'auth', 'STATUS 401': 'auth', 'status 403': 'auth',
                   '401 Unauthorized': 'auth', '403 forbidden': 'auth', 'error: -32001, bad': 'auth',
                   '{"code":-32002}': 'reserved', 'event401': 'other', '4013': 'other', 'HTTP 4013': 'other',
                   'status 4030': 'other', '-320010': 'other', 'id-32001': 'other', 'x-32002x': 'other'}
        for error, expected in samples.items():
            result = subprocess.run([BASH, '-c', 'source "$1"; classify_error "$2"', 'test', str(self.state / 'restart_listener.sh'), error], capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), expected, error)

    def test_17_self_cache_invites_and_failure_coverage(self):
        self.scenario(list_pending_invites=[{'out': {'invites': [{'loop_id': 'inv'}]}}],
                      list_vouches=[{'out': {'vouches': [{'loop_id': 'vouch'}]}}], poll_work=[work(item())])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertEqual(self.calls('accept_invite')[0]['args'], {'loop_id': 'inv'})
        self.assertEqual(self.calls('accept_vouch')[0]['args'], {'loop_id': 'vouch'})
        self.run_script(rc=0, last='ITEMS 1')
        self.assertEqual(len(self.calls('get_self')), 1)
        old = time.time() - 90000; os.utime(self.state / 'self.json', (old, old))
        own = item('self', '@self:test')
        self.scenario(get_self=[{'rc': 1, 'err': 'offline'}], poll_work=[work(own, item('human'))])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertEqual(self.calls('ack')[-1]['args']['ack'], ['self'])
        for tool in ['get_self', 'list_pending_invites', 'accept_invite', 'list_vouches', 'accept_vouch']:
            with self.subTest(tool=tool):
                (self.state / 'self.json').unlink(missing_ok=True)
                rules = {'list_pending_invites': [{'out': {'invites': [{'loop_id': 'x'}]}}],
                         'list_vouches': [{'out': {'vouches': [{'loop_id': 'x'}]}}], tool: [{'rc': 1, 'err': '-32002'}]}
                self.scenario(**rules)
                self.run_script(rc=2, last='AGENT_RESERVED')
                (self.state / 'auth_failed').unlink()

    def test_18_bad_budget_bad_json_and_expired_job(self):
        for args in [['--budget', '59'], ['--budget', '541'], ['--budget', 'x'], ['--budget'], ['--wrong', '60'], ['--budget', '999999999999']]:
            self.run_script(args=args, last='BAD_BUDGET')
        self.assertEqual(self.calls(), [])
        self.scenario(poll_work=[{'raw': 'not json'}, work(item())])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertIn('BAD-JSON', self.log())
        before = len(self.calls())
        self.run_script(age=480)
        self.assertEqual(len(self.calls()), before)

    def test_19_concurrent_replies_and_record_cap(self):
        self.fake_listener()
        self.write_item(); self.write_item(number=2)
        self.scenario(send_reply=[{'delay': .6, 'out': {}, 'assert_recorded': ['event-1']}])
        p1 = self.start('reply.sh', ['1'], body=b'first')
        self.wait_for(lambda: len(self.calls('send_reply')) == 1)
        p2 = self.start('reply.sh', ['2'], body=b'second')
        self.finish(p1, 3, 'NO_WORK')
        self.finish(p2, 3, 'NO_WORK')
        self.assertEqual(len(self.calls('send_reply')), 1)
        self.assertEqual(len(self.calls('ack')), 1)
        self.assertGreaterEqual(self.calls('ack')[0]['at'] - self.calls('send_reply')[0]['at'], .59)
        (self.state / 'replied.json').write_text(json.dumps({'ids': [f'old-{i}' for i in range(500)]}))
        self.write_item(item('last'))
        self.run_script('reply.sh', ['--ack', '1'])
        ids = json.loads((self.state / 'replied.json').read_text())['ids']
        self.assertEqual(len(ids), 500); self.assertEqual(ids[-1], 'last'); self.assertNotIn('old-0', ids)

    def test_20_no_self_fallback_and_agent_mentions(self):
        unknown = item('unknown', '@agent:test'); unknown['messages'][0]['sender_is_agent'] = True
        self.scenario(get_self=[{'rc': 1, 'err': 'unavailable'}], poll_work=[work(unknown)])
        self.run_script(rc=0, last='ITEMS 1')
        mentioned = item('mention', '@agent:test'); mentioned['messages'][0].update(sender_is_agent=True, is_mention=True)
        named = item('named', '@agent:test'); named['messages'][0].update(sender_is_agent=True, body='Hi @self:test')
        self.scenario(poll_work=[work(mentioned, named)])
        self.run_script(rc=0, last='ITEMS 2')

    def test_21_lock_timeouts_and_insufficient_time(self):
        self.fake_listener(); self.write_item()
        lock = self.state / 'reply.lock.d'; lock.mkdir()
        (lock / 'pid').write_text(str(os.getpid()))
        start = time.monotonic()
        self.run_script('reply.sh', ['1'], body=b'hello')
        self.assertLess(time.monotonic() - start, 8)
        self.assertIn('LOCK-TIMEOUT', self.log())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / 'replied.json').exists())
        self.assertTrue((self.state / 'items/1.json').exists())
        shutil.rmtree(lock)
        self.run_script('reply.sh', ['1'], age=475, body=b'hello')
        self.assertIn('DEADLINE', self.log())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / 'replied.json').exists())
        lock = self.state / 'restart.lock.d'; lock.mkdir()
        (lock / 'pid').write_text(str(os.getpid()))
        self.run_script('restart_listener.sh', last='RESTART_BUSY')

    def test_22_corrupt_inputs_and_state_fail_closed(self):
        self.fake_listener(); self.write_item()
        for raw in ['not JSON', '[]', '{"messages":null}', '{"messages":[],"reply_with":null}']:
            (self.state / 'items/1.json').write_text(raw)
            output = self.run_script('reply.sh', ['1'], body=b'hello')
            self.assertEqual(output.splitlines(), ['BAD_ITEM', 'NO_WORK'])
        self.write_item()
        (self.state / 'replied.json').write_text('corrupt')
        self.run_script('reply.sh', ['1'], body=b'hello')
        self.assertIn('STATE-ERROR', self.log())
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.state / 'replied.json').read_text(), 'corrupt')
        self.assertTrue((self.state / 'items/1.json').exists())

    def test_23_filtered_ack_errors_and_heartbeat_failure(self):
        self.scenario(heartbeat=[{'rc': 1, 'err': 'HTTP 401'}],
                      poll_work=[work(item('god', '@filament_god:test'), item('human'))],
                      ack=[{'rc': 1, 'err': 'HTTP 403'}])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertIn('HEARTBEAT-FAILED rc=1', self.log())
        self.assertIn('FILTER-ACK-FAILED rc=1', self.log())
        self.assertFalse((self.state / 'auth_failed').exists())

    def test_24_reply_ack_auth_and_null_only_polls(self):
        self.fake_listener(); self.write_item()
        self.scenario(ack=[{'rc': 1, 'err': '-32002', 'assert_recorded': ['event-1']}])
        self.run_script('reply.sh', ['--ack', '1'], rc=2, last='AGENT_RESERVED')
        self.assertIn('ACKED rc=1', self.log())
        self.assertEqual(len(self.calls('send_reply')), 0)
        (self.state / 'auth_failed').unlink()
        shutil.copy2(SCRIPTS / 'listen.sh', self.state / 'listen.sh')
        null = item(); null['reply_with'] = None
        self.scenario(poll_work=[work(null), work(item('new'))])
        before = len(self.calls('ack'))
        self.run_script(rc=0, last='ITEMS 1')
        self.assertEqual(len(self.calls('ack')), before)

    def test_25_media_enrichment_and_cleanup(self):
        media = [{'mxc_url': 'mxc://server/image'}]
        target = item()
        self.scenario(poll_work=[work(item('god', '@filament_god:test'), target, item('second'))],
                      get_recent_messages=[{'out': {'messages': [
                          {'event_id': 'unrelated', 'media': []},
                          {'event_id': 'event-1', 'media': media, 'msgtype': 'm.image'}]}},
                                           {'rc': 1, 'err': 'HTTP 403'}])
        directory = self.state / 'media'; directory.mkdir()
        old, fresh = directory / 'old.png', directory / 'fresh.png'
        old.write_bytes(b'old'); fresh.write_bytes(b'fresh')
        stamp = time.time() - 3700; os.utime(old, (stamp, stamp))
        output = self.run_script(rc=0, last='ITEMS 2')
        target['messages'][0].update(media=media, msgtype='m.image')
        delivered = json.loads(output.splitlines()[0])
        self.assertEqual(delivered['work'], [target, item('second')])
        self.assertEqual(json.loads((self.state / 'items/1.json').read_text()), target)
        self.assertEqual(json.loads((self.state / 'items/wake.json').read_text()), delivered)
        self.assertEqual([c['args'] for c in self.calls('get_recent_messages')],
                         [{'channel': '!room:test', 'limit': 25}] * 2)
        self.assertTrue(all(c['timeout'] == 15 for c in self.calls('get_recent_messages')))
        self.assertIn('MEDIA-ENRICH-FAILED', self.log())
        self.assertFalse((self.state / 'auth_failed').exists())
        self.assertFalse(old.exists()); self.assertTrue(fresh.exists())
        self.scenario(poll_work=[work(item())], get_recent_messages=[{'raw': 'bad JSON'}])
        self.run_script(rc=0, last='ITEMS 1')
        self.assertIn('MEDIA-ENRICH-FAILED', (self.state / 'listen.log').read_text())

    def test_26_media_download(self):
        import hashlib
        from urllib.parse import quote
        url = 'mxc://server/image?name=a & b'
        self.scenario(media=[{}])
        output = self.run_script('media.sh', [url], rc=0, last=None)
        target = self.state / 'media' / (hashlib.sha1(url.encode()).hexdigest() + '.png')
        self.assertEqual(output.strip(), str(target))
        self.assertEqual(target.read_bytes(), b'\x89PNG\r\n\x1a\n')
        self.assertEqual(self.calls('media')[0]['args']['url'],
                         'https://api.filament.dm/mcp/agents/media?mxc_url=' + quote(url, safe=''))
        target.unlink()
        self.scenario(media=[{'content_length': 25 * 1024 * 1024}])
        self.run_script('media.sh', [url], rc=1, last='TOO_LARGE')
        self.assertEqual(list((self.state / 'media').iterdir()), [])
        self.scenario(media=[{'rc': 22, 'err': 'HTTP 401'}])
        for marker in (None, 'reserved'):
            if marker: (self.state / 'auth_failed').write_text(marker)
            output = self.run_script('media.sh', [url], rc=1, last=None)
            self.assertTrue(output.startswith('DOWNLOAD_FAILED '))
            self.assertEqual(list((self.state / 'media').iterdir()), [])
            if marker: self.assertEqual((self.state / 'auth_failed').read_text(), marker)
            else: self.assertFalse((self.state / 'auth_failed').exists())

if __name__ == '__main__':
    print('Bash: ' + subprocess.check_output([BASH, '-c', 'printf "%s" "$BASH_VERSION"'], text=True), flush=True)
    print('Network: fake mcpcall and curl only; real epoch deadlines', flush=True)
    print('Process identity: ' + ('native ps' if NATIVE_PS else 'test catalogue (sandbox blocks ps); native ps unverified'), flush=True)
    print('ShellCheck: ' + ('enabled' if shutil.which('shellcheck') else 'not on PATH; skipped'), flush=True)
    unittest.main(verbosity=2)
