"""Keep the published reference and downloadable relay in sync and secret-free."""
import json
import re
import unittest
from pathlib import Path

TAB = Path(__file__).resolve().parents[1]


class RelaySourceTest(unittest.TestCase):
    def test_reference_is_the_downloadable_file(self):
        guide = (TAB / 'TAB.md').read_text()
        reference = guide.split('## Reference relay', 1)[1].split('```js\n', 1)[1].split('```', 1)[0]
        self.assertEqual(reference, (TAB / 'relay.mjs').read_text())

    def test_published_files_have_only_placeholder_credentials(self):
        # Catch plausible Filament, webhook and Resend credentials, including
        # accidentally pasted example keys. The shipped placeholders cannot match.
        patterns = (r'fmcp_[A-Za-z0-9_-]{20,}', r'whsec_[A-Za-z0-9+/]{32,}={0,2}',
                    r're_[A-Za-z0-9_]{20,}')
        for filename in ('relay.mjs', 'relay.tab-site.json'):
            source = (TAB / filename).read_text()
            for pattern in patterns:
                with self.subTest(filename=filename, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source))
        for placeholder in ('<random>', '<handle>', '<base64-32-bytes>', '<email-api-key>'):
            self.assertIn(placeholder, (TAB / 'relay.mjs').read_text())

    def test_manifest_publishes_worker_with_seen_store(self):
        manifest = json.loads((TAB / 'relay.tab-site.json').read_text())
        self.assertEqual(manifest['kind'], 'worker')
        self.assertTrue((TAB / manifest['entry']).is_file())
        self.assertIn({'type': 'kv', 'name': 'SEEN'}, manifest['bindings'])


if __name__ == '__main__':
    unittest.main()
