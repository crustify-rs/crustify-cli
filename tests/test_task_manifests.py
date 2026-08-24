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
            "# Campaign questions", "# Campaign execution questions",
            "# Autonomy questions", "# Benchmark recording questions",
        )
        banned = ("wave-planning", "campaign-surface", "campaign.json")

        for task in tasks:
            with self.subTest(task=task.relative_to(root)):
                text = task.read_text()
                for heading in headings:
                    self.assertIn(heading, text)
                for stale in banned:
                    self.assertNotIn(stale, text)

                sub_campaigns = len(re.findall(r"^## `", text, re.MULTILINE))
                if sub_campaigns:
                    self.assertIn("# Sub-campaign questions", text)
                for number in (1, 2, 3, 8, 9, 10):
                    self.assertRegex(text, rf"(?m)^{number}\. \*\*")
                question_three = re.search(
                    r"(?ms)^3\. \*\*(.*?)\*\*\n\s+- Answer:", text
                )
                self.assertIsNotNone(question_three)
                scope_question = question_three.group(1).lower()
                self.assertIn("one or two subsystems", scope_question)
                self.assertIn("functions or types", scope_question)
                self.assertIn("whole target", scope_question)
                self.assertIn("brainstormed", scope_question)
                for number in (4, 5, 6, 7):
                    self.assertEqual(
                        len(re.findall(rf"(?m)^{number}\. \*\*", text)),
                        sub_campaigns,
                    )
                autonomy_questions = (
                    "Should I run fully autonomously end to end?",
                    "If no, should I wait for your approval before starting the setup phase?",
                    "Should I wait for your approval before starting the translation phase?",
                    "Should I wait for your approval in between sub-campaigns?",
                    "Should I wait for your approval before starting review passes?",
                    "Should I wait for your approval before starting UB audit passes?",
                )
                for number, question in enumerate(autonomy_questions, 1):
                    self.assertIn(f"A{number}. **{question}**", text)
                question_count = len(re.findall(
                    r"(?m)^(?:\d+|A\d+)\. \*\*", text
                ))
                self.assertEqual(text.count("- Answer:"), question_count)
                self.assertEqual(
                    text.count("Which backend and model should translate"),
                    sub_campaigns,
                )
                self.assertIn("agentic UB pass", text)
                self.assertIn("which model", text.lower())


if __name__ == "__main__":
    unittest.main()
