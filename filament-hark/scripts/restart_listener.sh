#!/usr/bin/env bash
# Also the shared library for listen.sh and reply.sh; sourcing never runs a job.

classify_error() (
    shopt -s nocasematch
    local text=$1
    if [[ $text =~ (^|[^[:alnum:]_-])-32001([^[:alnum:]_]|$) ]] ||
       [[ $text =~ (^|[^[:alnum:]_])(HTTP|status)[[:space:]]+(401|403)([^[:alnum:]_]|$) ]] ||
       [[ $text =~ (^|[^[:alnum:]_])(401[[:space:]]+Unauthorized|403[[:space:]]+Forbidden)([^[:alnum:]_]|$) ]]; then
        echo auth
    elif [[ $text =~ (^|[^[:alnum:]_-])-32002([^[:alnum:]_]|$) ]]; then
        echo reserved
    else
        echo other
    fi
)

# Embedded JSON helpers shared by the scripts.
json_helper() {
    python3 - "$STATE" "$@" <<'PY'
import datetime, json, math, os, pathlib, re, shutil, subprocess, sys, time
S = pathlib.Path(sys.argv[1])
action, *args = sys.argv[2:]
def read(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)
def write(p, obj):
    p = pathlib.Path(p)
    tmp = p.with_name(p.name + '.tmp.' + str(os.getpid()))
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=True, separators=(',', ':'))
            f.write('\n')
        os.replace(tmp, p)
    finally:
        if tmp.exists(): tmp.unlink()
def seen_ids():
    if not (S / 'replied.json').exists(): return []
    d = read(S / 'replied.json')
    if not isinstance(d, dict) or not isinstance(d.get('ids'), list) or any(not isinstance(x, str) for x in d['ids']):
        raise ValueError('invalid replied.json')
    return d['ids']
def self_data(p):
    d = read(p)
    if not isinstance(d.get('user_id'), str) or not d['user_id']: raise ValueError('invalid self')
    return {k: d.get(k) for k in ('user_id', 'owner_id', 'display_name')}
