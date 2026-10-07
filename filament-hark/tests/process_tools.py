#!/usr/bin/env python3
"""Restricted-sandbox fixture only. No production script imports this module.

Catalogue actual test-owned PIDs at launch/exec. Emulate only the two ps queries
used by the scripts when the host refuses to execute ps at all.
"""
import json
import os
from pathlib import Path
import sys

def register(command):
    folder = Path(os.environ['FILAMENT_STATE']) / 'process-catalogue'
    folder.mkdir(exist_ok=True)
    pid = os.getpid()
    tmp = folder / (str(pid) + '.tmp')
    tmp.write_text(json.dumps({'pid': pid, 'ppid': os.getppid(), 'args': command}))
    os.replace(tmp, folder / str(pid))

def main():
    if Path(sys.argv[0]).name == 'bash':
        actual = os.environ['FILAMENT_TEST_REAL_BASH']
        register(' '.join([actual, *sys.argv[1:]]))
        os.execv(actual, [actual, *sys.argv[1:]])
    else:
        rows = []
        for p in (Path(os.environ['FILAMENT_STATE']) / 'process-catalogue').glob('*'):
            if p.suffix == '.tmp': continue
            d = json.loads(p.read_text())
            try: os.kill(d['pid'], 0)
            except ProcessLookupError: continue
            rows.append(d)
        if sys.argv[1:] == ['-ax', '-o', 'pid=', '-o', 'ppid=']:
            for row in rows: print(row['pid'], row['ppid'])
        elif sys.argv[1:3] == ['-o', 'args='] and sys.argv[3] == '-p':
            for row in rows:
                if str(row['pid']) == sys.argv[4]: print(row['args']); return
            sys.exit(1)
        else:
            raise ValueError('unsupported ps query: ' + repr(sys.argv))

if __name__ == '__main__': main()
