#!/usr/bin/env python3
"""The sole poll_work owner; publishes complete batches, then hands off presence."""
import os
import sys
sys.dont_write_bytecode = True
sys.path[0] = os.path.expanduser(os.environ.get('FM_STATE', '~/.filament'))
from fm_lib import *
import copy
import signal
import uuid


class StopRequested(Exception):
    pass


def terminate(*_):
    raise StopRequested()


def finish(reason, code=0):
    print('run complete: ' + reason, flush=True)
    return code


def rpc(tool, args, timeout=30):
    return call(tool, args, timeout=timeout, before=lock_write)


def accept_startup():
    for listing, key, accepting in (('list_pending_invites', 'invites', 'accept_invite'),
                                     ('list_vouches', 'vouches', 'accept_vouch')):
        try:
            result = rpc(listing, {})
            for row in (result if isinstance(result, list) else result.get(key, [])):
                try:
                    rpc(accepting, {'loop_id': row['loop_id']})
                    log('ACCEPTED {} {}'.format(accepting, row['loop_id']))
                except AuthErr:
                    raise
                except (NetErr, ToolErr, KeyError) as e:
                    log('ACCEPT-FAILED ' + str(e))
        except AuthErr:
            raise
        except (NetErr, ToolErr) as e:
            log('ACCEPT-FAILED ' + str(e))


def classify(response):
    kept, silent = [], []
    done = set(rd('replied_ids').split())
    for original in response.get('work') or []:
        item = copy.deepcopy(original)
        kind = item.get('kind')
        if kind in ('invite', 'vouch'):
            try:
                rw = item['reply_with']
                rpc(rw['tool'], rw.get('args', {}))
                log('ACCEPTED {} {}'.format(kind, item.get('loop_id')))
            except AuthErr:
                raise
            except (NetErr, ToolErr, KeyError, TypeError) as e:
                log('ACCEPT-FAILED ' + str(e))
            continue
        if kind != 'message':
            continue
        if item.get('reply_with') is None:
            log('NO-REPLY-SLOT channel=' + str(item.get('channel_id')))
            continue
        messages = item.get('messages') or []
        fresh = [m for m in messages if m.get('event_id') not in done
                 and not m.get('is_from_self') and not m.get('is_system')]
        fresh_ids = {m.get('event_id') for m in fresh}
        silent.extend(m['event_id'] for m in messages
                      if m.get('event_id') and m['event_id'] not in fresh_ids)
        answer = fresh and (item.get('is_backchannel') or any(
            m.get('is_mention') or m.get('is_from_principal') or
            (m.get('is_implicitly_mentioned') and m.get('reply_expected')) for m in fresh))
        if not answer:
            silent.extend(m['event_id'] for m in fresh if m.get('event_id'))
            continue
        item['messages'] = fresh
        if item['reply_with']['tool'] == 'post_message':
            newest = max(enumerate(fresh), key=lambda pair: (pair[1].get('timestamp', 0), pair[0]))[1]
            item['reply_with'].setdefault('args', {})['in_reply_to'] = newest['event_id']
        kept.append(item)
    with state_guard():
        ids_add('pending_acks', silent)
    return kept


def context_rows(result, limit):
    rows = result if isinstance(result, list) else result.get('messages', [])
    keys = ('event_id', 'sender', 'body', 'timestamp', 'media',
            'is_from_self', 'is_from_principal', 'is_from_agent')
    ordered = sorted(rows, key=lambda m: m.get('timestamp', 0))[-limit:]
    return [{k: ((m[k] or '')[:2000] if k == 'body' else m[k])
             for k in keys if k in m} for m in ordered]


def enrich(items):
    for n, item in enumerate(items, 1):
        item.update(n=n, context=[], thread=[])
        item.setdefault('thread_id', None)
        item.setdefault('is_backchannel', False)
        errors = []
        limit = 100 if item['is_backchannel'] else 40
        for field, tool, args, count in (
                ('context', 'get_recent_messages', {'channel': item['channel_id'], 'limit': limit}, limit),
                ('thread', 'get_thread', {'message_id': item['thread_id']}, 50)):
            if field == 'thread' and not item['thread_id']:
                continue
            try:
                item[field] = context_rows(rpc(tool, args), count)
            except AuthErr:
                raise
            except (NetErr, ToolErr) as e:
                errors.append(field + ': ' + str(e))
        if errors:
            item['context_error'] = '; '.join(errors)


def deliver(response, items):
    batch_id = str(uuid.uuid4())
    batch = {'batch_id': batch_id, 'at': ts(), 'server_time': response.get('server_time'),
             'settings': response.get('settings'), 'cursor': response.get('cursor'),
             'items': items, 'response': response}
    lines = ['BATCH ' + batch_id]
    for it in items:
        lines.append('ANSWER n={} channel={} thread={} backchannel={}'.format(
            it['n'], it.get('channel_id'), it.get('thread_id') or 'none',
            str(it.get('is_backchannel', False)).lower()))
        for m in it['messages']:
            flags = [k for k in ('is_from_principal', 'is_from_agent', 'is_mention',
                                 'is_implicitly_mentioned', 'reply_expected') if m.get(k)]
            lines.append('[{}] sender={} flags={} media={}'.format(
                m.get('event_id'), m.get('sender'), ','.join(flags), len(m.get('media') or [])))
            lines.append('body: ' + (m.get('body') or '')[:1500])
            log('work_received event={} msg_ts_ms={} server_time={}'.format(
                m.get('event_id'), m.get('timestamp'), response.get('server_time')))
    if 'settings' in response:
        lines.append('SETTINGS ' + json.dumps(response['settings']))
    lines.extend(['After replying, run: bash "{}" --commit'.format(STATE / 'poll.sh'),
                  'run complete: WORK'])
    with state_guard():
        # Both files are durable before either is exposed (D3).
        wr('last_work.json.tmp', json.dumps(batch, ensure_ascii=False))
        wr('run.out.tmp', redact('\n'.join(lines) + '\n'))
        os.replace(STATE / 'last_work.json.tmp', STATE / 'last_work.json')
        os.replace(STATE / 'run.out.tmp', STATE / 'run.out')
        remove('run.out.shown')
        (STATE / 'work.ready').touch(mode=0o600)
        history = rd('work_history.jsonl').splitlines()
        history.append(json.dumps(batch, ensure_ascii=False))
        wr('work_history.jsonl', '\n'.join(history[-200:]) + '\n')
    log('WORK items=' + str(len(items)))