try:
    if action == 'rotate':
        for name in ('timing.log', 'listen.log', 'reply.log', 'backstop.log'):
            p = S / name
            if p.exists() and p.stat().st_size > 512 * 1024:
                # Keep the inode so concurrent appenders still write to this log.
                with open(p, 'r+b') as f:
                    f.seek(-65537, 2)
                    data = f.read()
                    data = data[1:] if data[:1] == b'\n' else data.split(b'\n', 1)[-1]
                    data = data[:data.rfind(b'\n') + 1]
                    if len(data) > 65536: data = b''
                    f.seek(0); f.write(data); f.truncate()
    elif action == 'clean-media':
        cutoff = time.time() - 3600
        for p in (S / 'media').rglob('*'):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff: p.unlink()
            except FileNotFoundError:
                pass
    elif action == 'self':
        mode, path = args
        p = pathlib.Path(path)
        if mode == 'fresh' and time.time() - p.stat().st_mtime >= 86400: sys.exit(1)
        d = self_data(p)
        if mode == 'save': write(S / 'self.json', d)
        print(d['user_id'])
    elif action == 'invites':
        d = read(args[0]); key = args[1]
        rows = d if isinstance(d, list) else d.get(key, d.get('pending_invites', []))
        if not isinstance(rows, list): raise ValueError('invalid invitations')
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get('loop_id'), str) and row['loop_id']:
                print(json.dumps({'loop_id': row['loop_id']}, separators=(',', ':')))
    elif action == 'poll':
        run, self_id = pathlib.Path(args[0]), args[1]
        d = read(run / 'out')
        if not isinstance(d, dict) or not isinstance(d.get('work', []), list): raise ValueError('invalid poll')
        delay = d.get('next_poll_ms', 1000)
        if not isinstance(delay, (int, float)) or not math.isfinite(delay): delay = 1000
        delay = max(0, delay / 1000)
        if d.get('busy') is True:
            print('busy', delay, 0, 'none', 0)
            sys.exit()
        seen = set(seen_ids())
        kept, ack, filtered = [], [], 0
        def ids(messages):
            return [m['event_id'] for m in messages if isinstance(m.get('event_id'), str) and m['event_id']]
        def not_for_us(m):
            sender = m.get('sender', '')
            return (bool(self_id) and sender == self_id) or (isinstance(sender, str) and sender.startswith('@filament_god:'))
        for item in d.get('work', []):
            if not isinstance(item, dict): raise ValueError('invalid item')
            if item.get('reply_with') is None: continue
            messages = item.get('messages')
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages): raise ValueError('invalid messages')
            new = [m for m in messages if m.get('event_id') not in seen]
            if not new:
                ack.extend(ids(messages)); filtered += 1; continue
            item = dict(item, messages=new)
            if not item.get('is_backchannel') and all(not_for_us(m) for m in new):
                ack.extend(ids(new)); filtered += 1; continue
            kept.append(item)
        if ack:
            write(run / 'ack', {'wait_seconds': 0, 'max_items': 0, 'ack': list(dict.fromkeys(ack))})
        if kept:
            # The full envelope, with its work list filtered, is the delivered wake.
            d['work'] = kept
            write(run / 'filtered', d)
            with open(run / 'media-requests', 'w') as f:
                for item in kept:
                    f.write(json.dumps({'channel': item['channel_id'], 'limit': 25}, separators=(',', ':')) + '\n')
            stamp = str(kept[0]['messages'][0].get('timestamp', 'none')).replace(' ', '_').replace('\n', '_')[:100]
            print('work', delay, len(kept), stamp, filtered)
        else:
            print('truncated' if d.get('truncated') else 'empty', delay, 0, 'none', filtered)
    elif action == 'deliver':
        run = pathlib.Path(args[0])
        d = read(run / 'filtered')
        directory = S / 'items'
        directory.mkdir(exist_ok=True)
        seen = set(seen_ids())
        for old in directory.glob('*.json'):
            if not old.stem.isdigit(): continue
            expired = time.time() - old.stat().st_mtime > 86400
            try:
                messages = read(old)['messages']
                answered = all(m.get('event_id') in seen for m in messages)
            except (ValueError, KeyError, TypeError):
                answered = False
            if expired or answered: old.unlink()
        counter = directory / 'next'
        if not counter.exists(): write(counter, 1)
        number = int(counter.read_text())
        if number < 1: raise ValueError('invalid items/next')
        try: owner = read(S / 'self.json').get('owner_id')
        except (ValueError, OSError, AttributeError): owner = None
        tag_order = ('PRINCIPAL', 'AGENT', 'SYSTEM', 'MENTIONED', 'IMPLIED', 'REPLY_EXPECTED', 'NO_REPLY_EXPECTED')
        summaries = []
        for n, item in enumerate(d['work'], 1):
            response = run / ('media-' + str(n))
            if response.exists():
                try:
                    recent = read(response)
                    rows = recent if isinstance(recent, list) else recent['messages']
                    if not isinstance(rows, list): raise ValueError('invalid messages')
                    matches = {m['event_id']: m for m in rows if isinstance(m, dict)
                               and isinstance(m.get('event_id'), str)}
                    for m in item['messages']:
                        match = matches.get(m.get('event_id'))
                        if match is not None:
                            for key in ('media', 'msgtype', 'is_from_principal', 'is_implicitly_mentioned',
                                        'reply_expected', 'is_from_agent', 'is_system'):
                                if key in match: m[key] = match[key]
                except (ValueError, OSError, TypeError, KeyError) as e:
                    print(('MEDIA-ENRICH-FAILED ' + str(e))[:2000], file=sys.stderr)
            union = set()
            for m in item['messages']:
                flags = (bool(owner) and m.get('sender') == owner or m.get('is_from_principal') is True,
                         m.get('sender_is_agent') is True or m.get('is_from_agent') is True,
                         m.get('is_system') is True, m.get('is_mention') is True,
                         m.get('is_implicitly_mentioned') is True, m.get('reply_expected') is True,
                         m.get('reply_expected') is False)
                tags = [tag for tag, applies in zip(tag_order, flags) if applies]
                m['tags'] = ' '.join(tags)
                union.update(tags)
            item['tags'] = ('BACKCHANNEL' if item.get('is_backchannel') else 'ROOM') + (
                ' ANSWER' if union.intersection(('PRINCIPAL', 'MENTIONED', 'IMPLIED', 'REPLY_EXPECTED')) else ' QUIET')
            item['number'] = number
            # Reserve before writing: a crash can leave gaps, never reused numbers.
            write(counter, number + 1)
            write(directory / (str(number) + '.json'), item)
            summaries.append('ITEM {} {} channel={} messages={} {}'.format(
                number, item['tags'], item['channel_id'], len(item['messages']),
                ' '.join(t for t in tag_order if t in union)).rstrip())
            number += 1
        write(directory / 'wake.json', d)
        # Private output survives a later wake replacing the shared wake.json.
        (run / 'delivery').write_text('\n'.join(summaries) + '\n' + json.dumps(d, ensure_ascii=True, separators=(',', ':')) + '\n')
    elif action == 'lineage':
        p = S / 'lineage.json'
        if args[0] == 'reset':
            write(p, {'started': int(time.time()), 'count': 0})
        else:
            d = read(p) if p.exists() else {'started': int(time.time()), 'count': 0}
            d['count'] += 1
            write(p, d)
            try: raw = (S / 'rotate_after').read_text().strip()
            except FileNotFoundError: raw = ''
            limit = int(raw) if re.fullmatch(r'[0-9]{1,3}', raw) and 2 <= int(raw) <= 200 else 12
            print(d['count'], limit)
    elif action == 'reclaim':
        path, pid = pathlib.Path(args[0]), args[1]
        renamed = pathlib.Path(str(path) + '.reclaim.' + pid)
        def owner(p):
            try: value = (p / 'pid').read_text().strip()
            except FileNotFoundError: value = ''
            if not value: return '', time.time() - p.stat().st_mtime <= 5
            if value.isdigit() and int(value) > 1:
                try:
                    os.kill(int(value), 0)
                    command = subprocess.run(['ps', '-o', 'args=', '-p', value], capture_output=True, text=True).stdout
                    return value, ('listen.sh' if path.name == 'listener.lock.d' else '.sh') in command
                except ProcessLookupError: pass
                except PermissionError: return value, True
            return value, False
        # Avoid moving a known live lock or its PID-publication window.
        _, protected = owner(path)
        if protected: sys.exit(1)
        os.rename(path, renamed)  # Not mv: never nest into another directory.
        value, protected = owner(renamed)
        if protected:
            os.rename(renamed, path)
            sys.exit(1)
        shutil.rmtree(renamed)
        print(value or 'NONE')
    elif action == 'backstop-check':
        p = S / 'backstop.log'
        if p.exists(): p.write_text(''.join(p.read_text().splitlines(keepends=True)[-200:]))
        now = time.time()
        try: started = int((S / 'listener.lock.d/started').read_text())
        except (OSError, ValueError): started = 0
        fresh = 0 <= now - started < 180
        try: lines = (S / 'listen.log').read_text().splitlines()
        except FileNotFoundError: lines = []
        for line in reversed(lines):
            if ' POLL ' not in line: continue
            try:
                # New logs carry a date; accept old HH:MM:SS across midnight.
                stamp = line.split(' POLL ', 1)[0]
                if len(stamp) == 8:
                    dt = datetime.datetime.combine(datetime.date.today(), datetime.time.fromisoformat(stamp))
                    when = dt.timestamp()
                    if when > now: when -= 86400
                else: when = datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%S%z').timestamp()
                fresh = fresh or 0 <= now - when < 180
            except ValueError: pass
            break
        print('RUNNING' if fresh else 'WEDGED')
    elif action == 'quota':
        percent, resets = args
        if not re.fullmatch(r'[0-9]{1,3}', percent) or not 0 <= int(percent) <= 100:
            print('BAD_PERCENT'); sys.exit(1)
        p = S / 'quota_warned'
        if p.exists() and time.time() - p.stat().st_mtime < 20 * 3600:
            print('ALREADY_WARNED')
        else:
            print(json.dumps({'markdown_body': "Heads up: I have used {}% of today's Hark tokens. If I go quiet, that is why; the allowance resets at {}.".format(int(percent), resets)}))
    elif action == 'validate':
        run, number, mode = pathlib.Path(args[0]), args[1], args[2]
        try:
            item = read(S / 'items' / (number + '.json'))
            if not isinstance(item, dict) or not isinstance(item.get('messages'), list) or any(not isinstance(m, dict) for m in item['messages']):
                raise ValueError('invalid item')
            rw = item.get('reply_with')
            if mode != 'ack' and (not isinstance(rw, dict) or not isinstance(rw.get('tool'), str) or not rw['tool'] or '\n' in rw['tool'] or not isinstance(rw.get('args'), dict)):
                raise ValueError('invalid reply target')
        except (ValueError, OSError, TypeError):
            print('BAD_ITEM'); sys.exit()
        # Markdown is UTF-8; read bytes to preserve CRLF and all other whitespace.
        body = (run / 'body').read_bytes()
        if body.endswith(b'\n'): body = body[:-1]
        try: body = body.decode('utf-8')
        except UnicodeDecodeError:
            print('BAD_ITEM'); sys.exit()
        if mode != 'ack' and not body: print('EMPTY_BODY'); sys.exit()
        ids = [m['event_id'] for m in item['messages'] if isinstance(m.get('event_id'), str) and m['event_id']]
        if not ids: print('NO_IDS'); sys.exit()
        write(run / 'validated', {'item': item, 'body': body, 'ids': ids})
        print('ok')
    elif action == 'plan':
        run, mode = pathlib.Path(args[0]), args[1]
        v = read(run / 'validated'); ids = v['ids']; seen = seen_ids()
        ack = mode == 'ack' or all(i in seen for i in ids)
        if ack:
            tool, payload = 'poll_work', {'wait_seconds': 0, 'max_items': 0, 'ack': ids}
        else:
            rw = v['item']['reply_with']
            tool, payload = rw['tool'], dict(rw['args'], markdown_body=v['body'])
        write(run / 'args', payload)
        (run / 'tool').write_text(tool, encoding='utf-8')
        # Do not issue a request unless durable local recording has succeeded.
        unique = list(dict.fromkeys(ids))
        write(S / 'replied.json', {'ids': ([i for i in seen if i not in unique] + unique)[-500:]})
        print('ack' if ack else 'reply')
