from __future__ import annotations

import re
import unittest
from pathlib import Path


class TaskManifestTests(unittest.TestCase):
    def test_benchmark_tasks_follow_campaign_intake_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = sorted((root / "bench").rglob("TASK.md"))
        self.assertTrue(tasks)

        headings = (
            "# Campaign", "# Sub-campaigns", "# Workload",
            "# Agentic review", "# Execution", "# UB audit",
            "# Benchmark metadata",
        )
        fields = ("repository", "revision", "objective")
        banned = ("wave-planning", "campaign-surface", "campaign.json")

        for task in tasks:
            with self.subTest(task=task.relative_to(root)):
                text = task.read_text()
                for heading in headings:
                    self.assertIn(heading, text)
                for field in fields:
                    self.assertIn(f"- {field}:", text)
                for stale in banned:
                    self.assertNotIn(stale, text)

                sub_campaigns = len(re.findall(r"^## `", text, re.MULTILINE))
                self.assertGreater(sub_campaigns, 0)
                self.assertEqual(text.count("- translator-backend:"),
                                 sub_campaigns)
                self.assertEqual(text.count("- translator-model:"),
                                 sub_campaigns)

                ub = text.split("# UB audit", 1)[1].split("\n# ", 1)[0]
                self.assertIn("- run:", ub)
                self.assertIn("- model:", ub)


if __name__ == "__main__":
    unittest.main()
