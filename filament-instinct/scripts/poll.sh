#!/usr/bin/env bash
# Bash 3.2 bootstrap. Python keeps state, quoting and POSIX behaviour portable.
# Release pin replaces PINNED below with {filename: [immutable_url, sha256]} for
# the five Python scripts. The bootstrap hash lives in persistent tick text.
# An optional poll.sh pin can upgrade to a separately pinned successor bootstrap.
# Empty means unreleased: install fails closed.
# Never fill this from an unverified remote manifest or from an environment value.
exec python3 -B - "$@" 3<&0 <<'PY'
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import urllib.request

# PINNED_BEGIN
PINNED = {}  # RELEASE_PIN: URLs and sha256 are embedded here at release time.
# PINNED_END
STATE = Path(os.environ.get('FM_STATE', '~/.filament')).expanduser().absolute()
args = sys.argv[1:]


def install():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Standalone bootstrap: no fm_lib exists after a rebuild.
    with open(STATE / '.state.guard', 'a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        for name in ('listener.log', 'reply.log', 'listener.out'):
            path = STATE / name
            if path.exists() and path.stat().st_size > 512 * 1024:
                with path.open('rb') as f:
                    f.seek(-64 * 1024, 2)
                    tail = f.read().split(b'\n', 1)[-1]
                with path.open('wb') as f:
                    f.write(tail)
        lease = STATE / 'install.lock.d'
        if lease.exists():
            try:
                pid, stamp = (lease / 'owner').read_text().split()
                os.kill(int(pid), 0)
                live = time.time() - float(stamp) < 120
            except (OSError, ValueError):
                live = False
            if live:
                print('INSTALL_FAILED lock busy')
                return 1
            shutil.rmtree(lease)
        lease.mkdir(mode=0o700)
        (lease / 'owner').write_text('{} {}'.format(os.getpid(), time.time()))
        fcntl.flock(guard, fcntl.LOCK_UN)
    stage = STATE / 'install.tmp'
    current = 'manifest'
    try:
        names = {'fm_lib.py', 'fm_poll.py', 'fm_reply.py', 'fm_hb.py', 'fm_media.py'}
        if not names <= set(PINNED) or not set(PINNED) <= names | {'poll.sh'}:
            raise ValueError('release pin must contain the five Python files')
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(mode=0o700)
        for current, (url, digest) in PINNED.items():
            with open(STATE / '.state.guard', 'a') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                if (lease / 'owner').read_text().split()[0] != str(os.getpid()):
                    raise ValueError('install lock ownership lost')
                (lease / 'owner.tmp').write_text('{} {}'.format(os.getpid(), time.time()))
                os.replace(lease / 'owner.tmp', lease / 'owner')
            # The release owns these URLs; credentials are never attached.
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                raise ValueError('file too large')
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('sha256 mismatch')
            with open(stage / current, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(stage / current, 0o700)
        # Preserve originals so an ordinary rename error can roll back too.
        originals = {name: ((STATE / name).read_bytes(), (STATE / name).stat().st_mode & 0o777)
                     if (STATE / name).exists() else None for name in PINNED}
        installed = []
        try:
            for current in PINNED:
                os.replace(stage / current, STATE / current)
                installed.append(current)
        except OSError:
            for name in installed:
                if originals[name] is None:
                    (STATE / name).unlink()
                else:
                    data, mode = originals[name]
                    (stage / name).write_bytes(data)
                    os.chmod(stage / name, mode)
                    os.replace(stage / name, STATE / name)
            raise
        print('INSTALLED')
        return 0
    except Exception as e:
        # URLs/errors may contain credentials; print only controlled diagnostics.
        reason = str(e) if isinstance(e, ValueError) else type(e).__name__
        print('INSTALL_FAILED {} {}'.format(current, reason))
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        with open(STATE / '.state.guard', 'a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                if (lease / 'owner').read_text().split()[0] == str(os.getpid()):
                    shutil.rmtree(lease)
            except FileNotFoundError:
                pass


if args == ['--install']:
    sys.exit(install())

# stdin is occupied by this embedded program. Bash passes the caller's stdin
# separately on fd 3 (see final heredoc redirection below).
sys.path[0] = str(STATE)
try:
    from fm_lib import *
except ImportError:
    print('INSTALL_FAILED fm_lib.py run --install first')
    sys.exit(1)


def failure_line():
    count = unnotified()
    if count:
        print('FAILED_UNNOTIFIED ' + str(count))


def plain():
    failure_line()
    with state_guard():
        dead = dead_marker()
    if dead:
        if dead.get('kind') != 'reserved':
            print('TOKEN_DEAD auth')
            return 0
        try:
            call('get_self', {}, timeout=15)
        except (AuthErr, NetErr, ToolErr) as e:
            if isinstance(e, AuthErr):
                mark_dead(e)
            print('AGENT_RESERVED')
            return 0
        with state_guard():
            # Do not clear a newer auth failure written during the probe.
            if dead_marker() == dead:
                remove('token_dead')
    with state_guard():
        age = batch_gate()
        batch = batch_read()
        bid = batch.get('batch_id')
        out = rd('run.out')
        if (bid and out.startswith('BATCH ' + bid + '\n') and '\nANSWER ' in out
                and out.endswith('run complete: WORK') and rd('pending_cursor')
                and not rd('handling') and rd('run.out.shown') != bid):
            wr('handling', bid)
            wr('run.out.shown', bid)
            remove('work.ready')
            print(out)
            return 0
        if age is not None:
            print('BATCH_IN_HAND {}s'.format(int(age)))
            return 0
        delay = float(rd('next_try', '0') or 0) - time.time()
        if delay > 0:
            print('BACKOFF {:.1f}'.format(delay))
            return 0
        if lock_alive(tick=True):
            print('LISTENING age={}'.format(int(lock_read()[0] or 0)))
            return 0
        remove('run.out.shown')
        # Multiple tick spawns are harmless: the listener's mkdir chooses one.
        env = dict(os.environ)
        env.pop('FM_HANDOFF', None)
        spawn('fm_poll.py', env)
    sleep(4)
    print('STARTED')
    return 0


def commit(force):
    with reply_lock():
        with state_guard():
            # A late handler cannot revive an expired or replaced batch.
            if batch_gate() is None or not rd('handling') or not rd('pending_cursor'):
                print('NOTHING_TO_COMMIT')
                return 0
            batch = batch_read()
            if batch.get('batch_id') != rd('handling'):
                print('STALE_BATCH')
                return 2
            (STATE / 'handling').touch()
            done = set(rd('replied_ids').split())
            unhandled = [str(it['n']) for it in batch.get('items', [])
                         if any(m.get('event_id') not in done for m in it['messages'])]
            if unhandled and not force:
                print('UNHANDLED n=' + ','.join(unhandled))
                return 1
            wr('cursor', rd('pending_cursor'))
            for name in ('pending_cursor', 'handling', 'run.out.shown', 'work.ready', 'run.out'):
                remove(name)
            log('commit')
    print('COMMITTED')
    return plain()


def status(notify):
    if notify:
        with reply_lock():
            notify_failures()
    with state_guard():
        dead = dead_marker()
        age, pid = lock_read()
        lock = 'alive {}s'.format(int(age or 0)) if lock_alive(tick=True) else 'none'
        pending = 'none'
        if (STATE / 'handling').exists():
            pending = '{}s'.format(int(time.time() - (STATE / 'handling').stat().st_mtime))
        elif rd('pending_cursor'):
            pending = '{}s'.format(int(time.time() - (STATE / 'pending_cursor').stat().st_mtime))
        delay = max(0, float(rd('next_try', '0') or 0) - time.time())
        poll = next((line[:23].strip() for line in reversed(rd('listener.log').splitlines())
                     if ' poll_work ok ' in line), 'none')
        work = batch_read().get('at', 'none')
        hb = int(rd('hb.pid', '0') or 0)
        print('token={} lock={} pending={} backoff={:.1f} last_poll={} last_work={} hb={}'.format(
            'dead' if dead else 'ok', lock, pending, delay, poll, work, hb if alive(hb) else 'none'))
    failure_line()
    return 0


def stop():
    # Keep the lease in place until the owner has stopped, preventing tick races.
    with state_guard():
        _, pid = lock_read()
        if alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + (1 if FAST else 10)
    while alive(pid) and time.monotonic() < deadline:
        sleep(0.1)
    with state_guard():
        if pid and lock_read()[1] == pid:
            if alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            shutil.rmtree(STATE / 'lock.d', ignore_errors=True)
    print('STOPPED')
    return 0


def main():
    rotate_logs()
    if not args:
        return plain()
    if args in (['--commit'], ['--commit', '--force']):
        return commit('--force' in args)
    if args in (['--status'], ['--status', '--notify']):
        return status('--notify' in args)
    if args == ['--token']:
        with os.fdopen(3, 'r', encoding='utf-8') as source:
            value = source.read().strip()
        if not value or '\n' in value or '\r' in value:
            print('TOKEN_FAILED expected one line on stdin')
            return 2
        with state_guard():
            wr('token', value, mode=0o600)
            remove('token_dead')
        print('TOKEN_SET')
        return 0
    if args == ['--stop']:
        return stop()
    print('BAD_ARGS: --token reads stdin; see README.md')
    return 2


try:
    sys.exit(main())
except TimeoutError:
    print('BACKOFF reply lock busy')
    sys.exit(1)
PY
