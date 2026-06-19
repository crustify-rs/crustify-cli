"""Orchestration for the ``bindgen`` command.

Deterministic composer stage: scaffolds the ``<lib>-sys`` FFI crates for a
target from its scope.json + the annotated analysis tree, partitioned by
**owning crate** (``crates.json`` — crate name == link unit). Mirrors
``scaffold.py`` (no LLM, no ``cargo``).

The agent stage (macro shims, the ``cargo check`` verify loop) runs
separately and consumes the per-crate ``crustify-bindgen.json`` worklist
this composer emits.

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


def _topo_order(plan) -> list[str]:
    """Libraries in dependency order (a crate's ``foreign_libs`` deps come
    first), so each ``-sys`` builds before any crate that ``use``s it."""
    libs = set(plan.libs)
    order: list[str] = []
    seen: set[str] = set()

    def visit(lib: str, stack: frozenset[str]) -> None:
        if lib in seen or lib not in libs:
            return
        for dep in sorted(plan.libs[lib].foreign_libs):
            if dep in libs and dep not in stack:   # ignore cycles defensively
                visit(dep, stack | {lib})
        if lib not in seen:
            seen.add(lib)
            order.append(lib)

    for lib in sorted(libs):
        visit(lib, frozenset())
    return order


def bindgen(
    target: Path,
    *,
    libs: list[str] | None = None,
    scaffold_only: bool = False,
) -> None:
    """Scaffold the ``-sys`` crates for ``target``, scoped by its scope.json,
    then (unless ``scaffold_only``) run the per-crate agent stage.

    Args:
      target: the port target (crates go under ``target/rust/crates``).
      libs: optional library restriction (a crates.json crate/link unit,
        e.g. ``libssl``).
      scaffold_only: stop after the deterministic composer (no agent).
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
                f"`crustify {target} build` first."
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
              f"{len(lp.allow_vars)} vars, {len(lp.macro_worklist)} macro shims"
              + (f", deps={sorted(lp.foreign_libs)}" if lp.foreign_libs else ""))

    stats = write_plan(plan, rust_root)
    print(
        f"[crustify bindgen] composer: {stats.libs} -sys crate(s), "
        f"{stats.files_written} file(s) written, "
        f"{stats.skipped_existing} preserved → {rust_root}"
    )

    if scaffold_only:
        print("[crustify bindgen] --scaffold-only: stopping after composer.")
        return

    # Agent stage: macro/global shims + cargo-check verify loop, one agent
    # per crate, in dependency order (a dep -sys must build before a
    # dependent can resolve its `use <dep>_sys::*`).
    from crustify.agents.bindgen import CrustifyBindgenShimmer

    order = _topo_order(plan)
    print(f"[crustify bindgen] agent stage over {len(order)} crate(s) "
          f"in dependency order: {[l + '-sys' for l in order]}")

    failures: list[tuple[str, BaseException]] = []
    for lib in order:
        try:
            CrustifyBindgenShimmer(target, library=lib).run()
        except BaseException as exc:  # noqa: BLE001 — continue-then-report
            failures.append((lib, exc))
            print(f"[crustify bindgen] {lib}-sys: agent FAILED — "
                  f"{type(exc).__name__}: {str(exc)[:160]}")
            # A dependent can't build on a broken dep; stop the chain.
            break

    if failures:
        raise SystemExit(
            f"bindgen agent stage failed for "
            f"{', '.join(l + '-sys' for l, _ in failures)}. See "
            f".crustify/logs/<session>/bindgen__<lib>.log. Re-run "
            f"`crustify {target} bindgen` to resume (composer is idempotent; "
            f"agent skips already-wrapped symbols)."
        )
