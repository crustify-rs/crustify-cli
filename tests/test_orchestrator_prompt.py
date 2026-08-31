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
        playbook = (source / "docs" / "orchestrator-playbook.md").read_text()
        self.assertIn("crustify-orchestrator", prompt)
        self.assertIn("campaign-wide", prompt)
        self.assertIn("emitting `subsystems.json`", prompt)
        self.assertIn("crustify-oracle — Standalone semantic planning.", prompt)
        self.assertIn("crustify-audit — Standalone Rust safety review.", prompt)
        self.assertIn(str(source / "docs" / "conventions.md"), prompt)
        self.assertIn("named subset of", prompt)
        self.assertIn("subsystems", prompt)
        self.assertIn("named subset of functions and types", prompt)
        self.assertIn("whole target repo", prompt)
        self.assertIn("brainstorm them during the live session", prompt)
        self.assertIn("orchestrator's choice", prompt)
        self.assertIn("which backend and model should perform each review?", prompt)
        self.assertIn("Which billing mode should agentic stages use", prompt)
        self.assertRegex(
            prompt,
            r"Where and in what\s+format should results be recorded\?",
        )
        self.assertIn("What batch caps should review agents use?", prompt)
        self.assertIn("3x the translation caps", prompt)
        self.assertIn("### Autonomy", prompt)
        self.assertIn("Should I run fully autonomously end to end?", prompt)
        self.assertIn("approval before starting the setup phase?", prompt)
        self.assertIn("approval before starting the translation phase?", prompt)
        self.assertIn("approval between sub-campaigns?", prompt)
        self.assertIn("approval before starting review passes?", prompt)
        self.assertIn("approval before starting UB audit passes?", prompt)
        self.assertIn("Waves and steps are internal", prompt)
        self.assertNotIn("## Planning and pass alignment", prompt)
        self.assertNotIn("## Planning and pass alignment", playbook)
        self.assertIn("## Phase 2 — Translation", playbook)
        self.assertIn("### Plan sub-campaigns", playbook)
        self.assertIn("one sub-campaign per", playbook)
        self.assertIn("bottom-up", playbook)
        self.assertIn("### Raw lifetime discovery sub-campaigns", playbook)
        self.assertIn("first two sub-campaigns", playbook)
        self.assertIn("scope-config.json", playbook)
        self.assertIn("### Review objective", playbook)
        self.assertRegex(playbook, r"after\s+each completed sub-campaign")
        self.assertIn("### UB patch promotion", playbook)
        self.assertIn("once at the end of the whole campaign", playbook)
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
