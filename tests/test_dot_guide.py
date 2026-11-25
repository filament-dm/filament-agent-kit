import re
import unittest
from pathlib import Path

GUIDE = Path(__file__).resolve().parents[1] / "filament-dot" / "DOT.md"


def section(text, heading):
    start = text.index(heading)
    end = text.find("\n#", start + len(heading))
    return text[start:end if end != -1 else None]


class DotListenerTests(unittest.TestCase):
    """The dot's two automations start a listening session from the skill
    bundled in the Filament plugin, not a one-message turn from a fetched
    page. filament-openai's skills/filament-agent/SKILL.md carries the same
    instructions."""

    def setUp(self):
        self.text = GUIDE.read_text()
        part_c = section(self.text, "## Part C: listen")
        self.instructions = re.findall(r'Its instruction is: "([^"]+)"', part_c)

    def test_two_automations(self):
        self.assertEqual(len(self.instructions), 2)

    def test_routine_wake_uses_the_bundled_skill(self):
        for instruction in self.instructions:
            with self.subTest(instruction=instruction[:40]):
                self.assertIn("Run the listen loop from the Filament plugin's filament-agent skill", instruction)
                fetches = re.findall(r"[^.]*\bfetch\b[^.]*\.", instruction, re.IGNORECASE)
                self.assertEqual(len(fetches), 1, fetches)
                self.assertIn("Only if the plugin has no filament-agent skill", fetches[0])

    def test_routine_wake_skips_identity_lookup(self):
        for instruction in self.instructions:
            with self.subTest(instruction=instruction[:40]):
                self.assertIn("Reuse the saved Filament binding and the last completed cursor", instruction)
                self.assertIn("Do not call get_self", instruction)
        loop = section(self.text, "### The listen loop")
        self.assertNotIn("First, call `get_self`", loop)
        self.assertIn("On a routine wake, do not call `get_self`", loop)

    def test_session_keeps_polling(self):
        task, watch = self.instructions
        self.assertIn("for up to 50 minutes", task)
        self.assertIn("for up to nine minutes", watch)
        for instruction in self.instructions:
            with self.subTest(instruction=instruction[:40]):
                self.assertIn("server_time", instruction)
                self.assertIn("Keep polling after an empty poll and after a reply", instruction)
        self.assertIn("never reply from it", watch)
        self.assertIn("exit", watch)

    def test_owner_is_authenticated_by_flag_and_saved_id(self):
        loop = section(self.text, "### The listen loop")
        self.assertIn("`is_from_principal` true whose `sender` is the saved owner ID", loop)

    def test_update_leaves_connection_and_cursor_alone(self):
        updating = section(self.text, "## Updating")
        self.assertIn(
            "Do not touch the plugin connection or its sign-in, your profile, "
            "the event subscription or the saved cursor",
            updating,
        )


if __name__ == "__main__":
    unittest.main()
