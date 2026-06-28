"""Orchestration for the ``port`` command — port-stage glue over the shared
``--name`` scheduler.

The port stage rewrites port-scope C **functions and globals** into safe Rust
behind a compile-time feature switch, driven by :mod:`crustify._schedule`,
calling the already-wrapped safe type API for any types they touch. Types
(layout / lifecycle / accessors — wrap's) and macros (``ffi::`` bindings /
``crustify_<NAME>`` shims — bindgen's) are **not** ported: named symbols pool
per file. The user supplies the dependency order.

Port-specific glue (vs the wrap stage):

  - **workspace_root = repo_root.** The agent owns the per-file
    ``CRUSTIFY_<FILE>`` build wiring, and the ``#[no_mangle]`` re-exports live
    in each ported file's ``mod ffi_export`` submodule (compiled into the
    owning library's port crate, not a central ``ffi-exports`` crate),
    so it runs at the repo root.
  - **Feature manifest + synthetic gate.** Before scheduling, the orchestrator
    emits ``port-features.json`` (every ported file's ``CRUSTIFY_<FILE>``
    macro) and refuses to run unless the synthetic wrap pre-pass
    (strings/arrays clusters) is on disk — those primitives are consumed
    edge-invisibly, so they must exist first.

Scope (port/wrap) is applied **here**, never in the DAG, via the same
``compose.scope`` classifier the manifests use.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The composer package lives at ``utils/codeql/compose/``; mirror wrap.py.
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


# ---------------------------------------------------------------------------
# Path-sanitised tokens (shared rule for the guard macro + re-export symbol)
# ---------------------------------------------------------------------------

def file_token(defined_in: str) -> str:
    """``lib/vssh/libssh2.c`` -> ``LIB_VSSH_LIBSSH2`` (extension dropped,
    non-alnum -> ``_``, upper-cased). The guard macro is ``CRUSTIFY_<token>``;
    the re-export symbol prefix is ``crustify_<token.lower()>__``."""
    stem = re.sub(r"\.[^./]+$", "", defined_in or "")
    return re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper()


def guard_macro(defined_in: str) -> str:
    return "CRUSTIFY_" + file_token(defined_in)


# ---------------------------------------------------------------------------
# Pre-flight gate
# ---------------------------------------------------------------------------

def _preflight(target: Path, layout: "Layout") -> Path:
    analysis = layout.analysis
    if not analysis.is_dir() or not any(analysis.rglob("types.json")):
        raise SystemExit(
            f"port: no analysis tree at {analysis}. Run "
            f"`crustify {target} analyze --all` first.")
    if not (analysis / "deps-dag.json").exists():
        raise SystemExit(
            f"port: no deps-dag.json at {analysis}. Run "
            f"`crustify {target} analyze dag` first.")
    scope_json = layout.scope(target)
    if not scope_json.exists():
        raise SystemExit(
            f"port: no scope.json at {scope_json}. Run "
            f"`crustify {target} analyze scope` first.")
    # Scaffold the source-file stub tree up-front (idempotent — writes only
    # absent files; module blocks merge). The port agents then locate their
    # targets + deps via `scaffold --name` (query mode) and fill them; they
    # never create files themselves.
    from crustify.scaffold import scaffold
    scaffold(target, all=True, create=True)
    return scope_json


def _type_method_fns(analysis_root: Path) -> set[str]:
    """Every C function that is a wrapped type's **lifecycle** op (ctor / dtor /
    up_ref / clone / lock) — i.e. its ``scope.type_method_syms``. The WRAP stage
    emits these as the type's `&self` methods, so PORT must never (re-)port them.
    Field accessors are NOT here — the wrapper derives per-field accessors from
    the field layout, so a C field-accessor function stays port material like any
    other *behaviour* function."""
    from compose import scope
    return scope.type_method_fns(analysis_root)


# ---------------------------------------------------------------------------
# Public entry point (deterministic half + dry-run)
# ---------------------------------------------------------------------------

def _is_port_scope_pred(scope_json):
    """Port-scope predicate over a :class:`_schedule.Node` — scope.json
    membership (the authoritative port closure), not a re-derived classify."""
    from compose import scope
    return scope.in_scope_pred(scope_json, "port")


def _emit_features_for(files: set[str], out: Path) -> dict[str, str]:
    """Write ``port-features.json`` — the **CUMULATIVE** per-file feature-flag
    manifest. Unions ``files`` with whatever a prior wave already recorded
    (never drops a previously-ported file), because the Rust port crate is a
    single ``--whole-archive`` staticlib: the C link pulls in EVERY ported
    file's exports at once, so the flag set must cover the whole cumulative
    ported surface, not just this wave's slice (else a prior wave's still-guarded
    C body collides with its Rust export → duplicate-symbol link failure)."""
    cfiles = {f for f in files if f}
    if out.exists():
        try:
            for feat in json.loads(out.read_text()).get("features", []):
                if feat.get("c_file"):
                    cfiles.add(feat["c_file"])
        except (OSError, ValueError):
            pass
    mapping = {f: guard_macro(f) for f in sorted(cfiles)}
    out.write_text(json.dumps(
        {"_comment": "Per-file compile-time feature flags for the port stage; "
                     "define a file's macro to switch its ported bodies to Rust. "
                     "CUMULATIVE across waves (unions each wave's ported files).",
         "features": [{"c_file": f, "macro": m, "symbol_prefix": "crustify_"
                       + file_token(f).lower() + "__"}
                      for f, m in mapping.items()]},
        indent=2) + "\n")
    return mapping


def _port_emit(target: Path, layout, *, opacify: list[str], feature_file: Path):
    """One :class:`CrustifyPort` per scheduled batch — always a free-symbol pool
    (functions + globals; types and macros are never scheduled for port). The
    agent runs at ``workspace_root = repo_root`` (the port divergence) and pulls
    each symbol's record / deps / module via ``crustify query`` / ``scaffold``."""
    from crustify.agents.port import CrustifyPort

    def emit(batch) -> None:  # batch: _schedule.Batch
        syms = [{"name": m.id, "defined_in": m.defined_in} for m in batch.members]
        # Append THIS batch's files to the (cumulative) manifest BEFORE the agent
        # runs, so it reads a flag set coherent with its own tree. `feature_file`
        # is bound to the active `layout` — the WORKTREE's in an isolated wave
        # (→ baseline ∪ this chain's files; the merge unions them) or the main
        # tree's in a serial run (→ accumulates the whole wave). Replaces the old
        # wave-global pre-fan-out write, which was incoherent per-worktree.
        _emit_features_for({m.defined_in for m in batch.members if m.defined_in},
                           feature_file)
        # In an isolated wave `layout` is rooted at the worktree → the agent runs
        # against the worktree, not the pinned main repo.
        CrustifyPort(target, symbols=syms, feature_file=str(feature_file),
                     repo_root=layout.repo_root).run()

    return emit


