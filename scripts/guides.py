#!/usr/bin/env python3
"""Check that guide changes include a version bump. Never edit guides or publish."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "guides.json"
Reader = Callable[[str], bytes | None]


class GuideError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Version:
    day: date
    sequence: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.([1-9]\d*)", value)
        if not match:
            raise GuideError(f"Invalid version {value!r}; expected YYYY-MM-DD.N")
        try:
            return cls(date.fromisoformat(match[1]), int(match[2]))
        except ValueError as exc:
            raise GuideError(f"Invalid version date in {value!r}") from exc

    def __str__(self) -> str:
        return f"{self.day.isoformat()}.{self.sequence}"


@dataclass(frozen=True)
class Guide:
    directory: str
    name: str

    @property
    def path(self) -> str:
        return f"{self.directory}/{self.name}"

    def covers(self, path: str) -> bool:
        return path.startswith(f"{self.directory}/")


@dataclass(frozen=True)
class Finding:
    path: str
    message: str


def source_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", value)
        or value.startswith("/")
        or any(part in {".", "..", ".git"} for part in value.rstrip("/").split("/"))
        or str(PurePosixPath(value)) != value.rstrip("/")
    ):
        raise GuideError(f"Invalid repository path: {value!r}")
    return value


def inventory(read: Reader, *, optional: bool = False) -> dict[str, Guide]:
    raw = read(INVENTORY)
    if raw is None:
        if optional:
            return {}
        raise GuideError(f"Missing {INVENTORY}")
    try:
        entries = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise GuideError(f"Invalid {INVENTORY}: {exc}") from exc
    if not isinstance(entries, dict):
        raise GuideError(f"{INVENTORY} must map guide IDs to path and guide")
    result = {}
    paths = set()
    for guide_id, spec in entries.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", guide_id):
            raise GuideError(f"Invalid guide ID: {guide_id!r}")
        if not isinstance(spec, dict) or set(spec) != {"path", "guide"}:
            raise GuideError(f"{guide_id}: expected path and guide")
        directory = source_path(spec["path"]).rstrip("/")
        name = source_path(spec["guide"])
        if "/" in name or not name.endswith(".md") or directory in paths:
            raise GuideError(f"{guide_id}: expected a Markdown filename and unique directory")
        paths.add(directory)
        result[guide_id] = Guide(directory, name)
    return result


def guide_version(raw: bytes | None, guide_id: str) -> Version:
    if raw is None:
        raise GuideError("Guide file is missing")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise GuideError("Guide must be UTF-8") from exc
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise GuideError("Missing frontmatter with guide_id and version")
    fields = {}
    for line in lines[1 : lines.index("---", 1)]:
        if not line.strip():
            continue
        match = re.fullmatch(r"(guide_id|version):\s*(\S.*?)\s*", line)
        if not match or match[1] in fields:
            raise GuideError("Use one flat guide_id and version field in frontmatter")
        value = match[2]
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise GuideError("Unclosed frontmatter quote")
            value = value[1:-1]
        fields[match[1]] = value
    if fields.get("guide_id") != guide_id:
        raise GuideError(f"Frontmatter guide_id must be {guide_id!r}")
    return Version.parse(fields.get("version", ""))


def check_versions(before: Reader, after: Reader, changed: set[str]) -> list[Finding]:
    old_guides = inventory(before, optional=True)
    guides = inventory(after)
    findings = []
    for guide_id in sorted(old_guides.keys() | guides.keys()):
        old = old_guides.get(guide_id)
        guide = guides.get(guide_id)
        if guide is None:
            findings.append(
                Finding(
                    INVENTORY,
                    f"{guide_id}: removed from version checks; review this explicitly",
                )
            )
            continue
        try:
            current = guide_version(after(guide.path), guide_id)
        except GuideError as exc:
            findings.append(Finding(guide.path, f"{guide_id}: {exc}"))
            continue
        affected = old != guide or any(
            guide.covers(path) or (old is not None and old.covers(path))
            for path in changed
        )
        if not affected:
            continue
        try:
            previous = guide_version(before(old.path if old else guide.path), guide_id)
        except GuideError as exc:
            if old is not None:
                findings.append(
                    Finding(
                        guide.path,
                        f"{guide_id}: base version cannot be compared ({exc}); review the initial version",
                    )
                )
            continue
        if current <= previous:
            findings.append(
                Finding(
                    guide.path,
                    f"{guide_id}: covered content changed but version {current} is not newer than base {previous}; bump the guide version",
                )
            )
    return findings


def git(*args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if result.returncode:
        raise GuideError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def resolve(ref: str) -> str:
    return (
        git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode()
        .strip()
    )


def committed_reader(commit: str) -> Reader:
    paths = set(git("ls-tree", "-r", "--name-only", "-z", commit).decode().split("\0"))

    def read(path: str) -> bytes | None:
        return git("show", f"{commit}:{path}") if path in paths else None

    return read


def working_reader(path: str) -> bytes | None:
    target = ROOT / path
    if not target.resolve().is_relative_to(ROOT):
        raise GuideError(f"Path escapes the repository: {path}")
    return target.read_bytes() if target.is_file() else None


def command_escape(text: str) -> str:
    for char, escaped in (
        ("%", "%25"),
        ("\r", "%0D"),
        ("\n", "%0A"),
        (":", "%3A"),
        (",", "%2C"),
    ):
        text = text.replace(char, escaped)
    return text


def print_check(findings: list[Finding], github: bool) -> None:
    for error in findings:
        if github:
            print(
                f"::error file={command_escape(error.path)},line=1,title=Guide version::{command_escape(error.message)}"
            )
        else:
            print(f"ERROR {error.path}: {error.message}")
    result = (
        f"{len(findings)} guide version error(s)."
        if findings
        else "No guide version findings."
    )
    print(result)
    if github and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"## Guide versions\n\n{result}\n\n")
            for error in findings:
                summary.write(
                    f"- <code>{html.escape(error.path)}</code>: {html.escape(error.message)}\n"
                )
            summary.write(
                "\nVersion errors fail this check. Merge blocking depends on repository rules.\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Current PR target ref")
    parser.add_argument("--head", help="PR head ref; default is the working tree")
    parser.add_argument("--github", action="store_true")
    args = parser.parse_args(argv)
    try:
        base = resolve(args.base)
        head = resolve(args.head or "HEAD")
        merge_base = git("merge-base", base, head).decode().strip()
        diff_args = ["diff", "--name-only", "--no-renames", "-z", merge_base]
        if args.head:
            diff_args.append(head)
        changed = set(git(*diff_args, "--").decode().split("\0")) - {""}
        if not args.head:
            changed.update(
                git("ls-files", "--others", "--exclude-standard", "-z")
                .decode()
                .split("\0")
            )
        after = committed_reader(head) if args.head else working_reader
        findings = check_versions(committed_reader(base), after, changed)
        print_check(findings, args.github)
        return 1 if findings else 0
    except (GuideError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
