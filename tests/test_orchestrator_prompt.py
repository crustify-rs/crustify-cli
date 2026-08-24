from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class OrchestratorPromptTests(unittest.TestCase):
    def test_builder_uses_standalone_oracle_and_audit_skills(self) -> None:
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oracle = root / "oracle"
            audit = root / "audit"
            for checkout, name, description in (
                (oracle, "crustify-oracle", "Standalone semantic planning."),
                (audit, "crustify-audit", "Standalone Rust safety review."),
            ):
                checkout.mkdir()
                (checkout / "README.md").write_text(f"# {name}\n")
                (checkout / "SKILL.md").write_text(
                    f"# {name}\n\n"
                    f"- Skill name: {name}\n"
                    "- Doc path: README.md\n"
                    f"- Description: {description}\n"
                )

            result = subprocess.run(
                [sys.executable, "-m", "crustify.orchestrator_prompt",
                 oracle, audit],
                check=True,
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONPATH": str(source / "src")},
            )
            task = root / "TASK.md"
            task.write_text(
                "# Campaign questions\n\n"
                "1. **Which repository?**\n"
                "   - Answer: https://example.test/project.git\n"
            )
            with_task = subprocess.run(
                [sys.executable, "-m", "crustify.orchestrator_prompt",
                 oracle, audit, task],
                check=True,
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONPATH": str(source / "src")},
            )

        prompt = result.stdout
        self.assertIn("crustify-orchestrator", prompt)
        self.assertIn("crustify-oracle — Standalone semantic planning.", prompt)
        self.assertIn("crustify-audit — Standalone Rust safety review.", prompt)
        self.assertIn(str(source / "docs" / "conventions.md"), prompt)
        self.assertIn("one or two subsystems", prompt)
        self.assertIn("named subset of functions or types", prompt)
        self.assertIn("whole target", prompt)
        self.assertIn("brainstorm them together", prompt)
        self.assertIn("which model should perform each review?", prompt)
        self.assertIn("### Autonomy", prompt)
        self.assertIn("Should I run fully autonomously end to end?", prompt)
        self.assertIn("approval before starting the setup phase?", prompt)
        self.assertIn("approval before starting the translation phase?", prompt)
        self.assertIn("approval in between sub-campaigns?", prompt)
        self.assertIn("approval before starting review passes?", prompt)
        self.assertIn("approval before starting UB audit passes?", prompt)
        self.assertIn("Waves and steps are internal", prompt)
        self.assertNotIn("ffibox —", prompt)
        self.assertNotIn("- oracle —", prompt)
        self.assertNotIn("<!-- CONVENTIONS_PATH -->", prompt)
        self.assertNotIn("<!-- SKILLS -->", prompt)

        task_prompt = with_task.stdout
        self.assertIn("## Pre-filled campaign task", task_prompt)
        self.assertIn("Treat completed answers as campaign input", task_prompt)
        self.assertIn("https://example.test/project.git", task_prompt)


if __name__ == "__main__":
    unittest.main()