except (ValueError, OSError, TypeError, KeyError) as e:
    print(str(e)[:1800], file=sys.stderr)
    sys.exit(1)
PY
}

log_event() { printf '%s %.1980s\n' "$(date +%T)" "$*" >> "$STATE/timing.log"; }
log_file() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%.2000s\n' "$line" >> "$STATE/$2"
    done < "$1"
    if [ "$(wc -c < "$1")" -gt 2000 ]; then cp "$1" "$STATE/last_result.json"; fi
}
sleep_for() {
    local duration=$1
    if [ "${FILAMENT_TEST_FAST:-}" = 1 ]; then
        duration=$(awk -v n="$duration" 'BEGIN {printf "%.4f", n/10}')
    fi
    sleep "$duration"
}
remaining() { echo $((DEADLINE - $(date +%s))); }
bounded_sleep() {
    local rem duration
    rem=$(remaining); [ "$rem" -gt 0 ] || return 0
    duration=$(awk -v n="$1" -v r="$rem" 'BEGIN {print (n<r?n:r)}')
    sleep_for "$duration"
}
live_pid() {
    [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -gt 1 ] && kill -0 "$1" 2>/dev/null
}
live_listener() { live_pid "$1" && [[ $(ps -o args= -p "$1" 2>/dev/null) == *listen.sh* ]]; }
release_lock() {
    if [ "$(cat "$1/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$1"; fi
}
reclaim_stale() {
    local owner
    owner=$(json_helper reclaim "$1" "$$" 2>/dev/null) || return 1
    log_event "LOCK-RECLAIMED $1 $owner"
}
rotate_logs() {
    # media.sh uses this without init_job; give its rotation a local budget.
    local DEADLINE=${DEADLINE:-$(($(date +%s) + 60))} rc
    take_lock "$STATE/rotate.lock.d" 5 || return 0
    json_helper rotate; rc=$?
    release_lock "$STATE/rotate.lock.d"
    return "$rc"
}
take_lock() {
    local path=$1 tries=$2 i=0 stop now reclaimed=0
    stop=$(wait_limit "$(((tries + 4) / 5))")
    while ! mkdir "$path" 2>/dev/null; do
        if [ "$reclaimed" -eq 0 ]; then
            reclaimed=1
            reclaim_stale "$path" && continue
        fi
        now=$(date +%s)
        [ "$i" -lt "$tries" ] && [ "$now" -lt "$stop" ] && [ "$now" -lt "$DEADLINE" ] || return 1
        i=$((i + 1)); sleep_for 0.2
    done
    printf '%s\n' "$$" > "$path/pid"
}
wait_limit() {
    local seconds=$1
    if [ "${FILAMENT_TEST_FAST:-}" = 1 ]; then seconds=$(((seconds + 9) / 10)); fi
    echo "$(($(date +%s) + seconds))"
}
kill_tree() {
    local pid=$1 child
    # ps is portable across Linux and macOS; children are killed before parents.
    for child in $(ps -ax -o pid= -o ppid= | awk -v p="$pid" '$2==p {print $1}'); do
        kill_tree "$child"
    done
    kill -TERM "$pid" 2>/dev/null || :
    kill -KILL "$pid" 2>/dev/null || :
}
stop_child() {
    if [ -n "${CPID:-}" ]; then kill_tree "$CPID"; wait "$CPID" 2>/dev/null || :; CPID=; fi
}
run_mcp() {
    local rem cap=$3 rc
    rem=$(remaining)
    [ "$rem" -ge 10 ] || return 125
    [ "$cap" -le "$rem" ] || cap=$rem
    HARK_MCP_TIMEOUT="${cap}s" mcpcall call filament "$1" "$2" > "$RUN/out" 2> "$RUN/err" &
    CPID=$!
    wait "$CPID"; rc=$?; CPID=
    if [ "$(wc -c < "$RUN/out")" -gt 2000 ]; then cp "$RUN/out" "$STATE/last_result.json"; fi
    return "$rc"
}
mark_failure() {
    local kind
    kind=$(classify_error "$(cat "$RUN/err")")
    case "$kind" in auth|reserved) printf '%s\n' "$kind" > "$STATE/auth_failed";; esac
}
paused() {
    [ -f "$STATE/auth_failed" ] || return 1
    if [ "$(cat "$STATE/auth_failed")" = reserved ]; then echo AGENT_RESERVED; else echo AUTH_FAILED; fi
}
init_job() {
    STATE=${FILAMENT_STATE:-/workspace/filament}
    mkdir -p "$STATE" || return 1
    local start=${FILAMENT_JOB_START:-}
    if ! [[ $start =~ ^[0-9]{1,12}$ ]]; then start=$(date +%s); fi
    start=$((10#$start))
    DEADLINE=$((start + ${1:-480}))
    CPID=
    rotate_logs || return 1
}

stop_listener() {
    local old i=0 stop
    old=$(cat "$STATE/listener.lock.d/pid" 2>/dev/null)
    live_listener "$old" || return 1
    kill -TERM "$old" 2>/dev/null || :
    stop=$(wait_limit 10)
    while live_listener "$old" && [ "$i" -lt 100 ] && [ "$(date +%s)" -lt "$stop" ]; do
        sleep_for 0.1; i=$((i + 1))
    done
    if live_listener "$old"; then kill -KILL "$old" 2>/dev/null || :; fi
    log_event "LISTENER-KILLED $old"
    # Reclaim only stale ownership, never blindly remove a replacement.
    reclaim_stale "$STATE/listener.lock.d" || :
    if [ "$(cat "$STATE/listener.pid" 2>/dev/null)" = "$old" ]; then rm -f "$STATE/listener.pid"; fi
    return 0
}

restart_main() {
    init_job 480 || { echo RESTART_BUSY; exit 3; }
    if [ "${1:-}" != --stop ]; then paused && exit 2; fi
    if ! take_lock "$STATE/restart.lock.d" 100; then echo RESTART_BUSY; exit 3; fi
    trap 'release_lock "$STATE/reply.lock.d"; release_lock "$STATE/restart.lock.d"' EXIT
    trap 'echo KILLED; exit 3' TERM INT
    # A tiny handover record closes the release-before-exec race. Its owner is
    # this PID, which exec preserves; it is removed by the incoming listener.
    local pending pending_args old stop i=0
    stop=$(wait_limit 20)
    pending=$(cat "$STATE/restart.pending" 2>/dev/null)
    while live_pid "$pending" && [ "$pending" != "$$" ] && [ -f "$STATE/restart.pending" ]; do
        pending_args=$(ps -o args= -p "$pending" 2>/dev/null)
        [[ $pending_args == *restart_listener.sh* || $pending_args == *listen.sh* ]] || break
        if [ "$i" -ge 100 ] || [ "$(date +%s)" -ge "$stop" ] || [ "$(remaining)" -le 0 ]; then echo RESTART_BUSY; exit 3; fi
        sleep_for 0.2; i=$((i + 1))
    done
    old=$(cat "$STATE/listener.lock.d/pid" 2>/dev/null)
    if [ "${1:-}" = --stop ]; then
        if stop_listener; then echo STOPPED; else echo NO_LISTENER; fi
        exit 0
    fi
    if live_listener "$old"; then
        if [ "${1:-}" != --replace ]; then
            [ -z "${FILAMENT_JOB_START+x}" ] || log_event CHAIN-ALREADY-LISTENING
            echo ALREADY_LISTENING; exit 3
        fi
        stop_listener || :
    fi
    if [ -z "${FILAMENT_JOB_START+x}" ]; then
        if ! take_lock "$STATE/reply.lock.d" 25; then echo RESTART_BUSY; exit 3; fi
        if ! json_helper lineage reset; then echo RESTART_BUSY; exit 3; fi
        release_lock "$STATE/reply.lock.d"
    fi
    printf '%s\n' "$$" > "$STATE/restart.pending"
    release_lock "$STATE/restart.lock.d"
    trap - EXIT TERM INT
    exec bash "$STATE/listen.sh"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then restart_main "$@"; fi
