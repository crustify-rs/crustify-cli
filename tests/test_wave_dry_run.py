from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from crustify.wave import _dry_run, load


class WaveDryRunParityTests(unittest.TestCase):
    def test_render_is_byte_equivalent_after_policy_line_retirement(self) -> None:
        fixtures = Path(__file__).parent / "fixtures"
        dag = json.loads((fixtures / "scheduler-dag.json").read_text())
        items = []
        for layer, records in enumerate(dag["layers"]):
            for record in records:
                kind = ("type" if record["node_kind"] == "type" else
                        "callback" if record.get("subkind") == "callback" else
                        "symbol")
                items.append({
                    "name": record["id"], "defined_in": record.get("defined_in"),
                    "kind": kind, "source_kind": record.get("subkind"),
                    "layer": layer, "loc": record.get("loc", 0),
                    "deps": {
                        "types": record.get("deps", {}).get("types", []),
                        "symbols": record.get("deps", {}).get("syms", []),
                    },
                    "fallback": [], "back_fill": [], "generates": [],
                    "field_anchors": [],
                })
        by_name = {item["name"]: item for item in items}
        wave = {
            "schema_version": 2,
            "oracle_target": ".",
            "summary": {"unit_count": 5, "layer_count": 2,
                        "batch_count": 4, "file_count": 2},
            "plan_items": items,
            "dependency_nodes": [],
            "steps": [
                {"layers": [0], "unit_count": 2, "batches": [
                    {"kind": "type", "source_file": "include/alpha.h",
                     "items": [by_name["alpha_st"]]},
                    {"kind": "symbol", "source_file": None,
                     "items": [by_name["alpha_new"]]},
                ]},
                {"layers": [1], "unit_count": 3, "batches": [
                    {"kind": "symbol", "source_file": None,
                     "items": [by_name["alpha_use"]]},
                    {"kind": "symbol", "source_file": None,
                     "items": [by_name["alpha_free"], by_name["alpha_cb"]]},
                ]},
            ],
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _dry_run(wave, "wrap")
        self.assertEqual(output.getvalue(),
                         (fixtures / "scheduler-dry-run.txt").read_text())

    def test_v1_campaign_document_normalizes_to_steps(self) -> None:
        old = {
            "schema_version": 1,
            "oracle_target": ".",
            "summary": {"unit_count": 0, "layer_count": 1,
                        "batch_count": 0},
            "plan_items": [],
            "dependency_nodes": [],
            "waves": [{"layers": [0], "unit_count": 0, "batches": []}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-campaign.json"
            path.write_text(json.dumps(old))
            normalized = load(path)
        self.assertEqual(normalized["steps"], old["waves"])


if __name__ == "__main__":
    unittest.main()
