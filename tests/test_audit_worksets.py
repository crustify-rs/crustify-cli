from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from crustify_audit.agents.base import AuditAgent
from crustify_audit.layout import Layout


class AuditWorksetTests(unittest.TestCase):
    def agent(self, objective: str, workset: list[str] | None) -> AuditAgent:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return AuditAgent(
            Layout(Path(temporary.name)),
            objective=objective,
            workset=workset,
            instruments=["miri"],
        )

    def test_patch_workset_names_advisories(self) -> None:
        advisory = "crustify/audit/advisories/untethered-reference-owners"
        prompt = self.agent("patch", [advisory])._workset_text()

        self.assertIn("These advisories, and only these", prompt)
        self.assertIn(advisory, prompt)
        self.assertNotIn("These files", prompt)

    def test_patch_without_workset_repairs_every_advisory(self) -> None:
        prompt = self.agent("patch", None)._workset_text()

        self.assertIn("Every confirmed advisory", prompt)
        self.assertIn("do not hunt for new findings", prompt)

    def test_revisit_workset_still_names_leads(self) -> None:
        lead = "crustify/audit/leads/open-question.md"
        prompt = self.agent("revisit", [lead])._workset_text()

        self.assertIn("These leads, and only these", prompt)
        self.assertIn(lead, prompt)

    def test_audit_workset_still_names_source_files(self) -> None:
        source = "src/wrapper.rs"
        prompt = self.agent("audit", [source])._workset_text()

        self.assertIn("These files, and only these", prompt)
        self.assertIn(source, prompt)


if __name__ == "__main__":
    unittest.main()
