"""Orchestration for the ``analyze`` command family.

The analyze pipeline has three stages, run as separate verbs at the
CLI level:

  1. ``crustify analyze scope``    — composer-only (no agent).
     Reads `<target>/.crustify/config.json`; writes
     `<target>/.crustify/scope.json` (port-scope file list).

  2. ``crustify analyze symbols``  — composer-then-agent.
     Composer (`compose.syms_manifest`) emits per-stem skeleton; the
     `CrustifySymbolAnalyzer` agent annotates semantic fields.

  3. ``crustify analyze types``    — composer-then-agent.
     Same pattern with `compose.types_manifest` +
     `CrustifyTypeAnalyzer`.

  ``crustify analyze --all`` runs 1 → 2 → 3 in sequence.

Stage 2 depends on 1; stage 3 depends on 2 (the type analyzer
agent reads syms manifests for op-candidate discovery via inverted
`depends_on.types` lookup). Composers are pure functions of T1 + T2
+ scope.json — fast, deterministic, run unconditionally.
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

from crustify.agents.analyzer import (
    CrustifySymbolAnalyzer,
    CrustifyTypeAnalyzer,
)


# ---------------------------------------------------------------- composer wrappers

def _scope(target: Path) -> Path:
    """Run the scope composer; return the path to scope.json.

    v2 anchors the manifest on the CodeQL T1 tables, so this stage
    depends on `build execute` having produced `crustify/codeql/t1/`.
    Gated accordingly.
    """
    from compose.scope_manifest import compose as scope_compose
    import json

    layout = Layout.discover(target)
    t1 = layout.t1
    if not (t1 / "functions.csv").is_file():
        print(
            f"error: scope v2 requires CodeQL T1 CSVs at {t1}.\n"
            f"       Run `crustify <target> build execute` first.",
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
        f"[crustify analyze scope] {len(p['files'])} files / "
        f"{len(p['functions'])} fn / {len(p['globals'])} gv / "
        f"{len(p['macros'])} macro / {len(p['types'])} types → {out}"
    )
    return out


# ---------------------------------------------------------------- subject runner

def _slug_tag(tag: str) -> str:
    """Sanitize a tag into a log-filename-safe stage suffix component."""
    return "".join(c if (c.isalnum() or c in "_.-") else "_" for c in tag)


def _analysis_focus(entry: dict, scope: str,
                    focus_by_key: dict[tuple[str, str], list[str]] | None = None):
    """Per-entry analysis surface for the type agent (bound-only).

      - **wrap** → `{fields, methods}`: the port-touched surface the
        agent must focus on. `fields` = the port-touched field names
        from the composer's transient `focus_by_key` (NOT the entry's
        `fields[]`, which now carries the *full* scope-agnostic layout);
        `methods` = the union of the two footprints (functions touching
        the type that port-scope code references). The agent bounds its
        op selection to these.
      - **port** → `"all"`: analyze the full declared layout and the
        global footprint, no restriction.

    `focus_by_key` is keyed by `(type tag, defined_in)`. When a wrap
    entry isn't present (e.g. anonymous-base typedefs, or a port-only
    fallback), fall back to the entry's own field names — preserving the
    pre-Option-B behaviour for those entries.

    The agent still applies the §4 keep/drop rules *within* this
    surface (bound-only — the focus narrows the candidate pool, it
    does not pre-attribute).
    """
    if scope == "port":
        return "all"
    key = (entry.get("name") or entry.get("type") or "", entry.get("defined_in") or "")
    if focus_by_key is not None and key in focus_by_key:
        fields = list(focus_by_key[key])
    else:
        fields = [
            f["name"] for f in entry.get("fields", [])
            if isinstance(f, dict) and f.get("name")
        ]
    methods: set[str] = set()
    for grp in ("opaque_in", "non_opaque_in"):
        for syms in (entry.get(grp) or {}).values():
            methods.update(syms)
    return {"fields": sorted(fields), "methods": sorted(methods)}


def _build_chains(
    out_root: Path,
    manifest_filename: str,
    entries_by_dir: dict[Path, list[dict]],
    dir_scope: dict[Path, str],
    *,
    entry_tag_key: str,
    parallel: bool,
    parallel_max: int,
    per_entry: bool,
    focus_by_key: dict[tuple[str, str], list[str]] | None = None,
) -> list[list[tuple[str, list[dict]]]]:
    """Build the agent-invocation plan from composer output.

    The plan is a list of **chains**. Each chain is a list of **jobs**
    that must run sequentially (typically because they target the same
    on-disk manifest file). Each job is a ``(stage_suffix, manifests)``
    tuple where ``manifests`` is the list passed to one agent
    invocation. Chains themselves run in parallel up to
    ``parallel_max`` (when ``parallel=True``).

    The four cases:

      - ``per_entry=False, parallel=False`` — one chain with one job
        carrying every manifest dir's record (one agent processes
        everything).
      - ``per_entry=False, parallel=True``  — one chain per manifest
        dir, each chain holds one job carrying that dir's single
        record (``names: "all"``). The ThreadPoolExecutor schedules
        ``parallel_max`` chains concurrently; the rest queue. Per-
        agent workload stays bounded to one stem-group, matching the
        existing per-dir effort budget for the symbol analyzer.
      - ``per_entry=True, parallel=False``  — one chain with one job
        per entry; jobs target potentially many different paths but
        run sequentially.
      - ``per_entry=True, parallel=True``   — one chain per manifest
        path; jobs within a chain share the path and run sequentially
        (write-safe); chains run in parallel.

    `entry_tag_key` is ``"name"`` for both symbols and types — the key
    under which each entry carries its unique identity within a manifest
    (types were migrated from ``type`` -> ``name``). Both subjects emit
    schema-agnostic identity records (a type's ``{tag, file, scope}``; a
    symbol batch's ``{symbols: [{name, file}]}``) that the agent resolves
    through `crustify query`; the merge primitive always preserves
    prior-run annotations.
    """
    def mfest_path(rel_dir: Path) -> str:
        return str(out_root / rel_dir / manifest_filename)

    def dir_scope_for(rel_dir: Path) -> str:
        return dir_scope.get(rel_dir, "wrap")

    sorted_dirs = sorted(entries_by_dir.items())

    if per_entry:
        # One chain per ENTRY — same-dir entries run CONCURRENTLY. The agents
        # write only through `crustify query --update`, which serializes
        # same-dir writes under a directory lock + atomic rename (no lost
        # update). So write-safety no longer needs same-path jobs grouped into a
        # sequential chain; each entry is its own chain and the pool runs them
        # in parallel.
        chains: list[list[tuple[str, list[dict]]]] = []
        for rel_dir, entries in sorted_dirs:
            scope = dir_scope_for(rel_dir)
            slug_base = str(rel_dir).replace("/", "_")
            for e in entries:
                tag = e.get(entry_tag_key) or ""
                if not tag:
                    continue
                stage_suffix = f"{slug_base}__{_slug_tag(tag)}"
                if entry_tag_key == "type":
                    # Slim, tag-centric job: identity + the port-touched
                    # analysis surface. The agent pulls its record + the
                    # types.json to write via `crustify query` (no pushed path).
                    focus = _analysis_focus(e, scope, focus_by_key)
                    record = {"tag": tag, "file": e.get("defined_in"),
                              "scope": scope}
                    if focus == "all":
                        record["fields"], record["methods"] = "all", "all"
                    else:
                        record["fields"] = focus["fields"]
                        record["methods"] = focus["methods"]
                else:
                    # Symbol identity tuple (schema-agnostic; resolved via
                    # `crustify query syms`). Symbols normally run per-file
                    # (per_entry=False); this single-symbol shape keeps the
                    # per-entry path consistent if it is ever enabled for syms.
                    record = {"symbols": [{"name": tag,
                                           "file": e.get("defined_in")}]}
                chains.append([(stage_suffix, [record])])
        if not parallel:
            # Collapse into one big chain so all jobs run serially.
            flat = [job for ch in chains for job in ch]
            chains = [flat] if flat else []
        return chains

    # per_entry=False: each manifest dir contributes one record with
    # ``names: "all"``.
    #
    #   - parallel=False  → one chain, one job, all records in a
    #     single agent invocation.
    #   - parallel=True   → one chain per manifest dir, each with one
    #     job carrying its single record. The ThreadPoolExecutor
    #     schedules ``parallel_max`` chains concurrently; as each
    #     finishes the next from the queue starts. Per-agent
    #     workload stays bounded to one manifest (~one stem-group of
    #     entries), preserving the existing per-dir effort budget
    #     while routing through the new manifests-list contract.
    if not sorted_dirs:
        return []

    # Symbol records are SCHEMA-AGNOSTIC identity tuples: the agent reads each
    # symbol via `query syms --name <name> --file <file>` and submits findings
    # via `--update`, never opening the manifest (mirrors the type analyzer).
    # `file` is the entry's defining file (None for a header typedef such as a
    # callback — disambiguated by name). No `scope` rides along: symbol analysis
    # is uniform, a fact about the C code, independent of what the porter later
    # does with it.
    def syms_for(entries: list[dict]) -> list[dict]:
        return [{"name": e["name"], "file": e.get("defined_in")}
                for e in entries if e.get("name")]

    if not parallel:
        all_records = [
            {"symbols": syms_for(entries)} for rel_dir, entries in sorted_dirs
        ]
        return [[("all", all_records)]]

    chains: list[list[tuple[str, list[dict]]]] = []
    for rel_dir, entries in sorted_dirs:
        slug = str(rel_dir).replace("/", "_")
        chains.append([(slug, [{"symbols": syms_for(entries)}])])
    return chains


def _run_chain(
    target: Path, agent_cls, jobs: list[tuple[str, list[dict]]],
) -> list[tuple[str, BaseException]]:
    """Run a chain's jobs sequentially. Continue-then-report on
    per-job failures so one bad entry doesn't strand its siblings.
    """
    failures: list[tuple[str, BaseException]] = []
    for stage_suffix, manifests in jobs:
        try:
            agent_cls(
                target,
                manifests=manifests,
                stage_suffix=stage_suffix,
            ).run()
        except BaseException as exc:  # noqa: BLE001 — continue-then-report
            failures.append((stage_suffix, exc))
    return failures


def _run_subject_manifests_list(
    target: Path,
    repo_root: Path,
    t1: Path,
    t2: Path,
    *,
    subject: str,
    filter_spec,
    parallel: bool,
    parallel_max: int,
    per_entry: bool = False,
    compose_only: bool = False,
) -> None:
    """Unified subject runner using the manifests-list contract.

    Runs the composer once (fast, single-threaded), writes the
    composer output to disk via the merge primitive, then builds and
    executes the agent-invocation plan via :func:`_build_chains` and
    :func:`_run_chain`. Continue-then-report on agent failures: every
    chain runs to completion (or its own failure); a SystemExit is
    raised at the end summarising any failures.

    Used for both ``symbols`` and ``types`` subjects — only the
    composer module, agent class, manifest filename, entries key, and
    entry-tag key differ.

    Granularity:
      - ``per_entry=False`` — manifests-list mode. Without
        ``--parallel`` a single agent receives every manifest at
        once; with ``--parallel`` manifests are round-robin
        partitioned across up to ``parallel_max`` concurrent agents.
      - ``per_entry=True`` — one agent per composer-identified entry.
        Same-path agents run sequentially in a chain (write-safe);
        chains run in parallel up to ``parallel_max``. Each agent's
        effort budget is bounded to one entry — the structural guard
        against the overload bail in PITFALLS §2026-06-05.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Subject dispatch — composer, agent class, schema bits.
    if subject == "symbols":
        from compose.syms_manifest import compose as compose_fn
        from compose.syms_manifest import _COMMENT as COMMENT
        from compose.manifest_merge import merge_manifest_file, symbol_key as key_fn
        manifest_filename = manifest_name("symbols")
        entries_key = "symbols"
        entry_tag_key = "name"
        agent_cls = CrustifySymbolAnalyzer
    elif subject == "types":
        from compose.types_manifest import compose as compose_fn
        from compose.types_manifest import _COMMENT as COMMENT
        from compose.manifest_merge import merge_manifest_file, type_key as key_fn
        manifest_filename = manifest_name("types")
        entries_key = "types"
        entry_tag_key = "name"
        agent_cls = CrustifyTypeAnalyzer
    else:
        raise ValueError(f"unknown subject: {subject!r}")

    out_root = Layout(repo_root).analysis

    # 1. Composer (single-threaded, fast).
    #    `focus_by_key` is the transient per-target analysis surface for
    #    type entries (port-touched field names by entry identity); empty
    #    for symbols. It's NOT persisted — only used to bound the agent's
    #    workset below (the manifest carries the full, scope-agnostic layout).
    entries_by_dir, dir_scope, focus_by_key = compose_fn(t1, t2, filter_spec)

    # 2. Emit skeletons to disk (merge-primitive semantics preserve
    #    any prior agent annotations).
    for rel_dir, entries in sorted(entries_by_dir.items()):
        manifest = {"_comment": COMMENT, entries_key: entries}
        merge_manifest_file(
            out_root / rel_dir / manifest_filename,
            manifest,
            entries_key=entries_key,
            key=key_fn,
        )
    print(
        f"[crustify analyze {subject}] composer: "
        f"{len(entries_by_dir)} dirs → {out_root}"
    )

    if not entries_by_dir:
        print(f"[crustify analyze {subject}] no entries; nothing to do.")
        return

    # --compose-only: emit the deterministic skeletons (merged with any prior
    # agent annotations) and stop before spawning any analyzer agent. The
    # composer side is the fresh-tree generator; the agent pass (ownership /
    # lifecycle annotation) is the expensive LLM step deliberately skipped here.
    if compose_only:
        print(
            f"[crustify analyze {subject}] --compose-only: skeletons emitted, "
            f"no agent spawned."
        )
        return

    # 3. Build agent-invocation plan. The type analyzer only annotates
    #    STRUCTS (real structs + generated-container instances, which are
    #    ordinary structs). Enums, callbacks, and synthetic clusters stay
    #    in the manifest (composer-filled, deterministic) but carry NO
    #    agent worklist entry: enums have no lifecycle/fields; callbacks
    #    are signature-shaped → the SYMBOL analyzer's job; string/array
    #    are the buffer pass; typegen engines are the (deferred) wrap pass.
    worklist_by_dir = entries_by_dir
    if subject == "types":
        worklist_by_dir = {
            d: kept for d, es in entries_by_dir.items()
            if (kept := [e for e in es if e.get("kind") == "struct"])
        }
    chains = _build_chains(
        out_root, manifest_filename, worklist_by_dir, dir_scope,
        entry_tag_key=entry_tag_key,
        parallel=parallel, parallel_max=parallel_max, per_entry=per_entry,
        focus_by_key=focus_by_key,
    )
    total_jobs = sum(len(c) for c in chains)
    if total_jobs == 0:
        print(f"[crustify analyze {subject}] no agent jobs derivable; nothing to do.")
        return
    granularity_note = (
        "1 agent/entry" if per_entry else
        ("1 agent/dir, pool-scheduled" if parallel else "single agent over all dirs")
    )
    print(
        f"[crustify analyze {subject}] spawning {total_jobs} agent job(s) "
        f"across {len(chains)} chain(s) "
        f"({granularity_note}, max {parallel_max} chain(s) concurrent)"
    )

    # 4. Run chains. Each chain runs sequentially within itself;
    #    chains run in parallel up to ``parallel_max``.
    failures: list[tuple[str, BaseException]] = []
    if parallel and len(chains) > 1:
        with ThreadPoolExecutor(max_workers=parallel_max) as ex:
            futures = {
                ex.submit(_run_chain, target, agent_cls, jobs): (idx, len(jobs))
                for idx, jobs in enumerate(chains)
            }
            for fut in as_completed(futures):
                chain_idx, n_jobs = futures[fut]
                try:
                    chain_failures = fut.result()
                except BaseException as exc:  # noqa: BLE001
                    failures.append((f"chain_{chain_idx}", exc))
                    print(
                        f"[crustify analyze {subject}] chain_{chain_idx}: "
                        f"CHAIN FAILED — {type(exc).__name__}: {str(exc)[:120]}"
                    )
                    continue
                for sub_suffix, sub_exc in chain_failures:
                    failures.append((sub_suffix, sub_exc))
                    print(
                        f"[crustify analyze {subject}] chain_{chain_idx} "
                        f"{sub_suffix} FAILED — {type(sub_exc).__name__}: "
                        f"{str(sub_exc)[:120]}"
                    )
                ok = n_jobs - len(chain_failures)
                print(
                    f"[crustify analyze {subject}] chain_{chain_idx}: "
                    f"{ok}/{n_jobs} agents ok"
                )
    else:
        # Serial: walk chains in deterministic order, jobs sequentially.
        for chain_idx, jobs in enumerate(chains):
            chain_failures = _run_chain(target, agent_cls, jobs)
            for sub_suffix, sub_exc in chain_failures:
                failures.append((sub_suffix, sub_exc))
                print(
                    f"[crustify analyze {subject}] {sub_suffix} FAILED "
                    f"— {type(sub_exc).__name__}: {str(sub_exc)[:120]}"
                )

    if failures:
        details = "\n".join(
            f"  - {slug}: {type(exc).__name__}: {str(exc)[:200]}"
            for slug, exc in failures
        )
        raise SystemExit(
            f"\n{len(failures)} of {total_jobs} {subject} agent "
            f"invocation(s) failed:\n{details}\n\n"
            f"Successfully ran {total_jobs - len(failures)}/{total_jobs} "
            f"agents. The merge primitive's field-level union means a "
            f"retry (e.g. `--reset --dir <failed_dir>` or "
            f"`--name <failed_tag>`) only re-runs the failed work."
        )


# ---------------------------------------------------------------- public verbs

def analyze_scope(
    target: Path, *, port_only: bool = False, wrap_only: bool = False,
) -> None:
    """Derive one section of scope.json — exactly one of ``port_only`` /
    ``wrap_only`` (they are produced at different points in the pipeline):

      - ``port_only`` — the ``port`` section, from ``config.json`` (the seed;
        must run before files/symbols/types).
      - ``wrap_only`` — the derived ``wrap`` import-closure section. Walks port
        entities' ``depends_on`` edges into the FFI items they use, narrowed to
        the header(s) the importing TU ``#include``s. Appended alongside
        ``port``; requires ``analyze symbols``/``types`` to have run.
    """
    if port_only == wrap_only:
        raise SystemExit(
            "analyze scope: pass exactly one of --port-only / --wrap-only.")
    if port_only:
        _scope(target)
    else:
        _wrap_scope(target)


def analyze_symbols(
    target: Path, *,
    filter_spec=None,
    parallel: bool = False,
    parallel_max: int = 8,
    compose_only: bool = False,
) -> None:
    """Stage 2: compose syms skeletons + run the symbol analyzer agent.

    `filter_spec` narrows the composer's emission (which manifest dirs
    and which entries within them survive the seed/closure / scope /
    name filters). The composer's output is then fed to the agent via
    the manifests-list contract — each agent invocation receives a
    `manifests` list of ``{symbols: [{name, file}], scope}`` records
    (one per stem-group dir; the symbols are schema-agnostic identity
    tuples). The agent reads/writes each symbol through `crustify query
    syms`, never opening a manifest.

    Without `--parallel` a single agent processes every manifest in
    one invocation. With `--parallel` each manifest dir becomes its
    own chain (one agent invocation per dir, single-manifest
    workload); the ThreadPoolExecutor schedules ``parallel_max``
    chains concurrently and queues the rest.
    """
    repo_root = _repo_root_for(target)
    t1 = Layout(repo_root).t1
    t2 = Layout(repo_root).t2

    _run_subject_manifests_list(
        target, repo_root, t1, t2,
        subject="symbols",
        filter_spec=filter_spec,
        parallel=parallel,
        parallel_max=parallel_max,
        per_entry=False,
        compose_only=compose_only,
    )


def analyze_types(
    target: Path, *,
    filter_spec=None,
    parallel: bool = False,
    parallel_max: int = 8,
    compose_only: bool = False,
) -> None:
    """Stage 3: compose types skeletons + run the type analyzer agent.

    Uses the manifests-list contract. The agent receives a `manifests`
    list of ``{path, names, scope}`` records and does no selection
    parsing of its own.

    Granularity is always **per-entry** (one agent per composer-identified
    STRUCT): structs each carry substantial per-entry analysis work
    (lifecycle classification, per-field accessors, locking, conditional
    drop, per-pointer ownership), and one-per-entry bounds the per-agent
    effort budget. Enums / synthetic clusters get no type-agent job (callbacks
    are symbols now — see `_run_subject_manifests_list`). Same-path agents form a chain (sequential;
    write-safe); chains run in parallel up to `parallel_max` when
    `--parallel` is set. Without `--parallel` a single chain
    sequences all jobs.
    """
    repo_root = _repo_root_for(target)
    t1 = Layout(repo_root).t1
    t2 = Layout(repo_root).t2

    seed_mode = filter_spec is not None and filter_spec.is_seed_mode()

    # The per-entry pass analyzes every struct (generated-container instances
    # included — an instance is just an ordinary struct); enums, callbacks, and
    # synthetic clusters are filtered out of the worklist. The buffer pass is
    # skipped in seed mode — a focused --name X shouldn't trigger global
    # synthesis.
    _run_subject_manifests_list(
        target, repo_root, t1, t2,
        subject="types",
        filter_spec=filter_spec,
        parallel=parallel,
        parallel_max=parallel_max,
        per_entry=True,
        compose_only=compose_only,
    )

    # The buffer/synthesis pass is an agent pass too — skip it under
    # --compose-only (and in seed mode, as before).
    if not seed_mode and not compose_only:
        run_buffer_pass(target)


def run_buffer_pass(target: Path) -> None:
    """Single cross-cutting pass that creates `string` / `array`
    allocator-cluster entries. Gated on alloc.json (the allocator
    universe catalogue). Skipped with a note when absent.
    """
    alloc = Layout.discover(target).alloc_json
    if not alloc.exists():
        print(
            "[crustify analyze types] buffer pass: alloc.json absent — skipping "
            "string/array cluster synthesis. Run `crustify <target> alloc` "
            "first to catalogue the allocator universe, then re-run "
            "`analyze types --all` to synthesize them."
        )
        return
    print("[crustify analyze types] buffer pass: creating string/array clusters")
    CrustifyTypeAnalyzer(
        target,
        selection="strings; arrays",
        stage="buffer_analyzer",
    ).run()


def analyze_dag(target: Path) -> None:
    """Stage 4: emit deps-dag.json — the scope-agnostic unified
    types+symbols dependency DAG.

    Reads every ``types.json`` / ``syms.json`` under the analysis tree
    and writes the layered DAG (`stats` + `layers[]`) to
    ``<analysis>/deps-dag.json``. Downstream (wrap, port) consumes it
    as the layered work plan; classifying nodes port vs wrap is the
    consumer's job (apply ``scope.entry_scope`` per origin).

    Requires a populated analysis tree (``analyze symbols`` + ``types``).
    """
    from compose.deps_dag import compose as deps_dag_compose
    import json

    analysis = Layout.discover(target).analysis
    if not analysis.exists() or not any(analysis.rglob("types.json")):
        print(
            f"error: analyze dag requires a populated analysis tree at "
            f"{analysis}.\n"
            f"       Run `crustify <target> analyze types` (and `symbols`) "
            f"first.",
            file=sys.stderr,
        )
        sys.exit(1)

    dag = deps_dag_compose(analysis)
    out = analysis / "deps-dag.json"
    out.write_text(json.dumps(dag, indent=2) + "\n")
    s = dag["stats"]
    print(
        f"[crustify analyze dag] {s['nodes']} nodes "
        f"({s['types']} types / {s['symbols']} syms / "
        f"{s['external_syms']} ext) / {s['edges']} edges / "
        f"{s['layers']} layers / {s['sccs_flattened']} cycle(s) flattened "
        f"({s['fallback_edges']} fallback edges) → {out}"
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
    section) and ``build execute`` (the T1 ``includes.csv`` + T1/T2 tables).
    """
    import json
    from compose import scope as scope_mod
    from compose.wrap_closure import compose_wrap

    layout = Layout.discover(target)
    scope_path = layout.scope(target)
    if not scope_path.exists():
        print(
            f"error: scope.json missing at {scope_path}. Run "
            f"`crustify {target} analyze scope --port-only` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    includes_csv = layout.t1 / "includes.csv"
    if not includes_csv.is_file():
        print(
            f"error: includes.csv missing at {includes_csv}. Run "
            f"`crustify {target} build execute` first.",
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

    # NOTE: synthetic types (string/array clusters) are NOT written to
    # scope.json — they are *always* wrap-scope, so consumers treat any
    # synthetic-kind entity as wrap unconditionally. Only their real ops flow
    # through scope.json's normal sym wrap/port boundary.
    manifest = json.loads(scope_path.read_text())
    manifest["wrap"] = wrap
    scope_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"[crustify analyze scope --wrap-only] {len(wrap['files'])} files / "
        f"{len(wrap['functions'])} fn / {len(wrap['globals'])} gv / "
        f"{len(wrap['macros'])} macro / {len(wrap['types'])} types → {scope_path}"
    )


# ---------------------------------------------------------------- reset helpers

def reset_scope(target: Path) -> None:
    """Delete scope.json so the next ``analyze scope`` re-emits fresh."""
    p = Layout.discover(target).scope(target)
    if p.exists():
        p.unlink()
        print(f"[crustify --reset] removed {p}")


def reset_dag(target: Path) -> None:
    """Delete deps-dag.json so the next ``analyze dag`` re-emits fresh."""
    p = Layout.discover(target).analysis / "deps-dag.json"
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
