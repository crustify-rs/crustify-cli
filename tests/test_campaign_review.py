from __future__ import annotations

import unittest
from crustify.translate import lifetime_objective


class RawLifetimeReviewTests(unittest.TestCase):
    def test_raw_lifetime_campaign_preserves_review_objective(self) -> None:
        self.assertEqual(lifetime_objective("review"), "review")
        self.assertEqual(lifetime_objective("wrap"), "raw")
        self.assertEqual(lifetime_objective("port"), "raw")

if __name__ == "__main__":
    unittest.main()
