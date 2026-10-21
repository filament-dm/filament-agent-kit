import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "guides", Path(__file__).resolve().parents[1] / "scripts" / "guides.py"
)
guides = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guides
SPEC.loader.exec_module(guides)

PATH = "filament-grokbot/GROKBOT.md"
INVENTORY = json.dumps(
    {"grokbot": {"path": "filament-grokbot", "guide": "GROKBOT.md"}}
).encode()
RETIRED = json.dumps({"grokbot": {
    "path": "filament-grokbot",
    "guide": "GROKBOT.md",
    "retired": {
        "reason": "New agents use grokbot-mcp (MCP Events); existing agents stay here"
    },
}}).encode()


def document(version="2026-10-15.1", body="Current setup.", guide_id="grokbot"):
    return f'---\nguide_id: {guide_id}\nversion: "{version}"\n---\n\n{body}\n'.encode()


def snapshot(version="2026-10-15.1", body="Current setup."):
    return {"guides.json": INVENTORY, PATH: document(version, body)}


class VersionTests(unittest.TestCase):
    def test_daily_sequence_is_numeric(self):
        self.assertGreater(
            guides.Version.parse("2026-10-15.10"),
            guides.Version.parse("2026-10-15.9"),
        )

    def test_reject_invalid_versions(self):
        for value in ("2026-02-30.1", "2026-10-15", "2026-10-15.0", "2026-10-15.01"):
            with self.subTest(value=value), self.assertRaises(guides.GuideError):
                guides.Version.parse(value)

    def test_frontmatter_identity_must_match_inventory(self):
        with self.assertRaises(guides.GuideError):
            guides.guide_version(document(), "dot")

    def test_duplicate_metadata_is_invalid(self):
        raw = document().replace(b"\nversion:", b"\nguide_id: grokbot\nversion:")
        with self.assertRaises(guides.GuideError):
            guides.guide_version(raw, "grokbot")


