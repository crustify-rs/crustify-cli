from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from crustify import campaign
from crustify.translate import lifetime_objective


class CampaignReviewPlacementTests(unittest.TestCase):
    def test_raw_lifetime_campaign_preserves_review_objective(self) -> None:
        self.assertEqual(lifetime_objective("review"), "review")
        self.assertEqual(lifetime_objective("wrap"), "raw")
        self.assertEqual(lifetime_objective("port"), "raw")

    def test_review_retains_an_unplaced_item_as_context(self) -> None:
        missing = {("negative_finding", "src/example.c")}
        output = io.StringIO()

        with redirect_stdout(output):
            campaign._check_missing_homes("review", missing)

        self.assertIn("review-only worklist context", output.getvalue())
        self.assertIn("negative_finding", output.getvalue())

    def test_translation_rejects_an_unplaced_item(self) -> None:
        missing = {("needs_a_home", "src/example.c")}

        with self.assertRaisesRegex(SystemExit, "Author the missing module"):
            campaign._check_missing_homes("wrap", missing)


if __name__ == "__main__":
    unittest.main()
