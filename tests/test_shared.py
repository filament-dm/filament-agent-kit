import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "shared", Path(__file__).resolve().parents[1] / "scripts" / "shared.py"
)
shared = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shared
SPEC.loader.exec_module(shared)

START = "<!-- shared:loop:start -->\n"
END = "<!-- shared:loop:end -->"


class SharedBlocksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "shared").mkdir()
        (self.root / "shared" / "loop.md").write_text("Answer once.\n")
        (self.root / "a").mkdir()
        self.skill = self.root / "a" / "SKILL.md"

    def tearDown(self):
        self.dir.cleanup()

    def test_sync_fills_a_stale_copy(self):
        self.skill.write_text(f"# A\n{START}old\n{END}\ntail\n")
        self.assertEqual(shared.sync(self.root, check=False), ["a/SKILL.md"])
        self.assertEqual(self.skill.read_text(), f"# A\n{START}Answer once.\n{END}\ntail\n")

    def test_check_reports_without_writing(self):
        self.skill.write_text(f"{START}old\n{END}\n")
        self.assertEqual(shared.sync(self.root, check=True), ["a/SKILL.md"])
        self.assertIn("old", self.skill.read_text())

    def test_current_copy_passes(self):
        self.skill.write_text(f"{START}Answer once.\n{END}\n")
        self.assertEqual(shared.sync(self.root, check=True), [])

    def test_unknown_block_is_an_error(self):
        self.skill.write_text("<!-- shared:nope:start -->\nx\n<!-- shared:nope:end -->\n")
        with self.assertRaises(shared.SharedError):
            shared.sync(self.root, check=True)

    def test_sources_are_not_carriers(self):
        (self.root / "shared" / "loop.md").write_text(f"{START}x\n{END}\n")
        self.assertEqual(shared.sync(self.root, check=True), [])

    def test_kit_copies_match_their_sources(self):
        self.assertEqual(shared.sync(shared.ROOT, check=True), [])


if __name__ == "__main__":
    unittest.main()