class CheckTests(unittest.TestCase):
    def check(self, before, after, changed):
        return guides.check_versions(before.get, after.get, set(changed))

    def test_initial_enrollment_of_existing_unversioned_guide(self):
        before = {PATH: b"# Existing guide\n"}
        self.assertEqual(self.check(before, snapshot(), [PATH, "guides.json"]), [])

    def test_new_guide_requires_version(self):
        after = snapshot()
        after[PATH] = b"# No frontmatter\n"
        self.assertIn("Missing frontmatter", self.check({}, after, [PATH])[0].message)

    def test_changed_guide_without_bump_fails(self):
        findings = self.check(snapshot(), snapshot(body="Changed setup."), [PATH])
        self.assertEqual(len(findings), 1)
        self.assertIn("bump the guide version", findings[0].message)

    def test_changed_guide_with_bump_passes(self):
        self.assertEqual(
            self.check(snapshot(), snapshot("2026-10-15.2", "Changed setup."), [PATH]),
            [],
        )

    def test_changed_helper_also_requires_bump(self):
        findings = self.check(
            snapshot(), snapshot(), ["filament-grokbot/scripts/listen.py"]
        )
        self.assertEqual(len(findings), 1)

    def test_unrelated_files_do_not_require_bump(self):
        self.assertEqual(
            self.check(snapshot(), snapshot(), ["README.md", "scripts/guides.py"]), []
        )

    def test_same_prefix_is_not_a_covered_directory(self):
        self.assertEqual(
            self.check(snapshot(), snapshot(), ["filament-grokbot-other/a.md"]), []
        )

    def test_current_base_detects_version_collision(self):
        findings = self.check(
            snapshot("2026-10-15.2"), snapshot("2026-10-15.2", "PR changes"), [PATH]
        )
        self.assertEqual(len(findings), 1)

    def test_missing_registered_guide_fails(self):
        after = {"guides.json": INVENTORY}
        self.assertIn("missing", self.check(snapshot(), after, [PATH])[0].message)

    def test_removed_registration_fails(self):
        after = {"guides.json": b"{}"}
        message = self.check(snapshot(), after, ["guides.json"])[0].message
        self.assertIn("removed", message)
        self.assertIn("mark it retired", message)

    def test_retiring_needs_no_bump(self):
        after = snapshot()
        after["guides.json"] = RETIRED
        self.assertEqual(self.check(snapshot(), after, ["guides.json"]), [])

    def test_retired_guide_still_requires_bump(self):
        before, after = snapshot(), snapshot(body="Retired for new agents; see grokbot-mcp.")
        before["guides.json"] = after["guides.json"] = RETIRED
        self.assertEqual(len(self.check(before, after, [PATH])), 1)

    def test_retired_guide_file_must_remain(self):
        after = {"guides.json": RETIRED}
        self.assertIn("missing", self.check(snapshot(), after, [PATH])[0].message)

    def test_retired_requires_only_a_reason(self):
        for retired in (
            True,
            "Retired",
            {},
            {"reason": " "},
            {"reason": "Retired", "superseded_by": "grokbot-mcp"},
        ):
            spec = {"path": "filament-grokbot", "guide": "GROKBOT.md", "retired": retired}
            after = {"guides.json": json.dumps({"grokbot": spec}).encode()}
            with self.subTest(retired=retired), self.assertRaises(guides.GuideError):
                self.check(snapshot(), after, ["guides.json"])

    def test_changed_guide_id_fails(self):
        after = {
            "guides.json": json.dumps(
                {"grok": {"path": "filament-grokbot", "guide": "GROKBOT.md"}}
            ).encode(),
            PATH: document("2026-10-15.2", guide_id="grok"),
        }
        renamed, removed = self.check(snapshot(), after, ["guides.json", PATH])
        self.assertIn("grok: this guide was 'grokbot' on the base branch", renamed.message)
        self.assertIn("grokbot: removed", removed.message)

    def test_tests_and_documentation_in_directory_require_bump(self):
        for path in ("filament-grokbot/tests/test_loop.py", "filament-grokbot/README.md"):
            with self.subTest(path=path):
                self.assertEqual(len(self.check(snapshot(), snapshot(), [path])), 1)

    def test_moved_directory_does_not_hide_changes(self):
        after = {"guides.json": json.dumps({"grokbot": {
            "path": "new-grokbot", "guide": "GROKBOT.md"
        }}).encode(), "new-grokbot/GROKBOT.md": document()}
        self.assertEqual(len(self.check(snapshot(), after, ["guides.json", PATH])), 1)

    def test_invalid_inventory_is_a_tool_error(self):
        with self.assertRaises(guides.GuideError):
            self.check(snapshot(), {"guides.json": b"not JSON"}, [])

    def test_inventory_cannot_escape_repository(self):
        for value in (
            "../outside.md",
            "/etc/passwd",
            ".git/config",
            "a/../secret",
            "a//b",
        ):
            with self.subTest(value=value), self.assertRaises(guides.GuideError):
                guides.source_path(value)

    def test_findings_are_errors_and_visible_in_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}),
                redirect_stdout(output),
            ):
                guides.print_check([guides.Finding(PATH, "Missing bump")], github=True)
            self.assertIn("::error file=", output.getvalue())
            self.assertIn("Missing bump", summary.read_text())

    def test_annotation_message_preserves_punctuation_and_escapes_newlines(self):
        output = io.StringIO()
        with redirect_stdout(output):
            guides.print_check(
                [guides.Finding(PATH, "grokbot: bump required, 100%\r\nnext line")],
                github=True,
            )
        self.assertEqual(
            output.getvalue().splitlines()[0],
            f"::error file={PATH},line=1,title=Guide version::"
            "grokbot: bump required, 100%25%0D%0Anext line",
        )

    def test_annotation_properties_escape_delimiters(self):
        output = io.StringIO()
        with redirect_stdout(output):
            guides.print_check([guides.Finding("a:b,c%/GUIDE.md", "Missing bump")], github=True)
        self.assertIn("file=a%3Ab%2Cc%25/GUIDE.md,line=1,", output.getvalue())

    def test_check_command_exits_one_for_missing_bump(self):
        with (
            patch.object(guides, "resolve", return_value="a" * 40),
            patch.object(guides, "git", return_value=PATH.encode()),
            patch.object(guides, "committed_reader", return_value=snapshot().get),
            patch.object(guides, "working_reader", snapshot(body="Changed").get),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(guides.main(["--base", "origin/main"]), 1)
        self.assertIn("ERROR", output.getvalue())

    def test_check_command_exits_zero_with_bump(self):
        with (
            patch.object(guides, "resolve", return_value="a" * 40),
            patch.object(guides, "git", return_value=PATH.encode()),
            patch.object(guides, "committed_reader", return_value=snapshot().get),
            patch.object(guides, "working_reader", snapshot("2026-10-15.2").get),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(guides.main(["--base", "origin/main"]), 0)


if __name__ == "__main__":
    unittest.main()
