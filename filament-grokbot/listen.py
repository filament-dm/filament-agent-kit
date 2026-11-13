#!/usr/bin/env python3
"""Short Filament checks with a follow-up window, durable work and diagnostics.

A start (Filament's webhook wake, or the backstop schedule) checks briefly and
ends. Handled work opens a follow-up window so a conversation continues
without waiting for another start. --wake registers the routine's webhook
trigger with Filament, which then starts a run when work arrives and nobody is
listening.
Python owns empty polls; the model handles WORK. --update installs the guide
version Filament released and reports it, so an update is this script's job,
not the model's. This script cannot measure model tokens or guarantee provider
scheduling. State is private to one agent. Only --status and --check-state are
read-only operations.
"""
import argparse
import contextlib
import re
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Kept equal to the guide version in GROKBOT.md, so listeners older than
# 2026-11-13.1, which require it, can still update. This one never requires it.
REVISION = '2026-11-13.2'
STATE = os.environ.get('FM_STATE') or os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENDPOINT = 'https://api.filament.dm/mcp/agents'
POLL_WAIT = 60
HTTP_SLACK = 30
DISCONNECT_AFTER = int(os.environ.get('FM_DISCONNECT_AFTER', '900'))
# No Grok execution has been observed past about 50 minutes, so a batch this
# old has no live handler. Younger batches are left to their handler.
HANDLER_TIMEOUT = int(os.environ.get('FM_HANDLER_TIMEOUT', '3000'))
LOCK_WAIT = float(os.environ.get('FM_LOCK_WAIT', '15'))
WAKE_HOSTS = ('api2.cursor.sh',)
WAKE_KEY = re.compile(r'[A-Za-z0-9._~+/=-]{1,512}')
# Filament grants a subscription for up to a week; renew well before that.
WAKE_REFRESH_AFTER = int(os.environ.get('FM_WAKE_REFRESH_AFTER', '86400'))
BACKOFF_CAP = 600
LOG_LIMIT = 512 * 1024
# A fresh run sees no earlier turns, so each work item carries the last few.
# poll_work does not deliver them yet; an item that already has context keeps it.
CONTEXT_MESSAGES = 10
CONTEXT_ITEMS = 5
CONTEXT_BUDGET = 20
CONTEXT_FIELDS = ('sender', 'body', 'timestamp', 'is_from_self', 'is_from_principal', 'has_media')
CONTEXT_NOTE = ('Earlier messages in this conversation, oldest first, including your own replies. '
                'They were already handled: read them for context only; never answer or acknowledge them.')
GUIDE_ID = 'grokbot'
# The only URLs an update downloads: the released guide, then the listener and
# the routine protocol from the same branch. Filament releases a branch with ?ref=, as onboarding links do.
GUIDE_URL = re.compile(r'https://filament\.dm/agents/grokbot\.md'
                       r'(?:\?ref=([A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*))?')
SCRIPT_URL = 'https://raw.githubusercontent.com/filament-dm/filament-agent-kit/{ref}/filament-grokbot/listen.py'
ROUTINE_URL = 'https://raw.githubusercontent.com/filament-dm/filament-agent-kit/{ref}/filament-grokbot/ROUTINE.md'
# What each routine run reads; its first line marks it as this guide's protocol.
ROUTINE = 'ROUTINE.md'
# The guide version the last --update installed and reported.
INSTALLED = 'guide_version'
FETCH_LIMIT = 2 * 1024 * 1024
UPDATE_COMMAND = '/filament update'
# Filament's notices about a release; whether to update comes from get_self.
NOTICE_PREFIXES = ('Filament: your grokbot guide has a new version',
                   'Filament: you are connected with the grokbot guide')
# A release the update did not install is offered again after this long.
UPDATE_RETRY = int(os.environ.get('FM_UPDATE_RETRY', '21600'))


class AuthFailed(Exception):
    pass


class Transient(Exception):
    pass


class StateError(Exception):
    pass


class UpdateFailed(Exception):
    pass


def path(name):
    return os.path.join(STATE, name)


def read(name, default=None):
    try:
        with open(path(name)) as stream:
            return stream.read().strip()
    except FileNotFoundError:
        return default


