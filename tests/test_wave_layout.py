from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crustify.layout import Layout


class WaveLayoutTests(unittest.TestCase):
    def test_campaign_storage_is_repo_wide_not_target_derived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            layout = Layout(repo)
            self.assertEqual(layout.campaigns, repo / "crustify" / "campaigns")
            self.assertEqual(layout.logs,
                             repo / "crustify" / "campaigns" / "logs")


if __name__ == "__main__":
    unittest.main()
