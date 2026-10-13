#!/usr/bin/env python3
"""Detached presence owner while a batch is being enriched or answered."""
import os
import sys
sys.dont_write_bytecode = True
sys.path[0] = os.path.expanduser(os.environ.get('FM_STATE', '~/.filament'))
from fm_lib import *
import signal


def main():
    rotate_logs()
    stopped = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopped.__setitem__(0, True))
    # A separate guard serialises replacers; the old keeper can still clean up
    # under state_guard while we wait for its exit.
    with open(STATE / '.hb.guard', 'a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        with state_guard():
            old = int(rd('hb.pid', '0') or 0)
            if alive(old) and old != os.getpid():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(old, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while alive(old) and time.monotonic() < deadline:
            sleep(0.1)
        with state_guard():
            wr('hb.pid', str(os.getpid()))
    started = time.monotonic()
    predecessor = int(os.environ.get('FM_HB_PREDECESSOR', '0'))
    n, reason = 0, '40 min cap'
    log('hb keeper start pid=' + str(os.getpid()))
    try:
        while time.monotonic() - started < 2400:
            if stopped[0] or rd('hb.pid') != str(os.getpid()):
                reason = 'TERM/replaced'
                break
            age, pid = lock_read()
            if pid != predecessor and lock_alive() and age < 30:
                reason = 'listener live'
                break
            log('hb keeper heartbeat ' + str(heartbeat()))
            n += 1
            # Responsive TERM and presence handover, still one heartbeat per 20 s.
            deadline = time.monotonic() + (2 if FAST else 20)
            while time.monotonic() < deadline and not stopped[0]:
                sleep(0.5)
                age, pid = lock_read()
                if pid != predecessor and lock_alive() and age < 30:
                    break
    finally:
        log('hb keeper stop: {} ({} heartbeats)'.format(reason, n))
        with state_guard():
            if rd('hb.pid') == str(os.getpid()):
                remove('hb.pid')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FileNotFoundError:
        pass
