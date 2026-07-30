"""Orchestration for the ``wrap`` command — wrap-stage glue over the shared
``--name`` scheduler.

Wrap-stage codegen is driven by :mod:`crustify._schedule`: selection is
``--name`` (repeatable); a named **type** brings its in-scope ops
(budget-split), named **free symbols** pool per file. The user supplies the
dependency order (the DAG is what they read to choose it); a confirmation
prompt lists first-layer deps.

This module owns only the wrap-specific pieces — the scope-blind selection
predicate (:func:`_selection_pred`), the bindgen gate, and the emit seam to :class:`crustify.agents.wrap.CrustifyWrap` — plus
the stub-index / preflight / budget helpers the scheduler and the port stage
reuse. Scope (wrap/port) is applied **here**, never in the DAG, via the same
``compose.scope`` classifier the manifests use.

The scheduler schedules blindly — it emits every selected unit (budget-bounded),
checking only that each home ``.rs`` exists; it does not skip already-filled
anchors. The scaffolder still lays ``// Replaces:`` / ``// crustify:todo`` markers
for the agent to fill, but per-item idempotency is the agent's concern.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The composer package lives at ``utils/codeql/compose/`` in the crustify
# checkout, not as an installed package. Mirror scaffold.py / analyze.py.
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


# ---------------------------------------------------------------------------
# Pre-flight gate
# ---------------------------------------------------------------------------

def _preflight(target: Path, layout: "Layout") -> Path:
    """Verify upstream artifacts; return the scope.json path. Refuses with
    the exact command to run when a prerequisite is missing. All paths come
    from the single visible ``crustify/`` layout."""
    analysis = layout.analysis
    if not analysis.is_dir() or not any(analysis.rglob("types.json")):
        raise SystemExit(
            f"wrap: no analysis tree at {analysis}. Run "
            f"`crustify {target} analyze --all` first."
        )
    if not layout.deps_dag(target).exists():
        raise SystemExit(
            f"wrap: no deps-dag.json at {layout.deps_dag(target)}. Run "
            f"`crustify {target} analyze dag` first."
        )
    scope_json = layout.scope(target)
    if not scope_json.exists():
        raise SystemExit(
            f"wrap: no scope.json at {scope_json}. Run "
            f"`crustify {target} analyze scope` first."
        )
    # Scaffold the source-file stub tree up-front (idempotent — writes only
    # absent files; module blocks merge). The wrap agents then locate their
    # modules + deps via `scaffold --name` (query mode) and fill them; they
    # never create files themselves.
    from crustify.scaffold import scaffold
    scaffold(target, all=True, create=True)
    return scope_json


def _check_bindgen(layout: "Layout", target: Path, linked: set[str]) -> None:
    """Ensure each library a job binds has a scaffolded ``<lib>-sys`` crate.

    The Rust workspace is repo-root-shared (``crustify/rust``), so the
    ``-sys`` crates live there as ``rust/<lib>-sys``, not under the target."""
    missing = sorted(
        l for l in linked
        if l and not (layout.rust / f"{l}-sys" / "Cargo.toml").exists()
    )
    if missing:
        raise SystemExit(
            f"wrap: bindgen -sys crate missing for {missing!r}. Run "
            f"`crustify {target} bindgen --libs {' '.join(missing)}` first."
        )


# ---------------------------------------------------------------------------
# Manifest indexers + budget (shared with the port stage)
# ---------------------------------------------------------------------------

def _index_entry_files(analysis_root: Path) -> dict[str, str]:
    """Map each type tag to the ``types.json`` that carries its entry, so the
    agent reads the right manifest directly."""
    index: dict[str, str] = {}
    for f in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for entry in doc.get("types", []):
            tag = entry.get("name") or entry.get("type")
            if tag and not str(tag).startswith("_"):
                index.setdefault(tag, str(f))
    return index


def _index_sym_files(analysis_root: Path) -> dict[str, str]:
    """Map each symbol name to the ``syms.json`` carrying it (for the agent)."""
    index: dict[str, str] = {}
    for f in sorted(analysis_root.rglob("syms.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get("symbols", []):
            nm = e.get("name")
            if nm:
                index.setdefault(nm, str(f))
    return index


def _wrap_eligible_pred(scope_json):
    """Predicate: is this node something `wrap` may take? Wrap takes **wrap-scope**
    entities (types + symbols) and **any in-scope type** (port- *or* wrap-scope
    — every type is wrapped, never ported). A port-scope *symbol* belongs to the
    port stage, and an out-of-scope entity isn't wrappable — both are rejected by
    the gate in :func:`wrap_types`."""
    from compose import scope
    is_wrap = scope.in_scope_pred(scope_json, "wrap")
    is_port = scope.in_scope_pred(scope_json, "port")

    def pred(n) -> bool:
        if is_wrap(n):
            return True
        return n.node_kind == "type" and is_port(n)
    return pred


def _selection_pred(scope_json, *, wrap_only: bool,
                    port_only: bool, files: set[str]):
    """Selection predicate over a :class:`_schedule.Node`. The base is
    :func:`_wrap_eligible_pred` (wrap-scope ∪ in-scope types);
    `--wrap-only`/`--port-only` further *narrow* by scope.json membership, and
    `--file` restricts to a defining file (disambiguating a `--name` collision)."""
    from compose import scope
    eligible = _wrap_eligible_pred(scope_json)
    narrow = (scope.in_scope_pred(scope_json, "wrap") if wrap_only
              else scope.in_scope_pred(scope_json, "port") if port_only
              else None)

    def pred(n) -> bool:  # n: _schedule.Node
        if files and (n.defined_in or "") not in files:
            return False
        if narrow and not narrow(n):
            return False
        return eligible(n)
    return pred



def _wrap_emit(
    target: Path, layout, *, max_fields: int, max_syms: int,
):
    """Production emit: one :class:`CrustifyWrap` per scheduled batch. A type
    batch carries the type + this batch's op slice; a syms batch carries the
    pooled free symbols. The agent resolves each module's path itself via
    `scaffold --name` (query mode); nothing is pre-resolved here."""
    from crustify.agents.wrap import CrustifyWrap

    def emit(batch) -> None:  # batch: _schedule.Batch
        type_units = [u for u in batch.units if u.kind == "type"]
        if type_units and batch.field_range is not None:
            # type-pull (single struct/union/enum): tag + field-accessor window;
            # the agent pulls lifecycle + cast graph from the record itself. The
            # shared scheduler also tiles by op count (for the port stage), so a
            # tail batch may carry an empty field window and no type def — the wrap
            # agent has nothing to emit there (a type's methods are wrapped as
            # symbols), so skip it.
            u = type_units[0]
            f_lo, f_hi = batch.field_range
            introduces_type = any(m.id == u.node.id for m in batch.members)
            if not introduces_type and f_hi <= f_lo:
                return
            CrustifyWrap(
                target, batch_kind="type",
                tags=[u.node.id], kinds=[u.node.subkind],
                fields_range=list(batch.field_range),
                repo_root=layout.repo_root,
            ).run()
            return
        if type_units:
            # type-pull: hand the family tags + kind; the agent pulls each
            # tag's record/ops/deps/.rs.
            CrustifyWrap(
                target, batch_kind="type",
                tags=[u.node.id for u in type_units],
                kinds=[type_units[0].node.subkind],
                repo_root=layout.repo_root,
            ).run()
            return
        # syms-pull: hand the batch's pooled symbol names; the agent pulls each
        # one's record/deps/.rs via `crustify query sym`/`query dag`.
        syms = [{"name": m.id, "defined_in": m.defined_in} for m in batch.members]
        CrustifyWrap(target, batch_kind="syms", syms=syms,
                     repo_root=layout.repo_root).run()

    return emit


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def wrap_types(
    target: Path,
    *,
    names: list[str] | None = None,
    files: list[str] | None = None,
    wrap_only: bool = False,
    port_only: bool = False,
    dag_layer: int | None = None,
    skip: list[str] | None = None,
    parallel: bool = False,
    parallel_max: int = 8,
    max_fields: int | None = None,
    max_syms: int | None = None,
    yes: bool = False,
    dry_run: bool = False,
    emit_fn=None,
) -> None:
    """Wrap selected wrap-scope units via the shared ``--name`` scheduler.

    Selection is ``--name`` (repeatable). A named **type** brings its
    in-scope ops (budget-split); named free symbols pool per file. The
    scheduler runs in dependency-layer order and prints the first-layer deps
    as a heads-up (no prompt).
    """
    from compose import scope
    from crustify import config as _cfg
    from crustify import _schedule as S
    from crustify.layout import Layout

    if max_fields is None:
        max_fields = _cfg.WRAP_MAX_FIELDS
    if max_syms is None:
        max_syms = _cfg.WRAP_MAX_SYMS

    layout = Layout.discover(target)
    scope_json = _preflight(target, layout)
    dag = json.loads(layout.deps_dag(target).read_text())
    print(f"[crustify wrap] deps DAG: {dag.get('stats')}")

    by_key, by_name = S.load_nodes(dag)

    base_in_scope = _selection_pred(
        scope_json, wrap_only=wrap_only, port_only=port_only,
        files=set(files or []))

    # Op-facading is wrap-scope ONLY — a type's port-scope ops are the port
    # stage's (it translates their bodies), so wrap must not facade them.
    _wrap_op = scope.in_scope_pred(scope_json, "wrap")

    sel_names = list(names or [])
    if dag_layer is not None:
        # e2e driver mode: EVERY in-scope unit at dag layer N — types (any
        # in-scope) and wrap-scope free syms, EXCLUDING lifecycle ops that fold
        # into a type (they ride with `wrap types` at the type's lower layer)
        # and macros. So `wrap syms` never re-wraps an already-folded op.
        _elig = _wrap_eligible_pred(scope_json)
        _folded = scope.type_method_fns(layout.analysis)
        sel_names += sorted({
            n.id for n in by_key.values()
            if n.layer == dag_layer and _elig(n)
            and not (n.node_kind == "symbol"
                     and ((n.subkind or "").startswith("macro") or n.id in _folded))})
    if skip:
        _sk = set(skip)
        sel_names = [s for s in sel_names if s not in _sk]
    if not sel_names:
        raise SystemExit(
            "wrap: nothing selected — pass --name / --dag-layer N "
            "(a --skip blocklist may have emptied the selection).")

    # Macros are header-defined: bindgen owns their <lib>-sys shims, so the wrap
    # stage never facades them. (The PORT stage still translates port-scope TU
    # macros — this skip is wrap-only, hence here and not in the shared
    # scheduler.) Exclude macro_* symbols from selection, and drop any --name
    # that resolves ONLY to macros so it neither schedules a wrap job nor
    # reports as "unknown".
    def _is_macro(n) -> bool:
        return n.node_kind == "symbol" and (n.subkind or "").startswith("macro")
    in_scope = lambda n: base_in_scope(n) and not _is_macro(n)
    skipped, kept = [], []
    for nm in sel_names:
        hits = [by_key[k] for k in (by_name.get(nm) or []) if base_in_scope(by_key[k])]
        (skipped if (hits and all(_is_macro(n) for n in hits)) else kept).append(nm)
    if skipped:
        print(f"[crustify wrap] skipping {len(skipped)} macro(s) — bindgen owns "
              f"their -sys shims: {', '.join(sorted(skipped))}")
    sel_names = kept
    if not sel_names:
        raise SystemExit(
            "wrap: nothing to wrap — selection resolved only to macros "
            "(bindgen owns their -sys shims).")

    # Scope gate (parallels the port stage). Reject a named entity wrap cannot
    # take, with guidance: a port-scope SYMBOL belongs to `port` (wrap would
    # silently emit a stray FFI shim instead of the safe view); an out-of-scope
    # name isn't wrappable. Types (port- or wrap-scope) are eligible — they
    # pass `in_scope` above. Resolve scope-blind so we can
    # *see* the rejected ones instead of dropping them as "unknown".
    _is_port = scope.in_scope_pred(scope_json, "port")
    loose = {n.id: n for nm in sel_names
             for n in (by_key[k] for k in (by_name.get(nm) or []))
             if not _is_macro(n)}
    bad_port = sorted(i for i, n in loose.items()
                      if not in_scope(n) and n.node_kind == "symbol" and _is_port(n))
    bad_oos = sorted(i for i, n in loose.items()
                     if not in_scope(n) and not (n.node_kind == "symbol" and _is_port(n)))
    if bad_port:
        listing = "\n".join(f"  - {i}" for i in bad_port)
        raise SystemExit(
            f"wrap: {len(bad_port)} selected "
            f"{'entity is a' if len(bad_port)==1 else 'entities are'} port-scope "
            f"symbol — these belong to the PORT stage (wrap would emit a stray "
            f"FFI shim, not the safe view):\n{listing}\n"
            f"  Run `crustify {target} port --name …` instead.")
    if bad_oos:
        listing = "\n".join(f"  - {i}" for i in bad_oos)
        raise SystemExit(
            f"wrap: {len(bad_oos)} selected "
            f"{'entity is' if len(bad_oos)==1 else 'entities are'} out of scope "
            f"(neither wrap- nor port-scope):\n{listing}")

    # Bindgen gate for the libraries actually being wrapped. A selected unit's
    # owning library is its crate in crates.json (crate name == link unit);
    # a unit not placed there contributes nothing (skipped).
    sel_nodes, _ = S.resolve_names(sel_names, by_key, by_name, in_scope)
    from crustify import crates as _crates
    _doc = _crates.load(layout)

    def _lib_of(n) -> str | None:
        hit = _crates.lookup(_doc, n.id, file=n.defined_in)
        return hit["crate"] if hit else None

    _check_bindgen(layout, target,
                   {lib for n in sel_nodes if (lib := _lib_of(n))})

    from crustify.agents.merge import CrustifyMerge
    # The merge agent runs the OFF/ON C build+test matrix for wrap waves too: the
    # flag-ON build links already-ported C against the per-library port crates
    # (each library staticlib carries its own `mod ffi_export` re-exports),
    # which depend on this wave's wrappers, so a wrapper change can break the
    # cumulative ported build. It reads the SAME cumulative manifest the port
    # stage writes (absent until the first port wave, in which case ON == OFF).
    feature_file = layout.port_features          # rust/port-features.json (git-tracked)
    # Worktree isolation auto-engages with --parallel (production emit only; a
    # caller-supplied emit_fn, e.g. a test double, opts out).
    emit_factory = None if emit_fn else (
        lambda t, l: _wrap_emit(t, l, max_fields=max_fields, max_syms=max_syms))
    stage = S.Stage(
        verb="wrap", in_scope=in_scope, op_in_scope=_wrap_op,
        emit_fn=emit_fn or _wrap_emit(target, layout,
                                      max_fields=max_fields, max_syms=max_syms),
        max_syms=max_syms, max_fields=max_fields,
        emit_factory=emit_factory, target=target, layout=layout,
        merge_factory=(lambda base, results, crates: CrustifyMerge(
            target, base_commit=base, results=results, crates=crates, stage="wrap",
            feature_file=str(feature_file))),
    )
    failures = S.schedule(
        dag=dag, analysis_root=layout.analysis,
        names=sel_names, stage=stage, parallelize=parallel,
        parallel_max=parallel_max, yes=yes, dry_run=dry_run,
    )
    if failures:
        labels = ", ".join(b.label() for b, _ in failures)
        raise SystemExit(
            f"wrap stage failed for {len(failures)} batch(es): {labels}. "
            f"Per-agent logs live under crustify/targets/<rel>/logs/<session>/.")
    if not dry_run:
        print("[crustify wrap] done.")
