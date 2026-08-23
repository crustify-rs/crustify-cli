from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crustify.agents.translate import TranslateAgent
from crustify.layout import Layout


class PromptCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.ffibox = self.repo / "deps" / "ffibox"
        self.audit = self.repo / "deps" / "audit"
        self.oracle = self.repo / "deps" / "oracle"
        self.ffibox.mkdir(parents=True)
        self.audit.mkdir(parents=True)
        self.oracle.mkdir(parents=True)
        (self.ffibox / "README.md").write_text("# ffibox procedure\n")
        (self.audit / "README.md").write_text("# audit procedure\n")
        (self.ffibox / "SKILL.md").write_text(
            "# ffibox\n\n- Skill name: ffibox\n- Doc path: README.md\n"
            "- Description: Generic ownership wrappers.\n"
        )
        (self.audit / "SKILL.md").write_text(
            "# crustify-audit\n\n- Skill name: crustify-audit\n"
            "- Bin path: crustify-audit\n- Doc path: README.md\n"
            "- Description: Generic Rust safety review.\n"
        )
        (self.oracle / "SKILL.md").write_text(
            "# crustify-oracle\n\n- Skill name: crustify-oracle\n"
            "- Bin path: crustify-oracle\n- Doc path: README.md\n"
            "- Description: Generic semantic oracle.\n"
        )
        (self.oracle / "README.md").write_text("# oracle procedure\n")
        layout = Layout(self.repo)
        layout.root.mkdir(parents=True, exist_ok=True)
        self.layout = layout

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def configure(self, capabilities: list[str]) -> None:
        self.layout.repo_config.write_text(json.dumps({
            "deps": {
                "crustify": str(self.source),
                "crustify-oracle": str(self.oracle),
                "ffibox": str(self.ffibox),
                "crustify-audit": str(self.audit),
            },
            "bins": {"crustify-audit": "/tools/crustify-audit"},
            "prompt_capabilities": {"translator": capabilities},
        }))

    def type_translator(self) -> TranslateAgent:
        return TranslateAgent(
            self.repo,
            batch_kind="type",
            tags=["fixture_st"],
            kinds=["struct"],
            entry_files=["fixture.h"],
            repo_root=self.repo,
        )

    def test_configured_capabilities_add_generic_skills_and_role_headers(self) -> None:
        self.configure(["crustify-oracle", "ffibox", "crustify-audit"])
        agent = self.type_translator()
        system = agent.system_preamble()
        self.assertEqual(
            agent.prompt_capabilities(),
            ("crustify-oracle", "ffibox", "crustify-audit"),
        )
        self.assertIn("crustify-translator", system)
        self.assertIn("crustify-oracle", system)
        self.assertIn("Generic ownership wrappers", system)
        self.assertIn("never form a Rust reference", system)
        self.assertIn("Generic Rust safety review", system)
        self.assertIn("never invoke `crustify-audit ub`", system)
        self.assertIn("binary: /tools/crustify-audit", system)
        self.assertIn("Additional role guidance:", system)
        self.assertNotIn("<!-- SKILL -->", system)

    def test_absent_capabilities_leave_only_the_core_translator_skill(self) -> None:
        self.configure([])
        agent = self.type_translator()
        system = agent.system_preamble()
        self.assertEqual(agent.prompt_capabilities(), ())
        self.assertIn("# Crustify coding conventions", system)
        self.assertIn("crustify-translator", system)
        self.assertNotIn("Generic semantic oracle", system)
        self.assertNotIn("campaign worklist as fixed", system)
        self.assertNotIn("ffibox", system)
        self.assertNotIn("crustify-audit", system)

    def test_absent_role_configuration_enables_no_optional_capabilities(self) -> None:
        self.layout.repo_config.write_text(json.dumps({
            "deps": {"crustify": str(self.source)},
        }))
        agent = self.type_translator()
        self.assertEqual(agent.prompt_capabilities(), ())

    def test_unified_prompt_and_worklist_render_without_missing_arguments(self) -> None:
        self.configure([])
        agent = self.type_translator()
        rendered = agent._prompt().format(**agent._arguments())
        self.assertIn('"route": "type"', rendered)
        self.assertIn('"name": "fixture_st"', rendered)
        self.assertIn("task objective: `wrap`", rendered)
        self.assertIn("campaign objective: `wrap`", rendered)

    def test_symbol_and_raw_lifetime_routes_use_the_same_prompt(self) -> None:
        self.configure([])
        symbol = TranslateAgent(
            self.repo, batch_kind="syms",
            syms=[{"name": "fixture_free", "defined_in": "fixture.c"}],
            repo_root=self.repo,
        )
        raw = TranslateAgent(
            self.repo, batch_kind="syms", lifetime_for="void", objective="wrap",
            repo_root=self.repo,
        )
        self.assertEqual(symbol._prompt(), raw._prompt())
        self.assertEqual(json.loads(symbol._arguments()["worklist"])["route"], "symbol")
        self.assertEqual(json.loads(raw._arguments()["worklist"])["route"], "raw-lifetime")

    def test_unknown_capability_is_rejected(self) -> None:
        self.configure(["not-a-capability"])
        with self.assertRaisesRegex(SystemExit, "unknown translator prompt capability"):
            self.type_translator()


if __name__ == "__main__":
    unittest.main()
