#!/usr/bin/env python3
"""Copy shared text into the guides and skills that carry it.

Text written once under shared/ appears in each file that marks a block for
it, between `<!-- shared:<name>:start -->` and `<!-- shared:<name>:end -->`.
Each copy is part of its file, so installed plugins and fetched guides stay
self-contained. Run with no arguments to rewrite the copies, or with --check
to fail when any copy differs from its source.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = "shared"
BLOCK = re.compile(
    r"(<!-- shared:(?P<name>[a-z0-9-]+):start -->\n)(?P<body>.*?)(<!-- shared:(?P=name):end -->)",
    re.DOTALL,
)


class SharedError(Exception):
    pass


def sources(root: Path) -> dict[str, str]:
    return {p.stem: p.read_text() for p in sorted((root / SHARED).glob("*.md"))}


def carriers(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*.md")
        if ".git" not in p.parts and p.relative_to(root).parts[0] != SHARED
    )


def render(text: str, shared: dict[str, str], where: str) -> str:
    def fill(match: re.Match) -> str:
        name = match.group("name")
        if name not in shared:
            raise SharedError(f"{where}: no {SHARED}/{name}.md for its shared:{name} block")
        return match.group(1) + shared[name] + match.group(4)

    return BLOCK.sub(fill, text)


def sync(root: Path, check: bool) -> list[str]:
    """Returns the files whose copies differ (check) or were rewritten."""
    shared = sources(root)
    changed = []
    for path in carriers(root):
        text = path.read_text()
        if "<!-- shared:" not in text:
            continue
        rel = str(path.relative_to(root))
        new = render(text, shared, rel)
        if new != text:
            changed.append(rel)
            if not check:
                path.write_text(new)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on stale copies")
    args = parser.parse_args(argv)
    try:
        changed = sync(ROOT, args.check)
    except SharedError as error:
        print(error, file=sys.stderr)
        return 2
    for rel in changed:
        print(f"{rel}: {'differs from' if args.check else 'updated from'} {SHARED}/")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
