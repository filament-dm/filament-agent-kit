#!/usr/bin/env python3
"""Wait for Filament work without the model.

The bot's turn makes one shell call to this script. The script calls
poll_work over HTTP in a loop and returns only when there is work, when its
time is up, or when something needs the bot. While it waits, the model is
idle and spends no tokens.

The first line of output is always one verb:

  WORK n        n items follow as JSON. Handle them, then run with --done.
  AGAIN         this call's time is up, the run's is not. Run with --again.
  END           the run is over. End the run without a report.
  RUNNING       another run is listening. End the run without a report.
  BUSY          another run is handling work. End the run without a report.
  DISCONNECTED  Filament has been unreachable for 15 minutes. Tell the owner
                once, then end the run. Later runs print END until it is back.
  AUTH_FAILED   the token was revoked or the agent deleted. Stop for good.
  TOKEN_SET     --token stored the token.
  ERROR <why>   a local problem (no token, bad arguments).

Python 3.6+ standard library only. State lives in FM_STATE
(default /workspace/filament): token, endpoint, cursor, acks, session,
handling, fail_since, notified, listen.lock/, listen.log.
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

STATE = os.environ.get('FM_STATE', '/workspace/filament')
DEFAULT_ENDPOINT = 'https://api.filament.dm/mcp/agents'
POLL_WAIT = 60          # seconds; the server caps wait_seconds at 60
HTTP_TIMEOUT = 90       # a poll still open after this is abandoned
BEAT_STALE = 150        # a listener silent this long is dead
HANDLING_STALE = 1200   # a turn handling work this long is presumed dead
DISCONNECT_AFTER = int(os.environ.get('FM_DISCONNECT_AFTER', '900'))  # failing this long = disconnected
BACKOFF_CAP = 600


class AuthFailed(Exception):
    pass


class Transient(Exception):
    pass


def path(name):
    return os.path.join(STATE, name)


def read(name, default=None):
    try:
        with open(path(name)) as f:
            return f.read().strip()
    except OSError:
        return default


def write(name, text, mode=0o600):
    tmp = path(name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, path(name))


def remove(name):
    try:
        os.remove(path(name))
    except OSError:
        pass


def age(name):
    try:
        return time.time() - os.stat(path(name)).st_mtime
    except OSError:
        return None


def log(line):
    try:
        if (os.path.getsize(path('listen.log')) if os.path.exists(path('listen.log')) else 0) > 512 * 1024:
            os.replace(path('listen.log'), path('listen.log.1'))
        with open(path('listen.log'), 'a') as f:
            f.write('%s %d %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), os.getpid(), line))
    except OSError:
        pass


def say(verb, body=None):
    print(verb)
    if body is not None:
        print(body)
    sys.stdout.flush()


# ---- the lock: one listener at a time ------------------------------------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def lock():
    """Take listen.lock/. Return False when a live listener holds it."""
    lock_dir = path('listen.lock')
    for _ in range(2):
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            try:
                pid = int(read('listen.lock/pid', '0') or 0)
            except ValueError:
                pid = 0
            silent = age('listen.lock/beat')
            if silent is None:
                silent = age('listen.lock')
            if pid and pid_alive(pid) and silent is not None and silent < BEAT_STALE:
                return False
            log('LOCK-STALE pid=%s silent=%s' % (pid, silent))
            shutil.rmtree(lock_dir, ignore_errors=True)
            continue
        write('listen.lock/pid', str(os.getpid()))
        beat()
        return True
    return False


def beat():
    write('listen.lock/beat', str(int(time.time())))


def unlock():
    if read('listen.lock/pid') == str(os.getpid()):
        shutil.rmtree(path('listen.lock'), ignore_errors=True)


# ---- one poll_work call ---------------------------------------------------

def parse(body):
    body = body.strip()
    if body.startswith('{'):
        return json.loads(body)
    # Server-sent events: take the last data: payload that is a JSON-RPC reply.
    for event in reversed(body.split('\n\n')):
        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        if not data:
            continue
        try:
            reply = json.loads(data)
        except ValueError:
            continue
        if isinstance(reply, dict) and ('result' in reply or 'error' in reply):
            return reply
    raise ValueError('no JSON-RPC reply in body')


def poll(cursor, acks, wait):
    token = read('token')
    args = {'wait_seconds': wait}
    if cursor:
        args['cursor'] = cursor
    if acks:
        args['ack'] = acks
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'poll_work', 'arguments': args}}).encode()
    request = urllib.request.Request(read('endpoint') or DEFAULT_ENDPOINT, data=payload, method='POST', headers={
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthFailed('HTTP %d' % e.code)
        raise Transient('HTTP %d' % e.code)
    except Exception as e:  # timeouts, DNS, refused, reset
        raise Transient(type(e).__name__)
    try:
        reply = parse(body)
    except ValueError as e:
        raise Transient('bad body: %s' % e)
    if 'error' in reply:
        # A JSON-RPC error inside HTTP 200 (for example, the owner paused the
        # agent) is not a disconnection: keep polling.
        raise Transient('RPC %s: %s' % (reply['error'].get('code'), reply['error'].get('message')))
    result = reply.get('result') or {}
    text = result['content'][0]['text'] if result.get('content') else '{}'
    if result.get('isError'):
        raise Transient('tool error: %s' % text[:300])
    try:
        return json.loads(text)
    except ValueError:
        raise Transient('tool returned non-JSON')


# ---- the listener ---------------------------------------------------------

def pending_acks():
    try:
        return json.loads(read('acks', '[]'))
    except ValueError:
        return []


def listen(budget, session_max):
    started = time.time()
    try:
        session_start = float(read('session', '0'))
    except ValueError:
        session_start = 0.0
    if not session_start:
        session_start = started
        write('session', str(session_start))
    deadline = min(started + budget, session_start + session_max)
    session_over = session_start + session_max <= started

    wait_s = 1
    while True:
        beat()
        acks = pending_acks()
        # Past the deadline, still make one non-blocking poll when acks are
        # owed: they must reach the server, and it may hand back new work.
        remaining = deadline - time.time()
        if remaining <= 0 and not acks:
            break
        wait = 0 if session_over or remaining <= 0 else max(0, min(POLL_WAIT, int(remaining)))
        try:
            response = poll(read('cursor'), acks, wait)
        except AuthFailed as e:
            log('AUTH_FAILED %s' % e)
            return 'AUTH_FAILED', str(e)
        except Transient as e:
            fail_since = read('fail_since')
            if not fail_since:
                write('fail_since', str(time.time()))
                fail_since = str(time.time())
            log('POLL-FAILED %s retry_in=%ds' % (e, wait_s))
            if time.time() - float(fail_since) >= DISCONNECT_AFTER and not read('notified'):
                write('notified', '1')
                return 'DISCONNECTED', str(e)
            if time.time() + wait_s >= deadline:
                break
            time.sleep(wait_s)
            wait_s = min(wait_s * 2, BACKOFF_CAP)
            continue

        wait_s = 1
        remove('fail_since')
        remove('notified')
        if response.get('cursor'):
            write('cursor', response['cursor'])
        if acks:
            left = [a for a in pending_acks() if a not in acks]
            write('acks', json.dumps(left)) if left else remove('acks')
        work = response.get('work') or []
        if work:
            write('handling', str(time.time()))
            log('WORK n=%d' % len(work))
            return 'WORK %d' % len(work), json.dumps(work, ensure_ascii=False, indent=1)
        if session_over or time.time() >= deadline:
            break
        time.sleep(min(max(response.get('next_poll_ms', 1000), 0), 5000) / 1000.0)

    # The session, not this call's budget, set the deadline: the run is over.
    if session_over or deadline >= session_start + session_max:
        remove('session')
        log('END')
        return 'END', None
    log('AGAIN')
    return 'AGAIN', None


def main():
    parser = argparse.ArgumentParser(description='Wait for Filament work without the model.')
    parser.add_argument('--token', action='store_true', help='read the connect token from stdin and store it')
    parser.add_argument('--endpoint', help='store a non-default MCP URL')
    parser.add_argument('--done', action='store_true', help='finished handling the last WORK')
    parser.add_argument('--again', action='store_true', help='continue after AGAIN')
    parser.add_argument('--ack', default='', help='comma-separated event ids to skip')
    parser.add_argument('--budget', type=int, default=int(os.environ.get('FM_BUDGET', '540')),
                        help='longest this call may block, in seconds')
    parser.add_argument('--session', type=int, default=int(os.environ.get('FM_SESSION', '840')),
                        help='longest one run listens, in seconds')
    args = parser.parse_args()

    os.makedirs(STATE, mode=0o700, exist_ok=True)

    if args.token or args.endpoint:
        if args.token:
            token = sys.stdin.readline().strip()
            if not token.startswith('fmcp_'):
                return say('ERROR the token must start with fmcp_')
            write('token', token)
        if args.endpoint:
            write('endpoint', args.endpoint.strip())
        return say('TOKEN_SET')

    acks = [a.strip() for a in args.ack.split(',') if a.strip()]
    continuing = args.done or args.again

    if not read('token'):
        return say('ERROR no token; run with --token first')
    if acks:
        # Written before the lock: whichever listener polls next sends them.
        write('acks', json.dumps(sorted(set(pending_acks() + acks))))

    handling = age('handling')
    if handling is not None and not args.done:
        if handling < HANDLING_STALE:
            return say('BUSY')
        log('HANDLING-STALE age=%ds' % handling)
    if not lock():
        return say('RUNNING')
    try:
        remove('handling')
        if not continuing:
            remove('session')  # a new run starts a new session
        verb, body = listen(args.budget, args.session)
        if verb == 'DISCONNECTED' or verb == 'AUTH_FAILED':
            remove('session')
        say(verb, body)
    finally:
        unlock()


if __name__ == '__main__':
    main()