def main():
    rotate_logs()
    try:
        budget = int(os.environ.get('FM_BUDGET', '600'))
        if not ((20 if FAST else 60) <= budget <= 600):
            raise ValueError()
    except ValueError:
        return finish('BAD_BUDGET', 3)
    with state_guard():
        if dead_marker():
            return finish('TOKEN_DEAD', 3)
        if time.time() < float(rd('next_try', '0') or 0):
            return finish('BACKOFF', 3)
        if batch_gate() is not None:
            return finish('BATCH_IN_HAND', 3)
    if not lock_take(handoff=os.environ.get('FM_HANDOFF')):
        return finish('LOCK_ALIVE', 3)
    # The first gate and the lease acquisition are separate transactions.
    with state_guard():
        blocked = batch_gate() is not None
    if blocked:
        lock_release()
        return finish('BATCH_IN_HAND', 3)
    started = time.monotonic()
    handed_off = False
    signal.signal(signal.SIGTERM, terminate)
    try:
        media = STATE / 'media'
        if media.exists():
            for p in media.iterdir():
                if p.is_file() and time.time() - p.stat().st_mtime > 3600:
                    p.unlink()
        log('listener start pid={} t={:.3f}'.format(os.getpid(), time.time()))
        log('heartbeat ' + str(heartbeat(before=lock_write)))
        self_file = STATE / 'self.json'
        if not self_file.exists() or time.time() - self_file.stat().st_mtime > 86400:
            try:
                wr('self.json', json.dumps(rpc('get_self', {})))
            except AuthErr:
                raise
            except (NetErr, ToolErr) as e:
                log('SELF-FAILED ' + str(e))
        accept_startup()
        while time.monotonic() - started < budget:
            log('heartbeat ' + str(heartbeat(before=lock_write)))
            args = {'wait_seconds': max(1, 25 // (10 if FAST else 1)), 'max_items': 10}
            with state_guard():
                # Acks appended while this request is in flight survive its success.
                acks = rd('pending_acks').split()
                cur = rd('cursor')
            if cur:
                args['cursor'] = cur
            if acks:
                args['ack'] = acks
            log('poll_work call pid={} t={:.3f}'.format(os.getpid(), time.time()))
            response = rpc('poll_work', args, timeout=args['wait_seconds'] + 15)
            lock_write()  # A rebuild/stop may have removed our lease during I/O.
            if not isinstance(response, dict) or not response.get('cursor'):
                raise NetErr('poll response missing cursor')
            with state_guard():
                wr('backoff', '0')
                wr('next_try', '0')
                wr('pending_acks', ' '.join(x for x in rd('pending_acks').split() if x not in acks))
            log('poll_work ok items={} server_time={} truncated={}'.format(
                len(response.get('work') or []), response.get('server_time'), response.get('truncated')))
            items = classify(response)
            if not items:
                with state_guard():
                    if batch_gate() is not None:
                        return finish('BATCH_IN_HAND', 3)
                    wr('cursor', response['cursor'])
                if not response.get('truncated'):
                    sleep(min(max(0, response.get('next_poll_ms', 0) / 1000), 3))
                continue
            wr('pending_cursor', response['cursor'])
            # The keeper ignores this listener's lease while it enriches the batch.
            env = dict(os.environ, FM_HB_PREDECESSOR=str(os.getpid()))
            spawn('fm_hb.py', env)
            log('hb keeper spawned')
            enrich(items)
            deliver(response, items)
            return finish('WORK')
        nonce = uuid.uuid4().hex
        lock_write()
        wr('lock.d/handoff', '{} {}'.format(nonce, time.time()))
        try:
            # Log before spawn so an extremely fast child cannot invert gap timing.
            log('handoff spawn t={:.3f}'.format(time.time()))
            spawn('fm_poll.py', dict(os.environ, FM_HANDOFF=nonce, FM_BUDGET=str(budget)))
            handed_off = True
            return finish('HANDOFF')
        except OSError as e:
            remove('lock.d/handoff')
            log('handoff failed ' + type(e).__name__)
            return finish('NO_WORK')
    except (LockLost, StopRequested):
        return finish('NO_WORK')
    except AuthErr as e:
        mark_dead(e)
        log('AUTH FAILURE ' + str(e))
        return finish('AGENT_RESERVED' if classify_auth(e) == 'reserved' else 'AUTH_FAILED', 2)
    except (NetErr, ToolErr) as e:
        backoff = min(float(rd('backoff', '0') or 0) * 2 or 1, 600)
        wr('backoff', str(backoff))
        wr('next_try', str(time.time() + backoff))
        log('BACKOFF {} {}'.format(backoff, e))
        return finish('BACKOFF')
    finally:
        if not handed_off:
            lock_release()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FileNotFoundError:
        # Rebuilds remove STATE wholesale. Do not recreate a half-install.
        finish('NO_WORK')