def write(name, text):
    """Replace one private file, flushing before exposing the new state."""
    target = path(name)
    temporary = target + '.' + uuid.uuid4().hex + '.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(STATE, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def save(state):
    write('state.json', json.dumps(state, separators=(',', ':')))


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_state(state):
    if not isinstance(state, dict) or state.get('format') != 2:
        return False
    if state['cursor'] is not None and not isinstance(state['cursor'], str):
        return False
    if not isinstance(state['acks'], list) or not all(isinstance(v, str) for v in state['acks']):
        return False
    for key in ('failed_since', 'last_poll_at', 'last_success_at'):
        if state[key] is not None and not number(state[key]):
            return False
    if not all(type(state[key]) is bool for key in ('auth_failed', 'notified')):
        return False
    if not isinstance(state['counts'], dict):
        return False
    for key in ('attempts', 'empty', 'work_batches', 'work_items', 'errors'):
        if type(state['counts'][key]) is not int or state['counts'][key] < 0:
            return False
    session, pending = state['session'], state['pending']
    if session is not None:
        if not isinstance(session, dict) or not isinstance(session['id'], str) or not number(session['started_at']):
            return False
        # Revision 2026-10-20.1 sessions have no listen_until.
        if session.get('listen_until') is not None and not number(session['listen_until']):
            return False
    if pending is not None:
        if not isinstance(pending, dict) or not isinstance(pending['batch_id'], str):
            return False
        if not session or pending['session_id'] != session['id'] or not number(pending['created_at']):
            return False
        if not isinstance(pending['work'], list) or not all(isinstance(v, dict) for v in pending['work']):
            return False
    return True


def load():
    raw = read('state.json')
    if raw is not None:
        try:
            state = json.loads(raw)
            if not valid_state(state):
                raise ValueError('invalid state')
            return state
        except (ValueError, KeyError, TypeError):
            raise StateError('invalid state.json; preserve it and investigate, do not reset the cursor')
    # The old listener did not save its returned work. Never infer completion
    # from the age of its handling marker, or silently acknowledge that batch.
    if read('handling') is not None:
        raise StateError('legacy handling marker exists; reconcile that work before upgrading')
    try:
        acks = json.loads(read('acks', '[]'))
        if not isinstance(acks, list) or not all(isinstance(v, str) for v in acks):
            raise ValueError('invalid acks')
    except ValueError:
        raise StateError('invalid legacy acks; preserve state and investigate')
    return {'format': 2, 'cursor': read('cursor'), 'acks': acks,
            'session': None, 'pending': None, 'failed_since': None,
            'notified': False, 'auth_failed': False,
            'last_poll_at': None, 'last_success_at': None,
            'counts': {'attempts': 0, 'empty': 0, 'work_batches': 0, 'work_items': 0, 'errors': 0}}


