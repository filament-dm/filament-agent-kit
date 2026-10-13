#!/usr/bin/env python3
"""Foreground reply/ack for one claimed item. At most two reply post attempts."""
import os
import sys
sys.dont_write_bytecode = True
sys.path[0] = os.path.expanduser(os.environ.get('FM_STATE', '~/.filament'))
from fm_lib import *


def rpc(tool, args):
    return call(tool, args, timeout=30, before=lambda: lock_write('reply.lock.d'))


def main():
    rotate_logs()
    args = sys.argv[1:]
    ack = bool(args and args[0] == '--ack')
    try:
        if len(args) != (2 if ack else 1):
            raise ValueError()
        n = int(args[-1])
        if n < 1:
            raise ValueError()
    except ValueError:
        print('BAD_ITEM')
        return 2
    # Preserve CRLF and all UTF-8 body bytes except trailing LF (harvest behaviour).
    try:
        body = '' if ack else sys.stdin.buffer.read().decode('utf-8').rstrip('\n')
    except UnicodeDecodeError:
        print('EMPTY_BODY invalid UTF-8')
        return 2
    with reply_lock():
        with state_guard():
            batch = batch_read()
            item = next((i for i in batch.get('items', []) if i.get('n') == n), None)
            if not item:
                print('BAD_ITEM')
                return 2
            if rd('handling') != batch.get('batch_id') or not (STATE / 'handling').exists():
                print('STALE_BATCH')
                return 2
            if time.time() - (STATE / 'handling').stat().st_mtime >= 600:
                print('STALE_BATCH')
                return 2
            (STATE / 'handling').touch()
        if not ack and not body:
            print('EMPTY_BODY')
            return 2
        notify_failures()
        ids = [m['event_id'] for m in item['messages'] if m.get('event_id')]
        if ids and all(i in set(rd('replied_ids').split()) for i in ids):
            print('already recorded')
            return 0
        if ack:
            record(ids, ack=True)
            log('ACK-QUEUED n=' + str(n), 'reply.log')
            print('ACK-QUEUED')
            return 0
        rw = item.get('reply_with')
        if not rw or not ids:
            print('BAD_ITEM')
            return 2
        tool, params = rw['tool'], dict(rw.get('args', {}))
        params['markdown_body'] = body
        start_ms = time.time() * 1000 - 5000
        log('reply_start n={} tool={}'.format(n, tool), 'reply.log')

        def sent(result):
            eid = result.get('event_id') if isinstance(result, dict) else None
            record(ids)
            log('reply_sent posted=' + str(eid or 'unknown'), 'reply.log')
            print('sent ' + redact(eid or 'unknown'))
            return 0

        error = None
        try:
            try:
                return sent(rpc(tool, params))
            except NetErr:
                sleep(2)
                channel = params.get('channel') or item.get('channel_id')
                try:
                    result = rpc('get_recent_messages', {'channel': channel, 'limit': 40})
                    if not isinstance(result, dict) or not isinstance(result.get('messages'), list):
                        raise NetErr('malformed lookup')
                    for m in result['messages']:
                        if (m.get('is_from_self') and m.get('timestamp', 0) >= start_ms
                                and ' '.join((m.get('body') or '').split()) == ' '.join(body.split())):
                            record(ids)
                            print('landed ' + redact(m.get('event_id') or 'unknown'))
                            return 0
                except AuthErr as e:
                    mark_dead(e)
                    error = 'uncertain send: ' + str(e)
                except (NetErr, ToolErr) as e:
                    error = 'uncertain send: ' + str(e)
                if error is None:
                    return sent(rpc(tool, params))
        except ToolErr as e:
            if 'already answered' in str(e).lower():
                record(ids)
                print('already answered')
                return 0
            error = str(e)
        except AuthErr as e:
            mark_dead(e)
            error = str(e)
        except NetErr as e:
            error = str(e)
        error = redact(error or 'uncertain send')[:1000]
        original = (item['messages'][0].get('body') or '[no text, media only]')[:300]
        notice = redact('Could not send my reply to: "{}". Error: {}. '
                        'Recorded as failed and acknowledged; I will not retry it.'.format(original, error))
        rows = failures_read()
        rows.append({'at': ts(), 'ids': ids, 'error': error, 'notice': notice, 'notified': 'no'})
        failures_write(rows)
        record(ids, ack=True)
        log('reply FAILED n={} error={}'.format(n, error), 'reply.log')
        # Only this new notice here; old ones were retried once at entry.
        try:
            rpc('message_principal', {'markdown_body': notice})
            rows[-1]['notified'] = 'yes'
        except AuthErr as e:
            mark_dead(e)
            log('failure notice not sent ' + str(e), 'reply.log')
        except (NetErr, ToolErr) as e:
            log('failure notice not sent ' + str(e), 'reply.log')
        failures_write(rows)
        print('FAILED ' + error.replace('\n', ' '))
        return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except TimeoutError:
        print('FAILED reply lock busy')
        sys.exit(1)