def port(
    target: Path,
    *,
    names: list[str] | None = None,
    files: list[str] | None = None,
    dag_layer: int | None = None,
    skip: list[str] | None = None,
    parallel: bool = False,
    parallel_max: int = 8,
    max_syms: int | None = None,
    max_loc: int | None = None,
    yes: bool = False,
    dry_run: bool = False,
    emit_fn=None,
) -> None:
    """Port selected port-scope **functions and globals** via the shared
    ``--name`` scheduler. Types and macros are rejected/skipped (see below);
    named free symbols pool per file. Runs at ``workspace_root = repo_root`` —
    the port divergence."""
    from compose import scope
    from crustify import config as _cfg
    from crustify import _schedule as S
    from crustify.layout import Layout

    max_syms = _cfg.PORT_MAX_SYMS if max_syms is None else max_syms
    max_loc = _cfg.PORT_MAX_LOC if max_loc is None else max_loc

    layout = Layout.discover(target)
    scope_json = _preflight(target, layout)
    dag = json.loads((layout.analysis / "deps-dag.json").read_text())
    print(f"[crustify port] deps DAG: {dag.get('stats')}")

    by_key, by_name = S.load_nodes(dag)

    # Scope from scope.json membership (authoritative port/wrap closures).
    _is_port = scope.in_scope_pred(scope_json, "port")
    _is_wrap = scope.in_scope_pred(scope_json, "wrap")

    sel_names = list(names or [])
    if dag_layer is not None:
        # e2e driver mode: every port-scope SYMBOL at dag layer N, EXCLUDING
        # lifecycle ops that fold into a type (wrap emits those) and macros — so
        # the layer auto-selection never trips the lifecycle/type/macro gates.
        _folded = scope.type_method_fns(layout.analysis)
        sel_names += sorted({
            n.id for n in by_key.values()
            if n.layer == dag_layer and n.node_kind == "symbol" and _is_port(n)
            and not str(n.subkind).startswith("macro") and n.id not in _folded})
    if skip:
        _sk = set(skip)
        sel_names = [s for s in sel_names if s not in _sk]
    if not sel_names:
        raise SystemExit("port: nothing selected — pass --name and/or "
                         "--dag-layer N (a --skip blocklist may have emptied it).")

    def _scope_of(n) -> str:
        return "port" if _is_port(n) else "wrap" if _is_wrap(n) else "wrap"

    file_set = set(files or [])

    def _file_match(n) -> bool:
        return not file_set or (n.defined_in or "") in file_set

    # Scope gate: porting nativizes an entity, so it must be port-scope. A named
    # wrap-scope entity is a hard error — it was classified to stay C (external
    # lib, or a header/value type) and should be wrapped, not ported. (Resolve
    # all-scope so we can *see* the wrap-scope ones instead of silently dropping
    # them as the port-scope predicate would.)
    named, _unknown = S.resolve_names(
        sel_names, by_key, by_name, lambda n: _file_match(n))

    # A wrapped type's lifecycle ops (ctors/dtor/up_ref/clones/locking) are
    # normally wrap's to emit. But wrap has no constructor mechanism and can't
    # express ops whose C signature doesn't fit a `&self` method, so the
    # **port-scope** ones sit as `// crustify:todo` stubs (the blind spot). When
    # such an op is **explicitly named**, port it as a standalone free function —
    # bypassing the wrap-method guard for that selected subset (the dag-layer/file
    # sweeps still exclude them; see below). This is gated on `_is_port`: a
    # WRAP-scope lifecycle op (e.g. the `git_vector_dup` clone) was classified to
    # stay C and is still blocked by the wrap-scope gate below.
    method_fns = _type_method_fns(layout.analysis)
    port_lifecycle = {n.id for n in named
                      if n.node_kind == "symbol" and n.id in method_fns
                      and _is_port(n)}

    wrap_scoped = sorted(
        (n for n in named if _scope_of(n) == "wrap"), key=lambda x: x.id)
    if wrap_scoped:
        listing = "\n".join(
            f"  - {n.id}  ({n.defined_in or '?'})"
            for n in wrap_scoped)
        raise SystemExit(
            f"port: {len(wrap_scoped)} selected entit{'y' if len(wrap_scoped)==1 else 'ies'} "
            f"wrap-scope — porting would nativize something classified to stay C:\n"
            f"{listing}\n"
            f"  → an external entity belongs in its <lib>-sys crate; "
            f"an in-tree header/value type should be wrapped, not ported.\n"
            f"  Run `crustify {target} wrap --name …` instead.")

    # Port operates on FUNCTIONS and GLOBALS only. A named port-scope TYPE is a
    # hard error: wrap owns a type's layout, lifecycle and accessors, and a
    # type's "methods" are ordinary port-scope functions — name those. A named
    # port-scope MACRO is skipped with guidance: bindgen owns its `ffi::` binding
    # / `crustify_<NAME>` shim and the C `#define` stays (macro call-sites are
    # flattened to their underlying symbol in the dag, so a real dep shows up as
    # that symbol, not the macro).
    port_types = sorted(
        (n for n in named if _scope_of(n) == "port"
         and n.node_kind == "type" and n.subkind != "callback"),
        key=lambda x: x.id)
    if port_types:
        listing = "\n".join(f"  - {n.id}  ({n.defined_in or '?'})" for n in port_types)
        raise SystemExit(
            f"port: {len(port_types)} selected entit"
            f"{'y is a type' if len(port_types)==1 else 'ies are types'} — port "
            f"operates on functions/globals only (wrap owns a type's layout, "
            f"lifecycle & accessors):\n{listing}\n"
            f"  → name the type's free *behaviour* functions (not its accessors/"
            f"lifecycle, which wrap emits), or select by --file.")
    port_macros = sorted(
        {n.id for n in named if _scope_of(n) == "port"
         and str(n.subkind).startswith("macro")})
    if port_macros:
        print(f"[crustify port] skipping {len(port_macros)} macro(s) — bindgen owns "
              f"their ffi:: bindings / crustify_<NAME> shims; the C #define stays: "
              f"{', '.join(port_macros)}", file=sys.stderr)

    # Explicitly-named lifecycle ops (computed above as `port_lifecycle`) are
    # admitted as port material — a deliberate override of the wrap-method guard.
    # Unnamed ones never reach here: the dag-layer auto-selection excludes
    # `_folded` (line ~207) and `--file` requires a `--name`, so a sweep can't
    # silently pull a lifecycle op in. Field accessors were never in this set —
    # they port like any free function.
    if port_lifecycle:
        listing = ", ".join(sorted(port_lifecycle))
        print(f"[crustify port] porting {len(port_lifecycle)} lifecycle op(s) as "
              f"free function(s) (explicitly named, bypassing the wrap-method "
              f"guard): {listing}", file=sys.stderr)

    # TODO(opacity): once non_opaque_in is wired, also refuse a port whose target
    # type still has C field-touchers (dual-ownership risk) unless --force.

    # Selection is FUNCTIONS + GLOBALS: file-matched, symbol-kind (excludes
    # types — node_kind == "type"), non-macro, and EITHER an ordinary port-scope
    # free function (not a wrapped type's lifecycle method — those are wrap's, see
    # `method_fns`) OR an explicitly-named PORT-scope lifecycle op being ported on
    # purpose (`port_lifecycle`, e.g. `git_mwindow_open`, `git_odb_new`).
    in_scope = lambda n: (
        _file_match(n)
        and n.node_kind == "symbol"
        and not str(n.subkind).startswith("macro")
        and ((_is_port(n) and n.id not in method_fns)   # ordinary port free fn
             or n.id in port_lifecycle))                # explicitly-named lifecycle op
    sel_nodes, _ = S.resolve_names(sel_names, by_key, by_name, in_scope)
    files: set[str] = {n.defined_in for n in sel_nodes if n.defined_in}

    feature_file = layout.port_features          # rust/port-features.json (git-tracked)
    emit = emit_fn
    if not dry_run:
        # No wave-global pre-fan-out write: _port_emit appends each batch's files
        # to its layout's port_features (per-worktree in an isolated wave, main in
        # serial) — so a worktree's flag set is baseline ∪ its-own-files, coherent
        # with its own staticlib. The merge unions the per-worktree manifests.
        emit = emit or _port_emit(target, layout, opacify=[],
                                  feature_file=feature_file)

    from crustify.agents.merge import CrustifyMerge
    # Worktree isolation auto-engages with --parallel (production emit only). Bind
    # feature_file to the WORKTREE's layout (l.port_features) so each worktree
    # appends to its own (git-inherited baseline) copy; the merge unions them.
    emit_factory = None if emit_fn else (
        lambda t, l: _port_emit(t, l, opacify=[], feature_file=l.port_features))
    stage = S.Stage(
        verb="port", in_scope=in_scope, emit_fn=emit or (lambda b: None),
        # No op-facading: port schedules only free symbols (functions/globals),
        # so there are no type ops/fields to window (max_fields stays unbounded).
        # The per-batch budget is count (max_syms) ∧ lines-of-code (max_loc).
        max_syms=max_syms, max_loc=max_loc,
        emit_factory=emit_factory, target=target, layout=layout,
        merge_factory=(lambda base, results, crates: CrustifyMerge(
            target, base_commit=base, results=results, crates=crates,
            stage="port", feature_file=str(feature_file))))
    failures = S.schedule(
        dag=dag, analysis_root=layout.analysis,
        names=sel_names, stage=stage, parallelize=parallel,
        parallel_max=parallel_max, yes=yes, dry_run=dry_run)
    if failures:
        detail = "; ".join(
            f"{b.label()} ({type(e).__name__}: {e})" for b, e in failures
        )
        raise SystemExit(f"port stage failed for {len(failures)} batch(es): {detail}.")
    if not dry_run:
        print("[crustify port] done.")