def event(kind, **fields):
    """No tokens, message bodies, reply targets or server error bodies in logs."""
    record = dict(fields, event=kind, at=time.time(), pid=os.getpid(), revision=REVISION)
    try:
        target = path('listen.jsonl')
        if os.path.exists(target) and os.path.getsize(target) > LOG_LIMIT:
            os.replace(target, target + '.1')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, separators=(',', ':')) + '\n').encode())
        finally:
            os.close(fd)
    except OSError:
        # Work safety depends on state.json, not telemetry availability.
        pass


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (ValueError, TypeError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def legacy_running():
    return pid_alive(read('listen.lock/pid'))


@contextlib.contextmanager
def lock():
    # Never unlink this file: replacement would create a second lock domain.
    fd = os.open(path('listener.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        # A brief wait lets --done survive another start's millisecond check.
        give_up = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    break
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def lock_held():
    """Observe an existing lock without creating files or changing state."""
    try:
        fd = os.open(path('listener.lock'), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def pending_summary(pending):
    return {'batch_id': pending['batch_id'], 'session_id': pending['session_id'],
            'created_at': pending['created_at'], 'items': len(pending['work'])}


def status():
    records = []
    for name in ('listen.jsonl.1', 'listen.jsonl'):
        for line in (read(name, '') or '').splitlines():
            try:
                row = json.loads(line)
                if row.get('event') in ('CALL_START', 'CALL_END'):
                    records.append(row)
            except (ValueError, AttributeError):
                continue
    try:
        state = load()
        error = None
    except StateError as exc:
        state, error = None, str(exc)
    with open(os.path.abspath(__file__), 'rb') as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    return {'revision': REVISION, 'guide_version': installed_version(), 'script_sha256': digest, 'observed_at': time.time(),
            'lock_held': lock_held(), 'legacy_process_alive': legacy_running(),
            'state_error': error,
            'session': state['session'] if state else None,
            'pending': pending_summary(state['pending']) if state and state['pending'] else None,
            'poll_counts': state['counts'] if state else None,
            'last_poll_at': state.get('last_poll_at') if state else None,
            'last_success_at': state.get('last_success_at') if state else None,
            'handler_timeout_seconds': HANDLER_TIMEOUT, 'wake': wake_summary(),
            'recent_calls': records[-40:], 'model_tokens': None,
            'limits': 'Counts cover this script only. Scheduler starts, model turns, cached tokens and billing are not observable here. A held lock may mean polling or retry backoff.'}


def parse(body):
    body = body.strip()
    if body.startswith('{'):
        return json.loads(body)
    for block in reversed(body.split('\n\n')):
        data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
        try:
            reply = json.loads(data)
        except ValueError:
            continue
        if isinstance(reply, dict) and ('result' in reply or 'error' in reply):
            return reply
    raise ValueError('no JSON-RPC reply')


def rpc(method, params, timeout):
    """One JSON-RPC request; returns the parsed reply, error or result."""
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    request = urllib.request.Request(read('endpoint') or DEFAULT_ENDPOINT, data=payload, method='POST', headers={
        'Authorization': 'Bearer ' + read('token'), 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthFailed('HTTP %d' % exc.code)
        raise Transient('HTTP %d' % exc.code)
    except Exception as exc:
        raise Transient(type(exc).__name__)
    try:
        return parse(body)
    except ValueError:
        raise Transient('invalid response body')


def poll(cursor, acks, wait, timeout):
    args = {'wait_seconds': wait}
    if cursor:
        args['cursor'] = cursor
    if acks:
        args['ack'] = acks
    reply = rpc('tools/call', {'name': 'poll_work', 'arguments': args}, timeout)
    try:
        if 'error' in reply:
            raise Transient('RPC error')
        result = reply.get('result') or {}
        if result.get('isError'):
            raise Transient('tool error')
        response = json.loads(result['content'][0]['text'])
        if not isinstance(response, dict) or not isinstance(response.get('work', []), list):
            raise ValueError('invalid response')
        if not all(isinstance(v, dict) for v in response.get('work', [])):
            raise ValueError('invalid work')
        if response.get('cursor') is not None and not isinstance(response['cursor'], str):
            raise ValueError('invalid cursor')
        if not number(response.get('next_poll_ms', 1000)):
            raise ValueError('invalid poll delay')
        return response
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise Transient('invalid response body')


def read_tool(name, args, timeout):
    """A read-only tool call; returns its decoded result, or None."""
    reply = rpc('tools/call', {'name': name, 'arguments': args}, timeout)
    result = reply.get('result') if isinstance(reply, dict) else None
    if not isinstance(result, dict) or result.get('isError'):
        return None
    try:
        data = json.loads(result['content'][0]['text'])
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def earlier(item, timeout):
    """The messages before an item's new ones, trimmed; None when unreadable."""
    new = [m for m in item.get('messages') or [] if isinstance(m, dict)]
    ids = {m.get('event_id') for m in new}
    first = min((m['timestamp'] for m in new if number(m.get('timestamp'))), default=None)
    thread = item.get('thread_id')
    if isinstance(thread, str) and thread:
        data = read_tool('get_thread', {'message_id': thread}, timeout)
        rows = data and [data.get('root')] + list(data.get('replies') or [])
    else:
        limit = min(200, CONTEXT_MESSAGES + len(new))
        data = read_tool('get_recent_messages', {'channel': item['channel_id'], 'limit': limit}, timeout)
        rows = data and data.get('messages')
    if not isinstance(rows, list):
        return None
    rows = [r for r in rows if isinstance(r, dict) and r.get('event_id') not in ids
            and (first is None or not number(r.get('timestamp')) or r['timestamp'] < first)]
    # A thread's root says what the thread is about; keep it ahead of the tail.
    if rows and rows[0].get('event_id') == thread:
        rows = rows[:1] + rows[1:][-(CONTEXT_MESSAGES - 1):]
    else:
        rows = rows[-CONTEXT_MESSAGES:]
    return [{k: r[k] for k in CONTEXT_FIELDS if k in r} for r in rows]


def add_context(work, call_id):
    """Best effort: a failed read leaves the item without context, never blocks it."""
    deadline = time.monotonic() + CONTEXT_BUDGET
    items = [i for i in work if i.get('kind') == 'message' and isinstance(i.get('channel_id'), str)
             and 'context' not in i]
    for item in items[:CONTEXT_ITEMS]:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        try:
            rows = earlier(item, min(10, remaining))
        except (AuthFailed, Transient) as exc:
            event('CONTEXT_FAILED', call_id=call_id, reason=str(exc))
            break
        if rows:
            item['context'] = {'note': CONTEXT_NOTE, 'messages': rows}


def version_key(value):
    match = re.fullmatch(r'([0-9]{4})-([0-9]{2})-([0-9]{2})\.([1-9][0-9]*)', value or '')
    if not match:
        raise UpdateFailed('invalid guide version')
    return tuple(int(part) for part in match.groups())


def installed_version():
    """The guide version this agent runs: the last one --update installed, else this script's."""
    value = read(INSTALLED)
    try:
        version_key(value)
    except UpdateFailed:
        return REVISION
    return value


def guide_ref(url):
    """The branch a guide URL names, or None when it is not this guide's URL."""
    match = GUIDE_URL.fullmatch(url or '')
    if not match:
        return None
    ref = match.group(1) or 'main'
    # Git rejects these names, so such a URL could never resolve.
    if '..' in ref or any(part.endswith(('.', '.lock')) for part in ref.split('/')):
        return None
    return ref


def fetch(url):
    """GET one published file of this guide."""
    # Tests only: serve those URLs from a local server.
    base = os.environ.get('FM_FETCH_BASE')
    target = base + '?url=' + urllib.parse.quote(url, safe='') if base else url
    request = urllib.request.Request(target, headers={'User-Agent': 'filament-grokbot/' + REVISION})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(FETCH_LIMIT + 1)
    except Exception as exc:
        raise Transient(type(exc).__name__)
    if len(data) > FETCH_LIMIT:
        raise UpdateFailed('download too large')
    return data.decode('utf-8')


def guide_header(text):
    """The guide_id and version in a guide's frontmatter."""
    head = text.split('\n---', 1)[0] if text.startswith('---') else ''
    guide_id = re.search(r'^guide_id: (\S+)$', head, re.M)
    version = re.search(r'^version: "([^"]+)"$', head, re.M)
    return guide_id and guide_id.group(1), version and version.group(1)


def released(data):
    """Filament's answer to which version to adopt: get_self's guide.update."""
    update = ((data or {}).get('guide') or {}).get('update') if isinstance(data, dict) else None
    if not isinstance(update, dict) or not isinstance(update.get('version'), str) \
            or guide_ref(update.get('url')) is None:
        return None
    return {'version': update['version'], 'url': update['url']}


def load_update():
    try:
        info = json.loads(read('update.json', '{}'))
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    if not isinstance(info.get('offered'), dict):
        info['offered'] = {}
    return info


def offer_update(call_id, asked=False, retry=True):
    """Ask Filament once whether a newer guide is released; never blocks listening.

    The next listener call returns UPDATE when one is. The owner's command is
    answered even when there is none, so the owner hears that the bot is current.
    """
    try:
        offer = released(read_tool('get_self', {}, 15))
    except (AuthFailed, Transient) as exc:
        event('UPDATE_CHECK_FAILED', call_id=call_id, reason=str(exc))
        offer = None
    info = load_update()
    now = time.time()
    if offer:
        last = info['offered'].get(offer['version'])
        if asked or not retry or not number(last) or now - last >= UPDATE_RETRY:
            info.update(requested=True, version=offer['version'], asked_by_owner=asked)
            info['offered'][offer['version']] = now
    elif asked:
        info.update(requested=True, version=None, asked_by_owner=True)
    write('update.json', json.dumps(info))


def take_update():
    """The update to hand to the model now, once."""
    info = load_update()
    if not info.get('requested'):
        return None
    info['requested'] = False
    write('update.json', json.dumps(info))
    offer = {'asked_by_owner': bool(info.get('asked_by_owner'))}
    if info.get('version'):
        offer['version'] = info['version']
    return offer


def intercept(work, state, call_id):
    """Take Filament's update notices and the owner's update command out of the work.

    They are acknowledged here and never handed to the model as messages. Whether
    to update is Filament's answer in get_self, never the notice's text.
    """
    kept, asked, noticed = [], False, False
    for item in work:
        messages = item.get('messages') if item.get('kind') == 'message' else None
        if not isinstance(messages, list):
            kept.append(item)
            continue
        rest = []
        for message in messages:
            body = message.get('body') if isinstance(message, dict) else None
            if isinstance(body, str) and message.get('is_from_principal') \
                    and isinstance(message.get('event_id'), str):
                if body.split()[:2] == UPDATE_COMMAND.split():
                    asked = True
                    state['acks'].append(message['event_id'])
                    continue
                if body.startswith(NOTICE_PREFIXES):
                    noticed = True
                    state['acks'].append(message['event_id'])
                    continue
            rest.append(message)
        if rest:
            item['messages'] = rest
            kept.append(item)
    if asked or noticed:
        offer_update(call_id, asked=asked, retry=False)
    return kept


def report_version(version):
    reply = rpc('tools/call', {'name': 'report_guide_version',
                               'arguments': {'guide_id': GUIDE_ID, 'version': version}}, 30)
    result = reply.get('result') if isinstance(reply, dict) else None
    if not isinstance(result, dict) or 'error' in reply or result.get('isError'):
        raise Transient('report refused')


def install(script):
    """Replace this script, once the new one has read the current state."""
    target = os.path.abspath(__file__)
    download = target + '.download'
    with contextlib.suppress(FileNotFoundError):
        os.unlink(download)
    fd = os.open(download, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(script)
    try:
        check = subprocess.run([sys.executable, download, '--check-state'], capture_output=True,
                               text=True, timeout=60, env=dict(os.environ, FM_STATE=STATE))
        if check.stdout.strip() != 'STATE_OK':
            raise UpdateFailed('the new listener cannot read this state')
        shutil.copy2(target, target + '.previous')
        os.replace(download, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(download)


def update_listener():
    """Install and report the guide version Filament released; one result line."""
    with lock() as acquired:
        if not acquired or legacy_running():
            return 'NEEDS_RETRY', 'another execution owns the listener'
        state = load()
        if state['pending']:
            return 'NEEDS_RETRY', 'unfinished work; finish it with --done first'
        try:
            offer = released(read_tool('get_self', {}, 15))
        except AuthFailed as exc:
            return 'FAILED', str(exc)
        except Transient as exc:
            return 'NEEDS_RETRY', str(exc)
        # Filament's release is the only thing an update installs; nothing
        # released for this agent means nothing to do.
        current = installed_version()
        if not offer:
            return 'CURRENT', current
        url, ref = offer['url'], guide_ref(offer['url'])
        try:
            guide_id, version = guide_header(fetch(url))
            if guide_id != GUIDE_ID or not version:
                raise UpdateFailed('the released URL is not this guide')
            if version != offer['version']:
                raise UpdateFailed('the guide is %s but Filament released %s' % (version, offer['version']))
            if version_key(version) < version_key(current):
                raise UpdateFailed('refusing to move back to %s' % version)
            if version != current:
                # The files on the guide's branch go with it; their own revision
                # markers may lag a guide-only change and are not compared.
                script = fetch(SCRIPT_URL.format(ref=ref))
                if "\nREVISION = '" not in script:
                    raise UpdateFailed('the released listener is not this guide\'s')
                routine = fetch(ROUTINE_URL.format(ref=ref))
                if not routine.startswith('<!-- grokbot '):
                    raise UpdateFailed('the released routine protocol is not this guide\'s')
                install(script)
                write(ROUTINE, routine)
            write(INSTALLED, version)
        except UpdateFailed as exc:
            return 'FAILED', str(exc)
        except Transient as exc:
            return 'NEEDS_RETRY', 'download failed: %s' % exc
        try:
            report_version(version)
        except (AuthFailed, Transient) as exc:
            return 'FAILED', 'installed %s but could not report it: %s' % (version, exc)
        info = load_update()
        info['requested'] = False
        write('update.json', json.dumps(info))
        return 'UPDATED', version


def check_state():
    try:
        load()
    except StateError as exc:
        return 'STATE_ERROR %s' % exc
    return 'STATE_OK'


def load_wake():
    raw = read('wake.json')
    if raw is None:
        return None
    try:
        wake = json.loads(raw)
        if isinstance(wake, dict) and isinstance(wake.get('url'), str) and isinstance(wake.get('key'), str):
            return wake
    except ValueError:
        pass
    raise StateError('invalid wake.json; run --wake again')


def subscribe(wake):
    """Register or renew Filament's wake; returns (refresh_before, error)."""
    params = {'name': 'work.available', 'arguments': {}, 'ttlMs': None,
              'delivery': {'mode': 'webhook', 'url': wake['url'], 'bearer': wake['key'],
                           # Every POST starts a run; the run takes its work from poll_work.
                           'payload': 'nudge'}}
    try:
        reply = rpc('events/subscribe', params, 15)
    except AuthFailed as exc:
        return None, str(exc)
    except Transient as exc:
        return None, str(exc)
    error = reply.get('error')
    if isinstance(error, dict):
        data = error.get('data') if isinstance(error.get('data'), dict) else {}
        return None, str(data.get('reason') or error.get('code') or 'refused')
    result = reply.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('refreshBefore'), str):
        return None, 'invalid response body'
    return result['refreshBefore'], None


def renew_wake():
    """Best effort: a failed renewal leaves the backstop schedule working."""
    try:
        wake = load_wake()
    except StateError as exc:
        event('WAKE_RENEW_FAILED', reason=str(exc))
        return
    if wake is None or time.time() - (wake.get('refreshed_at') or 0) < WAKE_REFRESH_AFTER:
        return
    refresh_before, error = subscribe(wake)
    if error:
        wake['error'] = error
        event('WAKE_RENEW_FAILED', reason=error)
    else:
        wake.update(refreshed_at=time.time(), refresh_before=refresh_before, error=None)
    write('wake.json', json.dumps(wake, separators=(',', ':')))


def set_wake():
    url = sys.stdin.readline().strip()
    key = sys.stdin.readline().strip()
    # The routine panel also offers the key as a header value.
    key = re.sub(r'^(?:Authorization:\s*)?Bearer\s+', '', key, flags=re.I)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in WAKE_HOSTS or not parts.path.strip('/'):
        return 'WAKE_FAILED', 'the URL must be the routine webhook URL on ' + ', '.join(WAKE_HOSTS)
    if not WAKE_KEY.fullmatch(key):
        return 'WAKE_FAILED', 'the key must be the routine webhook key'
    wake = {'url': url, 'key': key}
    refresh_before, error = subscribe(wake)
    if error:
        return 'WAKE_FAILED', error
    wake.update(refreshed_at=time.time(), refresh_before=refresh_before, error=None)
    write('wake.json', json.dumps(wake, separators=(',', ':')))
    return 'WAKE_SET', refresh_before


def clear_wake():
    wake = load_wake()
    if wake is None:
        return 'WAKE_OFF', None
    try:
        rpc('events/unsubscribe', {'name': 'work.available', 'arguments': {},
                                   'delivery': {'mode': 'webhook', 'url': wake['url']}}, 30)
    except (AuthFailed, Transient) as exc:
        # The subscription still lapses within a week without renewal.
        event('WAKE_OFF_FAILED', reason=str(exc))
    os.unlink(path('wake.json'))
    return 'WAKE_OFF', None


def wake_summary():
    try:
        wake = load_wake()
    except StateError as exc:
        return {'configured': False, 'error': str(exc)}
    if wake is None:
        return {'configured': False}
    return {'configured': True, 'host': urllib.parse.urlsplit(wake['url']).hostname,
            'refreshed_at': wake.get('refreshed_at'), 'refresh_before': wake.get('refresh_before'),
            'error': wake.get('error')}


def work_result(pending, verb='WORK'):
    body = {'batch_id': pending['batch_id'], 'session_id': pending['session_id'], 'work': pending['work']}
    if verb == 'RECOVER':
        body['received_at'] = pending['created_at']
    return '%s %d' % (verb, len(pending['work'])), body


def take_over(state, pending, follow):
    now = time.time()
    state['session'] = {'id': uuid.uuid4().hex, 'started_at': now, 'listen_until': now + follow}
    # A late completion from the prior owner must not clear this handoff.
    pending['batch_id'] = uuid.uuid4().hex
    pending['session_id'] = state['session']['id']
    save(state)


def listen(state, budget, session_max, call_id, metrics):
    offer = take_update()
    if offer is not None:
        return 'UPDATE', offer
    started = time.monotonic()
    session = state['session']
    session_end = session['started_at'] + session_max
    window = min(session.get('listen_until') or session_end, session_end) - time.time()
    duration = max(0, min(budget, window))
    deadline = started + duration
    delay = 1
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not state['acks']:
            break
        # Flushing an explicit acknowledgment after the session ends can return
        # new work. Persist and hand it over before allowing any subsequent poll.
        wait = max(0, min(POLL_WAIT, int(remaining)))
        # The server holds an empty poll for the whole wait and answers after
        # it, so allow for its latency even past the deadline.
        timeout = wait + HTTP_SLACK
        state['counts']['attempts'] += 1
        metrics['poll_attempts'] += 1
        state['last_poll_at'] = time.time()
        save(state)
        try:
            response = poll(state['cursor'], state['acks'], wait, max(0.05, timeout))
        except AuthFailed as exc:
            state['counts']['errors'] += 1
            metrics['poll_errors'] += 1
            state['auth_failed'] = True
            save(state)
            return 'AUTH_FAILED', str(exc)
        except Transient as exc:
            state['counts']['errors'] += 1
            metrics['poll_errors'] += 1
            state['failed_since'] = state['failed_since'] or time.time()
            save(state)
            event('POLL_FAILED', call_id=call_id, reason=str(exc), retry_seconds=delay)
            if time.time() - state['failed_since'] >= DISCONNECT_AFTER and not state['notified']:
                state['notified'] = True
                save(state)
                return 'DISCONNECTED', str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, BACKOFF_CAP)
            continue
        delay = 1
        state['failed_since'], state['notified'] = None, False
        state['last_success_at'] = time.time()
        state['acks'] = []
        if response.get('cursor'):
            state['cursor'] = response['cursor']
        work = intercept(response.get('work') or [], state, call_id)
        if not work:
            offer = take_update()
            if offer is not None:
                save(state)
                return 'UPDATE', offer
        if work:
            state['counts']['work_batches'] += 1
            state['counts']['work_items'] += len(work)
            metrics['work_items'] += len(work)
            state['pending'] = {'batch_id': uuid.uuid4().hex, 'session_id': state['session']['id'],
                                'created_at': time.time(), 'work': work}
            # Cursor and payload are one atomic commit. No later run may poll
            # until --done names this batch, even if its owner disappeared.
            save(state)
            add_context(state['pending']['work'], call_id)
            save(state)
            return work_result(state['pending'])
        state['counts']['empty'] += 1
        metrics['empty_polls'] += 1
        save(state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pause = min(max(response.get('next_poll_ms', 1000), 0), 5000) / 1000.0
        time.sleep(min(max(pause, 0.05 if wait == 0 else 0), remaining))
    if duration >= window:
        state['session'] = None
        save(state)
        return 'END', None
    return 'AGAIN', {'session_id': state['session']['id']}


def execute(args, call_id, metrics):
    with lock() as acquired:
        if not acquired or legacy_running():
            return 'RUNNING', None
        state = load()
        if state['auth_failed']:
            return 'AUTH_FAILED', 'previous authentication failure; reconnect explicitly'
        pending = state['pending']
        now = time.time()
        if args.done:
            if not pending or pending['batch_id'] != args.done:
                # Already completed, or recovered by a later execution.
                return 'STALE', None
            skipped = {value for value in args.ack.split(',') if value}
            delivered = {message.get('event_id') for item in pending['work']
                         for message in (item.get('messages') or []) if isinstance(message, dict)}
            if not skipped <= delivered:
                raise StateError('--ack may only name messages in this pending batch')
            state['acks'] = sorted(set(state['acks']) | skipped)
            state['pending'] = None
            session = state['session']
            session['listen_until'] = max(session.get('listen_until') or 0, now + args.follow)
            # Completion, cursor and acks are saved together, before polling.
            save(state)
        elif args.resume:
            if not pending or pending['batch_id'] != args.resume:
                raise StateError('--resume must name the current pending batch')
            take_over(state, pending, args.follow)
            return work_result(pending)
        elif pending:
            if now - pending['created_at'] < HANDLER_TIMEOUT:
                return 'PENDING', pending_summary(pending)
            take_over(state, pending, args.follow)
            return work_result(pending, 'RECOVER')
        elif args.again:
            if not state['session'] or state['session']['id'] != args.again:
                # Ended or replaced by another execution; nothing to continue.
                return 'END', None
        else:
            state['session'] = {'id': uuid.uuid4().hex, 'started_at': now, 'listen_until': now + args.check}
            save(state)
            renew_wake()
            offer_update(call_id)
        if not state['session']:
            raise StateError('missing session; inspect state before continuing')
        metrics['session_id'] = state['session']['id']
        return listen(state, args.budget, args.session, call_id, metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument('--status', action='store_true', help='read-only JSON diagnostics; no poll or state writes')
    operations.add_argument('--token', action='store_true', help='store token from stdin, never command arguments')
    operations.add_argument('--done', metavar='BATCH_ID', help='all work in this batch was handled; resume polling')
    operations.add_argument('--resume', metavar='BATCH_ID', help='recover saved work only after verifying its prior owner ended')
    operations.add_argument('--again', metavar='SESSION_ID', help='continue this session after AGAIN')
    operations.add_argument('--wake', action='store_true',
                            help='register the routine webhook URL and key, read from stdin, as Filament\'s wake')
    operations.add_argument('--wake-off', action='store_true', help='stop Filament\'s webhook wake')
    operations.add_argument('--update', action='store_true',
                            help='install and report the guide version Filament released; prints one line')
    operations.add_argument('--check-state', action='store_true',
                            help='read-only: whether this version can read the saved state')
    parser.add_argument('--endpoint', help='store a non-default endpoint with --token')
    parser.add_argument('--ack', default='', help='skipped event ids, comma-separated, with --done only')
    parser.add_argument('--budget', type=int, default=int(os.environ.get('FM_BUDGET', '600')),
                        help='longest single call, seconds')
    parser.add_argument('--check', type=int, default=int(os.environ.get('FM_CHECK', '60')),
                        help='listening window of a scheduled start, seconds')
    parser.add_argument('--follow', type=int, default=int(os.environ.get('FM_FOLLOW', '600')),
                        help='listening window after handled work, seconds')
    parser.add_argument('--session', type=int, default=int(os.environ.get('FM_SESSION', '1800')),
                        help='longest session, seconds')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(), indent=2))
        return
    if args.check_state:
        print(check_state())
        return
    if args.endpoint and not args.token:
        parser.error('--endpoint requires --token')
    if args.ack and not args.done:
        parser.error('--ack requires --done BATCH_ID')
    if not 0 < args.budget <= 690 or not 0 < args.session <= 2400:
        parser.error('budget must be 1..690 seconds and session 1..2400 seconds')
    if not 0 < args.check <= 2400 or not 0 < args.follow <= 2400:
        parser.error('check and follow must be 1..2400 seconds')
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    if args.token:
        token = sys.stdin.readline().strip()
        if not token.startswith('fmcp_'):
            print('ERROR the token must start with fmcp_')
            return
        write('token', token)
        if args.endpoint:
            write('endpoint', args.endpoint)
        print('TOKEN_SET')
        return
    if not read('token'):
        print('ERROR no token; run with --token first')
        return
    if args.wake or args.wake_off:
        try:
            result, body = set_wake() if args.wake else clear_wake()
        except (StateError, OSError) as exc:
            result, body = 'ERROR', str(exc)
        print(result)
        if body is not None:
            print(body)
        return
    if args.update:
        try:
            result, detail = update_listener()
        except (StateError, OSError) as exc:
            result, detail = 'FAILED', str(exc)
        print('%s %s' % (result, detail) if detail else result)
        return
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    mode = 'done' if args.done else 'resume' if args.resume else 'again' if args.again else 'start'
    event('CALL_START', call_id=call_id, mode=mode, budget=args.budget, check=args.check,
          follow=args.follow, session_limit=args.session)
    result, body = 'ERROR', None
    metrics = {'poll_attempts': 0, 'empty_polls': 0, 'work_items': 0, 'poll_errors': 0}
    try:
        result, body = execute(args, call_id, metrics)
    except (StateError, OSError) as exc:
        body = str(exc)
    finally:
        event('CALL_END', call_id=call_id, result=result, elapsed_seconds=round(time.monotonic() - started, 3), **metrics)
    print(result)
    if body is not None:
        print(json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body)
    sys.stdout.flush()


if __name__ == '__main__':
    main()
