from __future__ import annotations

import unittest
from crustify.translate import lifetime_objective


class RawLifetimeReviewTests(unittest.TestCase):
    def test_raw_lifetime_wave_preserves_review_objective(self) -> None:
        self.assertEqual(lifetime_objective("review"), "review")
        self.assertEqual(lifetime_objective("wrap"), "wrap")
        self.assertEqual(lifetime_objective("port"), "wrap")

if __name__ == "__main__":
    unittest.main()
