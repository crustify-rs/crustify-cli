"""Orchestration for the ``analyze`` command family.

**Every analyze subject is composer-only — the stage spawns no agent.**

  0. ``crustify-cli analyze extract-ql``
     Runs the `.ql` batches against the hand-created CodeQL database at
     `<repo_root>/crustify/codeql/db/`; writes the T1/T2 CSVs under
     `<repo_root>/crustify/codeql/{t1,t2}/`. Every stage below reads them.

  1. ``crustify-cli analyze scope``
     Reads `crustify/targets/<target>/scope-config.json`; writes that target's
     `scope.json` (port set + wrap import-closure).

  2. ``crustify-cli analyze symbols`` / ``types``
     `compose.syms_manifest` / `compose.types_manifest` emit the per-stem
     skeletons.

  3. ``crustify-cli analyze dag``
     Unified types+symbols dependency DAG from the analysis tree.

Stage 1 depends on 0; stages 2 and 3 depend on 1. Composers are pure
functions of T1 + T2 + scope.json — fast, deterministic, run unconditionally.

The schemas' judgement fields (pointer facets, ownership, lifetime, locking)
carry no value out of this stage. They are submitted by the WRAPPER agents
through `query symbols/types --update` at the point the entity is wrapped;
the merge primitive unions at field level, so re-composing never clobbers
them. The retired analyzer agents' prompts are in `prompts/obsolete/`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from crustify.layout import Layout, manifest_name

# The composer package lives at `utils/codeql/compose/` in the crustify
# checkout, not as an installed Python package. Add the parent dir to
# sys.path so `from compose.X import Y` works from this orchestrator.
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))

# ---------------------------------------------------------------- composer wrappers

def analyze_extract_ql(target: Path) -> None:
    """Stage 0: run every `.ql` under `utils/codeql/entities/` and
    `utils/codeql/edges/` against the CodeQL database and write one CSV
    per query under `<repo_root>/crustify/codeql/{t1,t2}/`.

    Deterministic (no agent). The CodeQL database itself is **not**
    produced here — configuring the project, building it under
    `codeql database create --language=cpp --command=...`, and depositing
    the result at `<repo_root>/crustify/codeql/db/` is the orchestrator's
    job, done by hand. This stage only turns that database into the T1
    (entities) / T2 (edges) tables every other analyze subject reads.
    """
    import shutil

    from compose.extract_csvs import extract_t1_t2

    if shutil.which("codeql") is None:
        print(
            "error: the `codeql` CLI is not on PATH. Install it and run "
            "`codeql pack install` in utils/codeql/ so codeql/cpp-all "
            "resolves.",
            file=sys.stderr,
        )
        sys.exit(1)

    layout = Layout.discover(target)
    db = layout.codeql_db
    if not db.is_dir():
        print(
            f"error: CodeQL database not found at {db}.\n"
            f"       Build the project under CodeQL trace first, e.g.\n"
            f"         codeql database create {db} --language=cpp "
            f"--command=\"<build command>\"",
            file=sys.stderr,
        )
        sys.exit(1)

    succeeded, failed = extract_t1_t2(db, _CRUSTIFY_ROOT, layout.codeql)
    print(
        f"[crustify-cli analyze extract-ql] {succeeded} queries ok, "
        f"{failed} failed"
    )
    if failed:
        print(
            f"error: {failed} query extraction(s) failed; see output above. "
            f"Analyze stages will see empty / missing CSVs for those "
            f"queries.",
            file=sys.stderr,
        )
        sys.exit(1)


def _scope(target: Path) -> Path:
    """Run the scope composer; return the path to scope.json.

    v2 anchors the manifest on the CodeQL T1 tables, so this stage
    depends on `analyze extract-ql` having produced `crustify/codeql/t1/`.
    Gated accordingly.
    """
    from compose.scope_manifest import compose as scope_compose
    import json

    layout = Layout.discover(target)
    t1 = layout.t1
    if not (t1 / "functions.csv").is_file():
        print(
            f"error: scope v2 requires CodeQL T1 CSVs at {t1}.\n"
            f"       Run `crustify-cli <target> analyze extract-ql` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = layout.config(target)
    manifest = scope_compose(config_path, t1, layout.repo_root)
    out = layout.scope(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    p = manifest["port"]
    print(
        f"[crustify-cli analyze scope] {len(p['files'])} files / "
        f"{len(p['functions'])} fn / {len(p['globals'])} gv / "
        f"{len(p['macros'])} macro / {len(p['types'])} types → {out}"
    )
    return out


# ---------------------------------------------------------------- subject runner

def _run_subject_manifests_list(
    target: Path,
    repo_root: Path,
    t1: Path,
    t2: Path,
    *,
    subject: str,
    filter_spec,
) -> None:
    """Unified subject runner — composer only, no agent.

    Runs the composer once and writes its output through the merge
    primitive. Used for both ``symbols`` and ``types``; only the composer
    module, manifest filename, entries key and merge key differ.

    The schema's judgement fields (pointer facets, ownership, lifetime,
    locking) are NOT filled here. They are submitted by the wrapper agents
    through ``query symbols/types --update`` when the entity is wrapped, so
    the analysis is made where its consumer needs it rather than in a
    separate up-front pass. The merge primitive unions at field level, so
    re-running this composer never clobbers what a wrapper submitted.
    """
    if subject == "symbols":
        from compose.syms_manifest import compose as compose_fn
        from compose.syms_manifest import _COMMENT as COMMENT
        from compose.manifest_merge import merge_manifest_file, symbol_key as key_fn
        manifest_filename = manifest_name("symbols")
        entries_key = "symbols"
    elif subject == "types":
        from compose.types_manifest import compose as compose_fn
        from compose.types_manifest import _COMMENT as COMMENT
        from compose.manifest_merge import merge_manifest_file, type_key as key_fn
        manifest_filename = manifest_name("types")
        entries_key = "types"
    else:
        raise ValueError(f"unknown subject: {subject!r}")

    out_root = Layout(repo_root).analysis

    # `focus_by_key` bounded the retired analyzer agent's per-entry workset;
    # nothing consumes it now (the manifest carries the full, scope-agnostic
    # layout either way).
    entries_by_dir, _dir_scope, _focus_by_key = compose_fn(t1, t2, filter_spec)

    # Merge-primitive semantics: prior wrapper-submitted annotations survive.
    for rel_dir, entries in sorted(entries_by_dir.items()):
        manifest = {"_comment": COMMENT, entries_key: entries}
        merge_manifest_file(
            out_root / rel_dir / manifest_filename,
            manifest,
            entries_key=entries_key,
            key=key_fn,
        )
    print(
        f"[crustify-cli analyze {subject}] composer: "
        f"{len(entries_by_dir)} dirs → {out_root}"
    )
    if not entries_by_dir:
        print(f"[crustify-cli analyze {subject}] no entries; nothing to do.")


# ---------------------------------------------------------------- public verbs

def analyze_scope(
    target: Path, *, port_only: bool = False, wrap_only: bool = False,
) -> None:
    """Emit scope.json's two sections.

      - ``port`` — the translation set, seeded from ``scope-config.json``.
      - ``wrap`` — the FFI import-closure *derived from* ``port``: walk port
        entities' forward edges into the items they use, narrowed to the
        header(s) the importing TU ``#include``s.

    Default (neither flag) runs both in order, which is the normal case: the
    two were once separate stages because the wrap closure read the syms/types
    manifests and so had to run after ``analyze symbols``/``types``. It is now
    computed standalone from T1/T2 plus ``port``, so nothing needs to happen
    between them.

    The flags narrow to one section. ``--port-only`` leaves any existing
    ``wrap`` **stale**, since wrap is a function of port; ``--wrap-only``
    re-derives wrap against the port section already on disk.
    """
    if wrap_only:
        _wrap_scope(target)
        return
    _scope(target)
    if not port_only:
        _wrap_scope(target)


def analyze_symbols(target: Path, *, filter_spec=None) -> None:
    """Stage 2: compose the syms skeletons. Deterministic, no agent.

    `filter_spec` narrows the composer's emission (which manifest dirs and
    which entries within them survive the seed/closure / scope / name
    filters). The schema's judgement fields are submitted later by the
    wrapper agents via `query symbols --update` — see
    :func:`_run_subject_manifests_list`.
    """
    repo_root = _repo_root_for(target)
    _run_subject_manifests_list(
        target, repo_root, Layout(repo_root).t1, Layout(repo_root).t2,
        subject="symbols",
        filter_spec=filter_spec,
    )


def analyze_types(target: Path, *, filter_spec=None) -> None:
    """Stage 3: compose the types skeletons. Deterministic, no agent.

    Same contract as :func:`analyze_symbols`. Enums, callbacks and structs
    are all emitted; per-field ownership, lifecycle and locking are the
    type wrapper's submissions, not this stage's.
    """
    repo_root = _repo_root_for(target)
    _run_subject_manifests_list(
        target, repo_root, Layout(repo_root).t1, Layout(repo_root).t2,
        subject="types",
        filter_spec=filter_spec,
    )


def analyze_dag(target: Path) -> None:
    """Stage 4: emit this target's layered types+symbols dependency DAG.

    Reads the shared, scope-agnostic analysis tree and narrows each node's
    edges against the target's ``scope.json``: a port-scope node contributes
    every edge, a wrap-scope node only its signature (see
    ``compose.deps_dag._collect``). The graph is therefore per TARGET and is
    written beside scope.json at ``targets/<target>/deps-dag.json``, not into
    the shared tree.

    Requires a populated analysis tree (``analyze symbols`` + ``types``) and
    ``analyze scope``.
    """
    from compose.deps_dag import compose as deps_dag_compose
    import json

    layout = Layout.discover(target)
    analysis = layout.analysis
    if not analysis.exists() or not any(analysis.rglob("types.json")):
        print(
            f"error: analyze dag requires a populated analysis tree at "
            f"{analysis}.\n"
            f"       Run `crustify-cli <target> analyze types` (and `symbols`) "
            f"first.",
            file=sys.stderr,
        )
        sys.exit(1)

    scope_path = layout.scope(target)
    if not scope_path.is_file():
        print(
            f"error: analyze dag needs {scope_path} to narrow wrap-scope "
            f"edges.\n"
            f"       Run `crustify-cli <target> analyze scope` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    dag = deps_dag_compose(analysis, scope_path)
    out = layout.deps_dag(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dag, indent=2) + "\n")
    s = dag.get("stats", {})
    print(
        f"[crustify-cli analyze dag] {s.get('nodes')} nodes "
        f"({s.get('types')} types / {s.get('symbols')} syms / "
        f"{s.get('external_syms')} ext) / {s.get('edges')} edges / "
        f"{s.get('layers')} layers / {s.get('sccs_flattened')} cycle(s) "
        f"flattened ({s.get('fallback_edges')} fallback edges) → {out}"
    )


def _wrap_scope(target: Path) -> None:
    """Append the derived ``wrap`` import surface to scope.json — the
    ``analyze scope --wrap-only`` body.

    This is the per-target wrap closure, computed **standalone from CodeQL
    T1/T2 + the ``port`` section** (T2-standalone since c513c67 — it does NOT
    read the syms/types manifests, so it needs neither ``analyze symbols`` nor
    ``analyze types``): walk port entities' forward edges into the wrap items
    they use and narrow each item's declaration superset to the header(s) the
    importing port TU actually ``#include``s (build-resolved). Writes the
    ``wrap`` key alongside ``port`` in scope.json — derived, regenerable when
    ``port`` changes. Requires only ``analyze scope --port-only`` (the ``port``
    section) and ``analyze extract-ql`` (the T1 ``includes.csv`` + T1/T2
    tables).
    """
    import json
    from compose import scope as scope_mod
    from compose.wrap_closure import compose_wrap

    layout = Layout.discover(target)
    scope_path = layout.scope(target)
    if not scope_path.exists():
        print(
            f"error: scope.json missing at {scope_path}. Run "
            f"`crustify-cli {target} analyze scope --port-only` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    includes_csv = layout.t1 / "includes.csv"
    if not includes_csv.is_file():
        print(
            f"error: includes.csv missing at {includes_csv}. Run "
            f"`crustify-cli {target} analyze extract-ql` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    port_paths = scope_mod.load_port_paths(scope_path)
    inc = scope_mod.load_csv(includes_csv)
    # Type-side closure is CodeQL-derived (not types.json): T1 `types` for
    # per-type metadata (classify/home, incl. external leaves like
    # pthread_mutex_t) + T2 `field_type_uses` for the struct field→type graph.
    type_rows = scope_mod.load_csv(layout.t1 / "types.csv")
    field_type_rows = scope_mod.load_csv(layout.t2 / "field_type_uses.csv")
    wrap = compose_wrap(layout.t1, layout.t2, scope_path, inc, port_paths,
                        type_rows, field_type_rows)

    manifest = json.loads(scope_path.read_text())
    manifest["wrap"] = wrap
    scope_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"[crustify-cli analyze scope --wrap-only] {len(wrap['files'])} files / "
        f"{len(wrap['functions'])} fn / {len(wrap['globals'])} gv / "
        f"{len(wrap['macros'])} macro / {len(wrap['types'])} types → {scope_path}"
    )


# ---------------------------------------------------------------- reset helpers

def reset_extract_ql(target: Path) -> None:
    """Delete the T1/T2 CSV trees so the next ``analyze extract-ql``
    re-runs every query fresh. The CodeQL database is left alone — it is
    hand-created and expensive to rebuild."""
    import shutil

    layout = Layout.discover(target)
    for d in (layout.t1, layout.t2):
        if d.is_dir():
            shutil.rmtree(d)
            print(f"[crustify --reset] removed {d}")


def reset_scope(target: Path) -> None:
    """Delete scope.json so the next ``analyze scope`` re-emits fresh."""
    p = Layout.discover(target).scope(target)
    if p.exists():
        p.unlink()
        print(f"[crustify --reset] removed {p}")


def reset_dag(target: Path) -> None:
    """Delete deps-dag.json so the next ``analyze dag`` re-emits fresh."""
    p = Layout.discover(target).deps_dag(target)
    if p.exists():
        p.unlink()
        print(f"[crustify --reset] removed {p}")


def reset_syms(
    target: Path, *,
    all_entries: bool = False,
    dirs: Iterable[str] | None = None,
    files: Iterable[str] | None = None,
    names: Iterable[str] | None = None,
) -> None:
    """Delete syms.json entries matching the narrowing flags (or all of them
    when `all_entries` — the `--all --reset` reset).

    - `dirs`: repo-rel source-tree dirs; deletes every entry whose
      `defined_in` (or, when null, `declared_in[0]`) is under one of
      the dirs.
    - `files`: repo-rel file paths; deletes every entry whose
      `defined_in` (or `declared_in` for declaration-only entries)
      matches one of the files exactly.
    - `names`: symbol names; deletes every entry with one of the
      named symbols.

    The narrowing flags are unioned (an entry matching ANY of dir /
    file / name is deleted).
    """
    _delete_entries(
        target, manifest_name("symbols"),
        entries_key="symbols",
        name_key="name",
        all_entries=all_entries,
        dirs=dirs, files=files, names=names,
    )


def reset_types(
    target: Path, *,
    all_entries: bool = False,
    dirs: Iterable[str] | None = None,
    files: Iterable[str] | None = None,
    names: Iterable[str] | None = None,
) -> None:
    """Delete types.json entries matching the narrowing flags (or all of them
    when `all_entries` — the `--all --reset` reset)."""
    _delete_entries(
        target, manifest_name("types"),
        entries_key="types",
        name_key="name",
        all_entries=all_entries,
        dirs=dirs, files=files, names=names,
    )


# ---------------------------------------------------------------- internals

def _repo_root_for(target: Path) -> Path:
    """Repo root = nearest ancestor containing ``crustify/`` (the marker)."""
    from crustify.layout import Layout
    return Layout.discover(target).repo_root


def _normalize_dir(d: str) -> str:
    """Ensure a directory string has a trailing slash for prefix
    matching."""
    return d if d.endswith("/") else d + "/"


def _entry_in_dirs(entry: dict, dirs_norm: list[str]) -> bool:
    """True iff the entry's `defined_in` (or, when missing, the first
    `declared_in`) starts with one of the normalized dir prefixes."""
    paths: list[str] = []
    df = entry.get("defined_in")
    if df:
        paths.append(df)
    decls = entry.get("declared_in")
    if isinstance(decls, list) and decls:
        paths.append(decls[0])
    elif isinstance(decls, str) and decls:
        paths.append(decls)
    return any(
        any(p == pref.rstrip("/") or p.startswith(pref) for pref in dirs_norm)
        for p in paths
    )


def _entry_in_files(entry: dict, files_set: set[str]) -> bool:
    """True iff `defined_in` matches a file in the set, or any
    `declared_in` entry does."""
    df = entry.get("defined_in")
    if df and df in files_set:
        return True
    decls = entry.get("declared_in")
    if isinstance(decls, list):
        return any(d in files_set for d in decls)
    if isinstance(decls, str):
        return decls in files_set
    return False


def _delete_entries(
    target: Path,
    filename: str,
    *,
    entries_key: str,
    name_key: str,
    all_entries: bool = False,
    dirs: Iterable[str] | None = None,
    files: Iterable[str] | None = None,
    names: Iterable[str] | None = None,
    kinds: Iterable[str] | None = None,
) -> None:
    """Walk the analysis tree; for each `<filename>`, delete entries
    matching any of the narrowing predicates (dirs, files, names, kinds),
    or EVERY entry when `all_entries` is set (the `--all --reset` reset — so a
    full re-analysis recomposes fresh, current-schema skeletons rather than
    merging onto stale ones).

    With no predicates set (all None), deletes nothing.
    """
    import json
    dirs_norm = [_normalize_dir(d) for d in (dirs or [])]
    files_set = set(files or [])
    names_set = set(names or [])
    kinds_set = set(kinds or [])
    if not all_entries and not dirs_norm and not files_set and not names_set \
            and not kinds_set:
        return

    repo_root = _repo_root_for(target)
    tree = Layout(repo_root).analysis
    if not tree.exists():
        return

    for p in tree.rglob(filename):
        doc = json.loads(p.read_text())
        entries = doc.get(entries_key, [])
        kept: list[dict] = []
        removed = 0
        for e in entries:
            match = all_entries or (
                (names_set and e.get(name_key) in names_set)
                or (kinds_set and e.get("kind") in kinds_set)
                or (files_set and _entry_in_files(e, files_set))
                or (dirs_norm and _entry_in_dirs(e, dirs_norm))
            )
            if match:
                removed += 1
            else:
                kept.append(e)
        if removed:
            doc[entries_key] = kept
            p.write_text(json.dumps(doc, indent=2) + "\n")
            print(f"[crustify --reset] removed {removed} entries from {p}")
