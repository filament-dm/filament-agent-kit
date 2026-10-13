#!/usr/bin/env python3
"""Release pin: write filament-instinct/poll.sh from scripts/poll.sh with PINNED filled.

Usage: python3 pin.py <commit-sha>
The five Python scripts are pinned to raw.githubusercontent.com at that commit with
their sha256. scripts/poll.sh stays unpinned so the test suite can exercise installs
against its fake server. Prints the sha256 of the written poll.sh for INSTINCT.md.
"""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES = ('fm_lib.py', 'fm_poll.py', 'fm_reply.py', 'fm_hb.py', 'fm_media.py')
BASE = 'https://raw.githubusercontent.com/filament-dm/filament-agent-kit/{}/filament-instinct/scripts/{}'


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    sha = sys.argv[1]
    pinned = {n: [BASE.format(sha, n), hashlib.sha256((HERE / 'scripts' / n).read_bytes()).hexdigest()]
              for n in NAMES}
    src = (HERE / 'scripts' / 'poll.sh').read_text()
    marker = 'PINNED = {}  # RELEASE_PIN: URLs and sha256 are embedded here at release time.'
    assert marker in src, 'marker missing'
    body = 'PINNED = {\n' + ''.join("    '{}': ['{}', '{}'],\n".format(n, u, d) for n, (u, d) in pinned.items()) + '}'
    out = src.replace(marker, body)
    (HERE / 'poll.sh').write_text(out)
    (HERE / 'poll.sh').chmod(0o755)
    print(hashlib.sha256(out.encode()).hexdigest())
    return 0


if __name__ == '__main__':
    sys.exit(main())
