from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crustify.layout import Layout


class WaveLayoutTests(unittest.TestCase):
    def test_campaign_storage_is_target_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / "ssl" / "statem"
            layout = Layout(repo)
            self.assertEqual(layout.campaigns, repo / "crustify" / "campaigns")
            self.assertEqual(
                layout.campaign_dir(target),
                repo / "crustify" / "campaigns" / "ssl" / "statem",
            )
            self.assertEqual(
                layout.logs(target),
                repo / "crustify" / "campaigns" / "ssl" / "statem" / "logs",
            )

    def test_repo_root_target_uses_campaigns_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            layout = Layout(repo)
            self.assertEqual(layout.campaign_dir(repo), layout.campaigns)
            self.assertEqual(layout.logs(repo), layout.campaigns / "logs")


if __name__ == "__main__":
    unittest.main()
