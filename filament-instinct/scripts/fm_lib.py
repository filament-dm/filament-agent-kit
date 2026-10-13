"""Shared, stdlib-only Filament transport and state machinery (Python 3.9+)."""
import contextlib
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

STATE = Path(os.environ.get('FM_STATE', '~/.filament')).expanduser().absolute()
H = str(STATE)  # harvested public name
EP = os.environ.get('FM_ENDPOINT', 'https://api.filament.dm/mcp/agents').rstrip('/')
FAST = os.environ.get('FM_TEST_FAST') == '1'
# Do not leave bytecode next to scripts outside STATE.
sys.dont_write_bytecode = True


def token():
    return (STATE / 'token').read_text().strip()


def ts():
    return datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def rd(name, default=''):
    try:
        return (STATE / name).read_text().strip()
    except FileNotFoundError:
        return default


def wr(name, val, mode=0o600):
    """Fsync a private temporary file, then atomically replace its destination."""
    path = STATE / name
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(str(val))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def remove(name):
    with contextlib.suppress(FileNotFoundError):
        (STATE / name).unlink()


@contextlib.contextmanager
def state_guard():
    """Short filesystem transactions; never held across a network call.

    flock serialises directory takeover/claim and ledger read-modify-write. It
    complements mkdir leases and prevents two stale-lock removers winning.
    """
    with open(STATE / '.state.guard', 'a') as f:
        os.chmod(STATE / '.state.guard', 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def redact(value, secret=None):
    value = str(value)
    secrets = [secret] if secret else []
    try:
        secrets.append(token())
    except OSError:
        pass
    for s in secrets:
        if s:
            value = value.replace(s, '[REDACTED]')
    return value


def log(line, name='listener.log'):
    # Server-controlled text must not forge a second log line or leak a token.
    line = redact(line).replace('\r', '\\r').replace('\n', '\\n')
    with open(STATE / name, 'a', encoding='utf-8') as f:
        f.write(ts() + ' ' + line[:2000] + '\n')


def rotate_logs():
    with state_guard():
        for name in ('listener.log', 'reply.log', 'listener.out'):
            p = STATE / name
            try:
                if p.stat().st_size <= 512 * 1024:
                    continue
                with p.open('rb') as f:
                    f.seek(-64 * 1024, 2)
                    tail = f.read().split(b'\n', 1)[-1]
                # Preserve the inode: detached stdout may already have it open.
                with p.open('wb') as f:
                    f.write(tail)
            except FileNotFoundError:
                pass


def sleep(seconds):
    # D21's floor applies to integer protocol waits, not subsecond sleeps.
    time.sleep(max(0, seconds) / (10 if FAST else 1))


def alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def lock_read(name='lock.d'):
    try:
        pid, stamp = rd(name + '/owner').split()
        return max(0, time.time() - float(stamp)), int(pid)
    except (ValueError, OSError):
        return None, None


def handoff_fresh():
    try:
        nonce, stamp = rd('lock.d/handoff').split()
        return bool(nonce) and 0 <= time.time() - float(stamp) < 10
    except (ValueError, OSError):
        return False


def lock_alive(name='lock.d', tick=False):
    age, pid = lock_read(name)
    if name == 'lock.d' and (STATE / 'lock.d/handoff').exists():
        if not handoff_fresh():
            return False
        if tick:
            return True
    return age is not None and age < 120 and alive(pid)


class LockLost(Exception):
    pass


def lock_write(name='lock.d'):
    with state_guard():
        _, pid = lock_read(name)
        if pid != os.getpid():
            raise LockLost('lock ownership lost')
        wr(name + '/owner', '{} {}'.format(os.getpid(), time.time()))


def lock_take(name='lock.d', handoff=None):
    with state_guard():
        p = STATE / name
        if handoff is not None:
            parts = rd(name + '/handoff').split()
            if len(parts) != 2 or parts[0] != handoff or not handoff_fresh():
                return False
            wr(name + '/owner', '{} {}'.format(os.getpid(), time.time()))
            remove(name + '/handoff')
            return True
        if p.exists():
            if lock_alive(name, tick=True):
                return False
            log('STALE-LOCK ' + name)
            shutil.rmtree(p)
        try:
            p.mkdir(mode=0o700)
        except FileExistsError:
            return False
        wr(name + '/owner', '{} {}'.format(os.getpid(), time.time()))
        return True


def lock_release(name='lock.d'):
    with state_guard():
        if lock_read(name)[1] == os.getpid():
            shutil.rmtree(STATE / name)


@contextlib.contextmanager
def reply_lock():
    deadline = time.monotonic() + 30
    while not lock_take('reply.lock.d'):
        if time.monotonic() >= deadline:
            raise TimeoutError('reply lock busy')
        sleep(0.1)
    try:
        yield
    finally:
        lock_release('reply.lock.d')


def ids_add(name, ids, limit=None):
    # Caller holds state_guard.
    all_ids = list(dict.fromkeys(rd(name).split() + list(ids)))
    if limit:
        all_ids = all_ids[-limit:]
    wr(name, ('\n' if name == 'replied_ids' else ' ').join(all_ids))


def record(ids, ack=False):
    with state_guard():
        ids_add('replied_ids', ids, 2000)
        if ack:
            ids_add('pending_acks', ids)


class NetErr(Exception):
    pass


class ToolErr(Exception):
    pass


class AuthErr(Exception):
    def __init__(self, message, code=None, token_hash=None):
        super().__init__(message)
        self.code = code
        self.token_hash = token_hash


def classify_auth(err):
    code = getattr(err, 'code', None)
    if code == -32002:
        return 'reserved'
    if code in (-32001, 401, 403):
        return 'auth'
    value = str(err)
    if re.search(r'(?<!\w)-32002(?!\w)', value):
        return 'reserved'
    if re.search(r'(?<!\w)(?:-32001|401|403)(?!\w)', value):
        return 'auth'
    return None


def token_hash():
    return hashlib.sha256(token().encode()).hexdigest()


def mark_dead(err):
    data = {'at': ts(), 'kind': classify_auth(err) or 'auth',
            'sha256': err.token_hash, 'error': redact(err)}
    with state_guard():
        # A response to an old credential must not poison its replacement.
        if data['sha256'] == token_hash():
            wr('token_dead', json.dumps(data))


def dead_marker():
    # Caller holds state_guard.
    if not rd('token_dead'):
        return None
    try:
        data = json.loads(rd('token_dead'))
        if data.get('sha256') != token_hash():
            remove('token_dead')
            return None
        return data
    except (ValueError, OSError):
        return {'kind': 'auth'}


def _parse(body):
    try:
        return json.loads(body)
    except ValueError:
        # SSE events may contain several data lines and keepalive/[DONE] events.
        events = re.split(r'\r?\n\r?\n', body)
        for event in reversed(events):
            data = '\n'.join(line[5:].lstrip() for line in event.splitlines()
                             if line.startswith('data:'))
            try:
                result = json.loads(data)
                if isinstance(result, dict) and ('result' in result or 'error' in result):
                    return result
            except ValueError:
                pass
        raise ValueError('malformed JSON')


def call(tool, args, timeout=30, before=None):
    if before:
        before()
    try:
        secret = token()
    except OSError:
        raise NetErr('token unavailable')
    digest = hashlib.sha256(secret.encode()).hexdigest()
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': tool, 'arguments': args}}).encode()
    req = urllib.request.Request(EP, data=payload, method='POST', headers={
        'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthErr('HTTP {}'.format(e.code), e.code, digest)
        raise NetErr('HTTP {}'.format(e.code))
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise NetErr(type(e).__name__)
    try:
        result = _parse(body)
        if 'error' in result:
            code = result['error'].get('code')
            message = redact('RPC {}: {}'.format(code, result['error'].get('message')), secret)
            if code in (-32001, -32002):
                raise AuthErr(message, code, digest)
            raise ToolErr(message)
        res = result['result']
        text = res['content'][0]['text'] if res.get('content') else '{}'
        if len(text) > 2000:
            wr('last_result.json', redact(text, secret))
        if res.get('isError'):
            raise ToolErr(redact(text, secret))
        return json.loads(text)
    except (ValueError, KeyError, TypeError, IndexError):
        raise NetErr('malformed JSON')


def heartbeat(before=None):
    if before:
        before()
    try:
        req = urllib.request.Request(EP + '/heartbeat', data=b'', method='POST',
                                     headers={'Authorization': 'Bearer ' + token()})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError, http.client.HTTPException) as e:
        return 'ERR ' + type(e).__name__


def spawn(script, env=None):
    with open(STATE / 'listener.out', 'ab') as out:
        return subprocess.Popen([sys.executable, '-B', str(STATE / script)],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True, env=env)


def batch_read():
    try:
        return json.loads(rd('last_work.json', '{}'))
    except ValueError:
        return {}


def batch_gate():
    """Caller holds state_guard. Return age when work must not be polled over."""
    handling = STATE / 'handling'
    if handling.exists():
        age = time.time() - handling.stat().st_mtime
        if age < 600:
            return age
        log('STALE-BATCH')
        for name in ('handling', 'pending_cursor', 'run.out', 'run.out.shown', 'work.ready'):
            remove(name)
        return None
    if rd('pending_cursor'):
        batch = batch_read()
        # Published, unclaimed work remains available until a handler claims it.
        if rd('run.out').startswith('BATCH ' + str(batch.get('batch_id')) + '\n'):
            return max(0, time.time() - (STATE / 'pending_cursor').stat().st_mtime)
        age = time.time() - (STATE / 'pending_cursor').stat().st_mtime
        if age < 600:
            return age
        log('STALE-BATCH unpublished')
        remove('pending_cursor')
    return None


def failures_read():
    return [json.loads(line) for line in rd('failed_replies').splitlines() if line]


def failures_write(rows):
    wr('failed_replies', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def notify_failures():
    """Caller holds reply.lock.d. Notices are retried; original replies never are."""
    rows = failures_read()
    for row in rows:
        if row['notified'] == 'yes':
            continue
        try:
            call('message_principal', {'markdown_body': row['notice']}, timeout=30,
                 before=lambda: lock_write('reply.lock.d'))
            row['notified'] = 'yes'
        except AuthErr as e:
            mark_dead(e)
            log('failure notice not sent ' + str(e), 'reply.log')
        except (NetErr, ToolErr) as e:
            log('failure notice not sent ' + str(e), 'reply.log')
        failures_write(rows)


def unnotified():
    return sum(r['notified'] == 'no' for r in failures_read())
