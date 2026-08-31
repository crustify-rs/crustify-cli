from __future__ import annotations

import re
import unittest
from pathlib import Path


class TaskManifestTests(unittest.TestCase):
    def test_example_tasks_follow_campaign_intake_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tasks = sorted((root / "examples").rglob("TASK.md"))
        self.assertTrue(tasks)

        banned = ("wave-planning", "campaign-surface", "campaign.json")

        for task in tasks:
            with self.subTest(task=task.relative_to(root)):
                text = task.read_text()
                for stale in banned:
                    self.assertNotIn(stale, text)

                is_canonical = "# Mandatory questions" in text
                if is_canonical:
                    headings = (
                        "# Mandatory questions",
                        "## Campaign",
                        "# Optional questions",
                        "## Campaign execution",
                        "## Autonomy (if question 7 is answered `no`)",
                        "# Benchmark recording questions",
                    )
                else:
                    headings = (
                        "# Campaign questions",
                        "# Campaign execution questions",
                        "# Autonomy questions",
                        "# Benchmark recording questions",
                    )
                for heading in headings:
                    self.assertIn(heading, text)

                sub_campaigns = len(re.findall(r"^## `", text, re.MULTILINE))
                if sub_campaigns:
                    self.assertIn("# Sub-campaign questions", text)
                if is_canonical:
                    for number in range(1, 17):
                        self.assertEqual(
                            len(re.findall(rf"(?m)^{number}\. \*\*", text)),
                            1,
                        )
                else:
                    for number in (1, 2, 3, 8, 9, 10):
                        self.assertRegex(text, rf"(?m)^{number}\. \*\*")
                question_three = re.search(
                    r"(?ms)^3\. \*\*(.*?)\*\*\n\s+- Answer:", text
                )
                self.assertIsNotNone(question_three)
                scope_question = question_three.group(1).lower()
                self.assertIn("subsystems", scope_question)
                self.assertIn("whole target", scope_question)
                self.assertIn("brainstorm", scope_question)
                if is_canonical:
                    self.assertIn("functions and types", scope_question)
                    self.assertIn("orchestrator's choice", scope_question)
                else:
                    self.assertIn("one or two", scope_question)
                    self.assertIn("functions or types", scope_question)
                if not is_canonical:
                    for number in (4, 5, 6, 7):
                        self.assertEqual(
                            len(re.findall(rf"(?m)^{number}\. \*\*", text)),
                            sub_campaigns,
                        )
                autonomy_questions = (
                    "Should I run fully autonomously end to end?",
                    "Should I wait for your approval before starting the setup phase?",
                    "Should I wait for your approval before starting the translation phase?",
                    "Should I wait for your approval between sub-campaigns?",
                    "Should I wait for your approval before starting review passes?",
                    "Should I wait for your approval before starting UB audit passes?",
                )
                for question in autonomy_questions:
                    if question not in text and "between sub-campaigns" in question:
                        question = question.replace("between", "in between")
                    if question not in text and "starting the setup" in question:
                        question = f"If no, {question[0].lower()}{question[1:]}"
                    self.assertIn(question, text)
                question_count = len(
                    re.findall(r"(?m)^(?:\d+|A\d+)\. \*\*", text)
                )
                self.assertEqual(text.count("- Answer:"), question_count)
                if is_canonical:
                    self.assertEqual(
                        text.count(
                            "Which agentic backend and model should do the "
                            "translation work?"
                        ),
                        1,
                    )
                else:
                    self.assertEqual(
                        text.count("Which backend and model should translate"),
                        sub_campaigns,
                    )
                self.assertNotIn(
                    "Which backend and model run the orchestrator?", text
                )
                self.assertEqual(
                    text.count("What batch caps should review agents use?"), 1
                )
                self.assertIn("recommend 3x", text.lower())
                self.assertIn("agentic UB", text)
                self.assertIn("backend and model", text.lower())
                self.assertNotIn("orchestrator" + "-selected", text)


if __name__ == "__main__":
    unittest.main()
