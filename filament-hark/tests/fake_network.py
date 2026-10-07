#!/usr/bin/env python3
"""Offline, scenario-driven mcpcall/curl with atomic call counting."""
import json
import os
from pathlib import Path
import sys
import time
from process_tools import register

state = Path(os.environ['FILAMENT_STATE'])
if os.environ.get('FILAMENT_TEST_PS_CATALOGUE') == '1': register(' '.join(sys.argv))
kind, *argv = sys.argv[1:]
if kind == 'curl':
    key, args = ('heartbeat' if '-X' in argv else 'media'), {}
    timeout = float(argv[argv.index('-m') + 1])
    if key == 'heartbeat':
        assert argv[argv.index('-X') + 1] == 'POST'
        assert 'https://api.filament.dm/mcp/agents/heartbeat' in argv
    else:
        args = {'url': argv[-1]}
        assert '-f' in argv or '--fail' in argv
else:
    assert argv[:2] == ['call', 'filament'], argv
    key, args = argv[2], json.loads(argv[3])
    assert len(argv) == 4, argv
    timeout = float(os.environ['HARK_MCP_TIMEOUT'].removesuffix('s'))
    if key == 'poll_work':
        assert 'cursor' not in args
        if args.get('max_items') == 0: key = 'ack'
assert timeout > 0
lock = state / 'fake.lock.d'
until = time.monotonic() + 10
while True:
    try:
        lock.mkdir(); break
    except FileExistsError:
        if time.monotonic() > until: raise
        time.sleep(.005)
try:
    scenario = json.loads((state / 'scenario.json').read_text())
    counters_file = state / 'counts.json'
    counters = json.loads(counters_file.read_text()) if counters_file.exists() else {}
    index = counters.get(key, 0)
    counters[key] = index + 1
    counters_file.write_text(json.dumps(counters))
    defaults = {
        'heartbeat': {}, 'get_self': {'out': {'user_id': '@self:test', 'owner_id': '@owner:test', 'display_name': 'Test'}},
        'list_pending_invites': {'out': {'invites': []}}, 'list_vouches': {'out': {'vouches': []}},
        'accept_invite': {'out': {}}, 'accept_vouch': {'out': {}},
        'poll_work': {'out': {'work': [], 'next_poll_ms': 60000}},
        'get_recent_messages': {'out': {'messages': []}},
        'ack': {'out': {}}, 'send_reply': {'out': {'ok': True}},
    }
    entries = scenario.get(key, [defaults.get(key, {'rc': 98, 'err': 'Unexpected tool'})])
    response = entries[min(index, len(entries) - 1)]
    ids = response.get('assert_recorded', [])
    if ids:
        recorded = json.loads((state / 'replied.json').read_text())['ids']
        assert all(i in recorded for i in ids), (ids, recorded)
        assert (state / 'reply.lock.d').is_dir(), 'reply lock not held through request'
    call = {'key': key, 'args': args, 'timeout': timeout, 'pid': os.getpid(), 'at': time.time(),
            'job_start': os.environ.get('FILAMENT_JOB_START')}
    with open(state / 'calls.jsonl', 'a') as f: f.write(json.dumps(call) + '\n')
finally:
    lock.rmdir()
delay = response.get('delay', 0)
if delay == 'poll_wait': delay = args['wait_seconds']
time.sleep(min(delay, timeout))
if delay > timeout:
    print('request timed out', file=sys.stderr)
    sys.exit(124)
if key == 'media':
    Path(argv[argv.index('-o') + 1]).write_bytes(b'\x89PNG\r\n\x1a\n')
    Path(argv[argv.index('-D') + 1]).write_text(
        'HTTP/1.1 200 OK\r\nContent-Type: ' + response.get('content_type', 'image/png') +
        '\r\nContent-Length: ' + str(response.get('content_length', 8)) + '\r\n\r\n')
    if 'bytes' in response:
        Path(argv[argv.index('-o') + 1]).write_bytes(bytes.fromhex(response['bytes']))
    if 'headers' in response:
        Path(argv[argv.index('-D') + 1]).write_text(response['headers'])
if 'raw' in response: print(response['raw'])
elif 'out' in response: print(json.dumps(response['out'], ensure_ascii=True))
if response.get('err'): print(response['err'], file=sys.stderr)
sys.exit(response.get('rc', 0))
