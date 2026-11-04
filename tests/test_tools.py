import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_LIST = re.compile(r"<!-- tool-list:start -->.*?<!-- tool-list:end -->", re.S)


def tool_list(path):
    blocks = TOOL_LIST.findall((ROOT / path).read_text())
    if len(blocks) != 1:
        raise AssertionError(f"{path} has {len(blocks)} tool lists, expected one")
    return blocks[0]


class ToolListTests(unittest.TestCase):
    def test_every_tool_is_named_once(self):
        names = re.findall(r"`([a-z_]+)`", tool_list("filament-tools/TOOLS.md"))
        tools = [n for n in names if n not in {"reply_with"}]
        self.assertEqual(len(tools), len(set(tools)), tools)


if __name__ == "__main__":
    unittest.main()
