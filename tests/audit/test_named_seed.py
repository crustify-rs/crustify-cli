import json
import tempfile
import unittest
from pathlib import Path

from crustify_audit.cli import build_parser
from crustify_audit.driver import _collect_emissions, measure
from crustify_audit.layout import Layout
from crustify_audit.unsafe_scan import _ensure_scan_ignored, summarize


class NamedSeedTests(unittest.TestCase):
    def test_scan_gitignore_is_narrow_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = Layout(Path(tmp))
            layout.root.mkdir(parents=True)
            ignore = layout.root / ".gitignore"
            ignore.write_text("/tmp/\n")

            _ensure_scan_ignored(layout)
            _ensure_scan_ignored(layout)

            body = ignore.read_text()
            self.assertTrue(body.startswith("/tmp/\n"))
            self.assertEqual(1, body.count("# crustify-audit artifacts"))
            for pattern in ("/unsafe.json", "/scratch/", "**/target/",
                            "**/target-*/"):
                self.assertIn(pattern, body)
            self.assertNotIn("\n/logs/\n", body)

    def test_campaign_ignore_avoids_a_per_agent_ignore_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = Layout(Path(tmp))
            layout.root.mkdir(parents=True)
            (layout.repo / "crustify" / ".gitignore").write_text(
                "audit/unsafe.json\n")

            _ensure_scan_ignored(layout)

            self.assertFalse((layout.root / ".gitignore").exists())

    def test_name_can_be_repeated_and_extended(self):
        args = build_parser().parse_args(
            ["/tmp/repo", "unsafe", "--name", "SSL", "SSL_new",
             "--name", "SSL_free"])
        self.assertEqual(args.name, ["SSL", "SSL_new", "SSL_free"])

    def test_seed_entries_keep_their_crate_and_sys_crates_are_ignored(self):
        stdout = "\n".join([
            json.dumps({"crate": "wrapper", "seeds": [
                {"name": "SSL", "raw_ptr_sites": [
                    {"file": "src/lib.rs", "count": 2, "lines": [4, 8]},
                ], "raw_deref_sites": [
                    {"file": "src/lib.rs", "count": 1, "lines": [9]},
                ], "deref_impl_sites": [], "deref_mut_impl_sites": [],
                   "slice_ref_sites": [], "slice_mut_sites": []},
            ]}),
            json.dumps({"crate": "wrapper", "unsafe_blocks": 3,
                        "raw_ptr_sites": [{"file": "src/lib.rs", "line": 4}]}),
            json.dumps({"crate": "helper", "unsafe_blocks": 5}),
            json.dumps({"crate": "openssl_sys", "seeds": [
                {"name": "SSL", "raw_ptr_sites": [], "raw_deref_sites": []},
            ]}),
            json.dumps({"crate": "openssl_sys", "unsafe_blocks": 99}),
        ])

        counts, entries, seen = _collect_emissions(stdout)

        self.assertEqual(seen, 2)
        self.assertEqual(counts["unsafe_blocks"], 8)
        self.assertEqual(
            counts["raw_ptr_sites"], [{"file": "src/lib.rs", "line": 4}])
        self.assertEqual(entries, [
            {"name": "SSL", "raw_ptr_sites": [
                {"file": "src/lib.rs", "count": 2, "lines": [4, 8]},
            ], "raw_deref_sites": [
                {"file": "src/lib.rs", "count": 1, "lines": [9]},
            ], "deref_impl_sites": [], "deref_mut_impl_sites": [],
               "slice_ref_sites": [], "slice_mut_sites": [],
               "crate": "wrapper"},
        ])

    def test_summary_names_seed_crate_and_surface(self):
        text = summarize({
            "counts": {"code_lines": 1, "unsafe_blocks": 0,
                       "unsafe_fns": 0, "raw_ptr_args": 0,
                       "raw_ptr_rets": 0, "wrapper_newtypes": 1,
                       "ffi_calls": 0},
            "derived": {},
            "entries": [{"crate": "wrapper", "name": "SSL",
                         "raw_ptr_sites": [{"count": 2}],
                         "raw_deref_sites": [{"count": 1}],
                         "deref_impl_sites": [{"count": 1}],
                         "deref_mut_impl_sites": [{"count": 1}],
                         "slice_ref_sites": [{"count": 1}],
                         "slice_mut_sites": [{"count": 1}]}],
        })
        self.assertIn(
            "wrapper::SSL  raw-pointer sites 2, dereference sites 1,"
            " Deref/DerefMut impl sites 1/1, shared/mutable slice sites 1/1",
            text)

    def test_driver_finds_field_argument_return_and_dereference_sites(self):
        fixture = Path(__file__).parent / "fixtures" / "raw_sites"
        _counts, entries = measure(fixture, names=["c_thing"])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["crate"], "fixture")
        self.assertEqual(entry["name"], "c_thing")
        self.assertEqual(
            sum(site["count"] for site in entry["raw_ptr_sites"]), 12)
        self.assertEqual(
            sum(site["count"] for site in entry["raw_deref_sites"]), 2)
        self.assertEqual(
            sum(site["count"] for site in entry["deref_impl_sites"]), 1)
        self.assertEqual(
            sum(site["count"] for site in entry["deref_mut_impl_sites"]), 1)
        self.assertEqual(
            sum(site["count"] for site in entry["slice_ref_sites"]), 3)
        self.assertEqual(
            sum(site["count"] for site in entry["slice_mut_sites"]), 3)

    def test_driver_seeds_called_and_exported_symbol_names(self):
        fixture = Path(__file__).parent / "fixtures" / "raw_sites"
        _counts, entries = measure(
            fixture, names=["c_touch", "c_ping", "exported_touch"])
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(
            set(by_name), {"c_touch", "c_ping", "exported_touch"})
        self.assertEqual(
            sum(site["count"] for site in by_name["c_touch"]["raw_ptr_sites"]),
            3)
        self.assertEqual(
            sum(site["count"] for site in by_name["c_touch"]["raw_deref_sites"]),
            0)
        self.assertEqual(
            sum(site["count"] for site in
                by_name["exported_touch"]["raw_ptr_sites"]),
            2)
        self.assertEqual(
            sum(site["count"] for site in
                by_name["exported_touch"]["raw_deref_sites"]),
            1)
        self.assertEqual(by_name["c_ping"]["raw_ptr_sites"], [])
        self.assertEqual(by_name["c_ping"]["raw_deref_sites"], [])

    def test_driver_seeds_a_symbol_called_inside_a_closure(self):
        fixture = Path(__file__).parent / "fixtures" / "raw_sites"
        _counts, entries = measure(fixture, names=["c_closure_touch"])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["name"], "c_closure_touch")
        self.assertGreaterEqual(
            sum(site["count"] for site in entry["raw_ptr_sites"]), 1)


if __name__ == "__main__":
    unittest.main()
