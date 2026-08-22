from __future__ import annotations

import unittest

from crustify.crates import validate


class CratesValidateTests(unittest.TestCase):
    def test_dependency_cycle_is_reported_from_manifest_only(self) -> None:
        doc = {"crates": {
            "a": {"crate_path": "crustify/rust/a", "depends_on": ["b"],
                  "modules": {}},
            "b": {"crate_path": "crustify/rust/b", "depends_on": ["a"],
                  "modules": {}},
        }}
        errors = validate(doc)
        self.assertTrue(any("dependency cycle" in error for error in errors), errors)

    def test_unknown_dependency_is_reported(self) -> None:
        doc = {"crates": {
            "a": {"crate_path": "crustify/rust/a", "depends_on": ["missing"],
                  "modules": {}},
        }}
        errors = validate(doc)
        self.assertTrue(any("undefined crate" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
