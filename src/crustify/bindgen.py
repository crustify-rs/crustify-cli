"""Orchestration for the ``bindgen`` command.

Deterministic composer stage, and the whole stage: scaffolds the ``<lib>-sys``
FFI crates for a target from its scope.json + the annotated analysis tree,
partitioned by **owning crate** (``crates.json`` — crate name == link unit).
Mirrors ``scaffold.py`` (no LLM, no ``cargo``).

The crates come out INCOMPLETE by design — build.rs carries the per-kind
allowlists but no ``fn main``, and bindgen.h's ``crustify:shims`` block is empty.
Finishing them needs a compiler in the loop, which this stage does not have;
see ``compose/bindgen_manifest.py``.

Stage gate: ``analyze`` + ``scaffold`` must have run (annotated tree +
``crates.json`` placing the wrap-scope entities) and the CodeQL T1/T2 CSVs
must exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


def bindgen(target: Path, *, libs: list[str] | None = None,
            reset: bool = False) -> None:
    """Scaffold the ``-sys`` crates for ``target``, scoped by its scope.json.

    Args:
      target: the port target (crates go under ``target/rust/crates``).
      libs: optional library restriction (a crates.json crate/link unit,
        e.g. ``libssl``).
      reset: recompute the composer-owned state (build.rs allowlists,
        bindgen.h's include block) instead of accumulating onto it. Leaves
        everything the composer does not own alone.
    """
    from compose.bindgen_manifest import compose, write_plan
    from compose.filter_spec import FilterSpec
    from crustify.layout import Layout

    layout = Layout.discover(target)
    repo_root = layout.repo_root

    scope_json = layout.scope(target)
    if not scope_json.exists():
        raise SystemExit(
            f"error: scope.json not found at {scope_json}. Run "
            f"`crustify {target} analyze scope` first."
        )
    analysis_root = layout.analysis
    if not analysis_root.exists():
        raise SystemExit(
            f"error: analysis tree not found at {analysis_root}. Run "
            f"`crustify {target} analyze` first."
        )
    t1, t2 = layout.t1, layout.t2
    for csv_dir in (t1, t2):
        if not csv_dir.exists():
            raise SystemExit(
                f"error: CodeQL CSVs not found at {csv_dir}. Run "
                f"`crustify {target} analyze extract-ql` first."
            )

    spec = FilterSpec(scope_json_path=scope_json)
    # Shared, cross-target output tree at crustify/rust/ (-sys crates live at
    # crustify/rust/<lib>-sys/ and grow as targets reach more of each library).
    rust_root = layout.rust

    plan = compose(t1, t2, analysis_root, spec, lib_filter=libs,
                   repo_root=repo_root)
    if not plan.libs:
        raise SystemExit(
            "error: no in-scope FFI libraries for this target"
            + (f" matching --libs {libs}" if libs else "")
            + ". Check crates.json places the wrap-scope entities "
            + f"(scaffold must run first) for {analysis_root}."
        )

    print(f"[crustify bindgen] -sys crates: "
          f"{sorted(l + '-sys' for l in plan.libs)}")
    for lib, lp in sorted(plan.libs.items()):
        print(f"[crustify bindgen]   {lib}-sys: "
              f"{len(lp.allow_types)} types, {len(lp.allow_funcs)} funcs, "
              f"{len(lp.allow_macros)} macros, {len(lp.allow_vars)} vars, "
              f"{len(lp.allow_callbacks)} callbacks"
              + (f", deps={sorted(lp.foreign_libs)}" if lp.foreign_libs else ""))

    stats = write_plan(plan, rust_root, reset=reset)
    print(
        f"[crustify bindgen] {stats.libs} -sys crate(s), "
        f"{stats.files_written} file(s) written, "
        f"{stats.skipped_existing} preserved → {rust_root}"
    )
    print("[crustify bindgen] crates are incomplete scaffolds: build.rs has "
          "the allowlists but no fn main; shims go in bindgen.h's "
          "crustify:shims block.")
