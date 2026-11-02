import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SiteTests(unittest.TestCase):
    """site/ is served at filament.dm/agents and filament.dm/llms.txt, so
    every guide an agent can be sent to must be listed in both."""

    def test_every_current_guide_is_listed(self):
        inventory = json.loads((ROOT / "guides.json").read_text())
        for name in ("agents.md", "llms.txt"):
            text = (ROOT / "site" / name).read_text()
            for guide_id, entry in inventory.items():
                link = f"https://filament.dm/agents/{guide_id}.md"
                with self.subTest(page=name, guide=guide_id):
                    if "retired" in entry:
                        self.assertNotIn(link, text)
                    else:
                        self.assertIn(link, text)

    def test_listed_guides_exist(self):
        inventory = json.loads((ROOT / "guides.json").read_text())
        for name in ("agents.md", "llms.txt"):
            text = (ROOT / "site" / name).read_text()
            for guide_id in _linked_guides(text):
                with self.subTest(page=name, guide=guide_id):
                    self.assertIn(guide_id, inventory)


def _linked_guides(text):
    prefix = "https://filament.dm/agents/"
    for part in text.split(prefix)[1:]:
        name = part.split(".md", 1)[0]
        if name and all(c.isalnum() or c == "-" for c in name):
            yield name


if __name__ == "__main__":
    unittest.main()
