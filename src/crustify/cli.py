from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parallel_max_type(s: str) -> int:
    """argparse type for `--parallel-max`: integer ≥ 2."""
    try:
        n = int(s)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--parallel-max: expected integer, got {s!r}"
        )
    if n < 2:
        raise argparse.ArgumentTypeError(
            "--parallel-max: must be ≥ 2 (1 is the serial default; "
            "drop --parallel to run serially)"
        )
    return n


def _add_analyze_filter_flags(p: argparse.ArgumentParser) -> None:
    """Narrowing flags for `analyze {{symbols,types}}` subjects.

    **Seed selectors** (union): `--dir` / `--file` / `--name`.
    Without `--all`, at least one of these is required.

    **Scope**: port-aware analysis uses the target's
    `crustify/targets/<target>/scope.json` automatically (computed from
    the target, alongside its config.json); there is no flag to pass.
    When that scope.json is absent, all entries emit as wrap-shape
    (base only).

    **Post-emission filters**: `--port-only` / `--wrap-only` (mutually
    exclusive). Apply after seed/closure logic to keep only entries
    with port additions (port-only) or without (wrap-only).

    `--reset` may combine with any of these.
    """
    p.add_argument(
        "--all", action="store_true",
        help="Process every entry in the analysis tree (no narrowing).",
    )
    p.add_argument(
        "--dir", nargs="+", metavar="DIR", default=None,
        help="Narrow to entries whose source path falls under these "
             "repo-root-relative directories (e.g. crypto/, ssl/, "
             "include/openssl/). Repeatable.",
    )
    p.add_argument(
        "--file", nargs="+", metavar="FILE", default=None,
        help="Narrow to entries defined or declared in these files. "
             "Accepts a repo-root-relative path (e.g. "
             "ssl/statem/statem_local.h) or a bare basename "
             "(e.g. statem_local.h) when uniquely identifiable in "
             "the analysis tree. Repeatable.",
    )
    p.add_argument(
        "--name", nargs="+", action="extend", metavar="NAME", default=None,
        help="Seed ONLY the given symbol or type names -- precise, no "
             "transitive closure (the named entities are emitted/annotated "
             "alone; use --dir/--file to pull a region's closure). Pass all "
             "names as a space-separated list after a single --name (e.g. "
             "`--name a b c`); repeating the flag keeps only the last group.",
    )
    # Scope selection (mutually exclusive). Default (none set) = --scope-only:
    # emit + analyze port ∪ wrap. The composer classifies port/wrap into the
    # per-target scope.json (no per-entry tag); these pick which slice rides.
    post_filter = p.add_mutually_exclusive_group()
    post_filter.add_argument(
        "--scope-only", action="store_true",
        help="Port ∪ wrap (the default): emit the port-reachable surface and "
             "analyze it. Explicit form of the default scope.",
    )
    post_filter.add_argument(
        "--port-only", action="store_true",
        help="After seed/closure, keep only port-shape entries (with "
             "port additions). Equivalent to wanting just the "
             "port-scope subset of the result.",
    )
    post_filter.add_argument(
        "--wrap-only", action="store_true",
        help="After seed/closure, keep only base-shape entries. "
             "Equivalent to wanting just the wrap-scope subset of "
             "the result.",
    )
    post_filter.add_argument(
        "--unscoped", action="store_true",
        help="Repo-wide: emit EVERY candidate, skipping the out-of-scope "
             "reachability drop (so dispatch-table-only handlers and their "
             "primitives are present). scope.json still classifies port/wrap. "
             "Pairs with --compose-only for a cheap comprehensive foundation.",
    )
    p.add_argument(
        "--reset", action="store_true",
        help="Delete matching entries before running.",
    )
    p.add_argument(
        "--compose-only", action="store_true", dest="compose_only",
        help="Run only the deterministic composer (emit/merge the manifest "
             "skeletons) and skip the analyzer agent — a fresh analysis tree "
             "with no LLM spend. For types this also skips the buffer pass.",
    )
    p.add_argument(
        "--out-suffix", dest="out_suffix", default=None, metavar="SUFFIX",
        help="Write/read manifests as types_<SUFFIX>.json / syms_<SUFFIX>.json "
             "instead of the canonical types.json / syms.json, isolating this "
             "run from the real tree (and from other suffixed runs). The "
             "composer emits a fresh suffixed skeleton; the agents -- which "
             "shell out to `crustify query` with no pushed path -- pick up the "
             "suffix via the CRUSTIFY_OUT_SUFFIX env var. Lets several analyzer "
             "runs (e.g. model-comparison instances) execute in parallel "
             "without clobbering; downstream stages (dag/wrap/port) read only "
             "the canonical names and ignore suffixed artifacts.",
    )


def _add_subject_scope_flags(
    p: argparse.ArgumentParser,
    *,
    include_names: bool = True,
    include_files: bool = True,
    include_strings_arrays: bool = False,
    include_scope: bool = True,
    include_libraries: bool = True,
) -> None:
    """Attach the unified subject + scope filter flag set to a subparser.

    Subject filter group (mutually exclusive; defaults to ``--all``
    semantics when none is set):

      ``--all``     — all matching entries
      ``--names``   — entries by name
      ``--files``   — entries defined/declared in specific files
      ``--strings`` — synthetic ``string_system`` clusters (types only)
      ``--arrays``  — synthetic ``array_system`` clusters (types only)

    Scope filter group (mutually exclusive; defaults to both scopes):

      ``--port``    — narrow to port-scope manifest
      ``--wrap``    — narrow to wrap-scope manifest

    Optional refinement:

      ``--libraries L [L ...]`` — narrow to entries tagged with these
      libraries. Requires ``--wrap`` (or implicit wrap context, as in
      the ``wrap`` verb's subjects).

    Per-subcommand availability is controlled by the include_* flags —
    e.g. ``analyze scope`` doesn't take ``--names`` or scope filters;
    ``wrap types`` has no scope group (wrap-scope is implicit) but does
    take ``--libraries``.
    """
    subject = p.add_mutually_exclusive_group()
    subject.add_argument(
        "--all", action="store_true",
        help="Match all entries in the chosen scope (default).",
    )
    if include_names:
        subject.add_argument(
            "--names", nargs="+", metavar="NAME",
            help="Match entries by name (struct tag / symbol name / typedef alias).",
        )
    if include_files:
        subject.add_argument(
            "--files", nargs="+", metavar="FILE",
            help="Match entries defined or declared in these files.",
        )
    if include_strings_arrays:
        subject.add_argument(
            "--strings", action="store_true",
            help="Match synthetic string_system clusters.",
        )
        subject.add_argument(
            "--arrays", action="store_true",
            help="Match synthetic array_system clusters.",
        )

    if include_scope:
        scope = p.add_mutually_exclusive_group()
        scope.add_argument(
            "--port", action="store_true",
            help="Narrow to port-scope manifest.",
        )
        scope.add_argument(
            "--wrap", action="store_true",
            help="Narrow to wrap-scope manifest.",
        )

    if include_libraries:
        p.add_argument(
            "--libraries", nargs="+", metavar="LIB", default=None,
            help="Narrow to entries tagged with these libraries "
                 "(requires --wrap or implicit wrap context).",
        )


def _add_query_flags(p: argparse.ArgumentParser, *, facets: bool) -> None:
    """Flags for `query types`/`query syms` — the read-only oracle, resolved
    from the manifest (dag-free). With no `--name` they enumerate (filtered by
    scope / synthetic kind / `--file`) as a name list; with `--name T` they
    introspect one entry — always the WHOLE record (several names → several
    records). On a type, `--fields`/`--ops` print its windowable lists
    (`facets`). The .rs module of an entry is found via
    `crustify <target> scaffold --name <X>`, not here."""
    sc = p.add_mutually_exclusive_group()
    sc.add_argument("--wrap-only", action="store_true", dest="wrap_only",
                    help="Narrow to wrap scope: enumeration → wrap-scope entries; "
                         "--ops/--methods → wrap-scope functions; --fields/--field-touchers "
                         "→ fields touched by wrap-scope code. (Facets are complete "
                         "by default.)")
    sc.add_argument("--port-only", action="store_true", dest="port_only",
                    help="Narrow to port scope: enumeration → port-scope entries; "
                         "--ops/--methods → port-scope functions; --fields/--field-touchers "
                         "→ fields touched by port-scope code. (Facets are complete "
                         "by default.)")
    p.add_argument("--arrays", action="store_true",
                   help="Synthetic array clusters.")
    p.add_argument("--strings", action="store_true",
                   help="Synthetic string clusters.")
    p.add_argument("--typegens", action="store_true",
                   help="(symbols) Type-generator macro primitives (macro.typegen) "
                        "— the DEFINE_*/DECLARE_* families that generate types.")
    p.add_argument("--name", nargs="+", action="extend", default=None, metavar="NAME",
                   help="No --name → enumerate; one → introspect; several → batch records.")
    p.add_argument("--file", nargs="+", default=None, metavar="FILE",
                   dest="files",
                   help="Restrict/disambiguate by defining file.")
    facet = p.add_mutually_exclusive_group()
    facet.add_argument("--manifest", action="store_true",
                       help="Introspect: print the types.json/syms.json that homes this entry.")
    # `--update` is available for BOTH subjects (types AND syms): the schema
    # boundary through which an analyzer agent merges its findings.
    facet.add_argument("--update", default=None, metavar="FINDINGS",
                       help="Ingest an agent findings JSON (path or '-' for stdin) "
                            "into the named entry: validate (hard-reject only), then "
                            "partial-merge under a lock. types: lifecycle + per-field "
                            "ptr. syms: macro kind + per-arg/return "
                            "ownership (ptr_args/ptr_ret). The agent never edits the "
                            "manifest directly.")
    facet.add_argument("--update-help", action="store_true", dest="update_help",
                       help="Print the findings JSON schema that --update expects "
                            "for this subject (types vs syms), then exit. No --name "
                            "needed — schema discovery for the analyzer agent.")
    facet.add_argument("--schema", action="store_true", dest="schema",
                       help="Print the record's field/slot DEFINITIONS (the "
                            "_comment_* blocks, the schema authority), then exit. "
                            "No --name needed. types: struct-analyzer fields only "
                            "(synthetic string/array-cluster fields excluded).")
    if facets:
        facet.add_argument("--fields", action="store_true",
                           help="Introspect a type: ALL declared fields with their "
                                "per-field structural + ptr detail (--port-only/"
                                "--wrap-only narrow to that scope's touched fields); "
                                "'[]' if none.")
        facet.add_argument("--ops", action="store_true",
                           help="Introspect a type: its method surface "
                                "(lifecycle ops), lifecycle-first.")
        facet.add_argument("--methods", action="store_true",
                           help="Introspect a type: its COMPLETE footprint — the "
                                "opaque_in ∪ non_opaque_in functions (every function "
                                "tree-wide that touches the type, incl. out-of-scope); "
                                "--port-only/--wrap-only intersect with that scope's "
                                "functions; '[]' if none.")
        facet.add_argument("--field-touchers", action="store_true",
                           dest="field_touchers",
                           help="Introspect a type: {field: [touchers]} — ALL "
                                "declared fields by default (--port-only/--wrap-only "
                                "narrow the FIELDS to that scope's touched subset); "
                                "each field's toucher set is the COMPLETE, unfiltered "
                                "set of functions that access it.")
        facet.add_argument("--create", default=None, metavar="ENTRY",
                           help="Ingest a WHOLE synthetic string/array cluster entry "
                                "(JSON path or '-' for stdin) — the buffer pass's "
                                "create path: validate, home it by `defined_in`, write "
                                "under a lock. The agent never edits types.json.")
        p.add_argument("--range", default=None, metavar="A:B", dest="rng",
                       help="Window the --fields/--ops/--methods list to [A:B).")
    else:
        # Symbols-only REVERSE lifecycle lookup, parameterized by a TYPE (no
        # --name). Feeds the type analyzer: which symbols realize TYPE's Drop /
        # dispose / Clone, read off the arg-level lifetime flags.
        facet.add_argument(
            "--lifetime-for", default=None, metavar="SPEC", dest="lifetime_for",
            help="Reverse lifecycle lookup (READ: flags that already exist): "
                 "every symbol with an ARG matching SPEC whose ptr carries a "
                 "lifetime flag (is_dropped/is_disposed/is_cloned), grouped into "
                 "the type's dropped_by / fields_disposed_by / cloned_by. SPEC is "
                 "a struct tag / typedef, or the keyword `void` (raw byte-level, "
                 "untyped) or `string` (NUL-terminated; the char family or the "
                 "analyzer's own ptr.string verdict). Lifetime flags are arg-only, "
                 "so returns are not scanned. No --name needed.")
        facet.add_argument(
            "--taking", default=None, metavar="SPEC", dest="taking",
            help="CANDIDATE discovery (the inverse of --lifetime-for, which reads "
                 "flags that already exist): every symbol with an ARG matching "
                 "SPEC (tag / typedef / `void` / `string`). Pair with --calling "
                 "to keep only those that reach a lifecycle primitive. No --name "
                 "needed.")
        p.add_argument(
            "--calling", default=None, metavar="FN[,FN...]", dest="calling",
            help="Narrow --taking to symbols that reach one of these routines "
                 "within --hops call hops (via the composer's depends_on.syms). "
                 "A dropper/cloner must ultimately reach a raw primitive -- but "
                 "the top-level one often does so through a helper, so >1 hop is "
                 "the norm (e.g. ASN1_STRING_free -> "
                 "ossl_asn1_string_free_internal -> CRYPTO_free is 2 hops).")
        p.add_argument(
            "--hops", type=int, default=1, metavar="N", dest="hops",
            help="Call-hop depth for --calling (default 1). NOT capped -- every "
                 "function transitively reaches malloc/free, so depth is a "
                 "precision/recall trade the caller owns.")
        p.add_argument(
            "--array", action="store_true", dest="array",
            help="With --lifetime-for/--taking: keep only args whose ptr carries "
                 "an `array` shape (a buffer, not a lone pointee). Only "
                 "meaningful on an analyzed record.")


def _add_wrap_filter_flags(p: argparse.ArgumentParser) -> None:
    """Selection flags for the unified `wrap` command (types + free symbols,
    no subject split). Wrap is **scope-blind by default**
    — a named entity wraps regardless of port/wrap scope (no refusal).
    `--wrap-only` / `--port-only` are opt-in *narrowing* filters, and `--file`
    restricts to a defining file (disambiguating a `--name` collision). The
    synthetic selectors (`--strings`/`--arrays`) build the working
    set from cluster kinds.

    A per-agent **effort budget** (`--max-fields` / `--max-ops`) caps each
    type's workload so a "god object" can't blow one agent's context; the
    orchestrator slices the surface and hands the agent a fixed worklist.
    """
    p.add_argument(
        "--name", nargs="+", action="extend", metavar="NAME", default=None,
        help="Select unit(s) by name (any scope). Pass all names as a "
             "space-separated list after a single --name (e.g. "
             "`--name T1 sym2 sym3`) to batch them; repeating the flag keeps "
             "only the last group. A type name brings its in-scope ops; a "
             "free-symbol name wraps it alone. The user supplies dependency "
             "order.",
    )
    p.add_argument(
        "--file", nargs="+", metavar="FILE", default=None, dest="files",
        help="Restrict the selection to entities defined in these files "
             "(disambiguates a --name collision).",
    )
    _wrap_scope = p.add_mutually_exclusive_group()
    _wrap_scope.add_argument(
        "--wrap-only", action="store_true", dest="wrap_only",
        help="Narrow the selection to wrap-scope entities.",
    )
    _wrap_scope.add_argument(
        "--port-only", action="store_true", dest="port_only",
        help="Narrow the selection to port-scope entities.",
    )
    p.add_argument(
        "--strings", action="store_true",
        help="Narrow to synthetic `string` clusters (wrapped first; see §4.7).",
    )
    p.add_argument(
        "--arrays", action="store_true",
        help="Narrow to synthetic `array` clusters (wrapped first; see §4.7).",
    )
    p.add_argument(
        "--max-fields", type=int, default=None, metavar="N", dest="max_fields",
        help="Per-type field budget: wrap at most the first N fields[] of a "
             "type, deferring the rest. Default: config.WRAP_MAX_FIELDS.",
    )
    p.add_argument(
        "--max-syms", type=int, default=None, metavar="N", dest="max_syms",
        help="Per-batch symbol budget for wrap: a type's op-chunk size "
             "(lifecycle + method ops counted together) AND the free-symbol "
             "pool size per file. Default: config.WRAP_MAX_SYMS.",
    )
    p.add_argument(
        "--dag-layer", type=int, default=None, metavar="N", dest="dag_layer",
        help="Select EVERY in-scope unit at dag layer N (types + wrap-scope "
             "syms; lifecycle ops that fold into a type are excluded — they "
             "ride with `wrap types`). Combines with --name; e2e driver mode.",
    )
    p.add_argument(
        "--skip", nargs="+", action="extend", default=None, metavar="NAME",
        help="Blocklist: drop these names from the selection (manual "
             "already-done list; the driver seeds it, crustify just honours it).",
    )
    p.add_argument(
        "--parallel-max", type=int, default=8, metavar="N", dest="parallel_max",
        help="Max concurrent agents across disjoint files (with --parallel).",
    )
    p.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the dependency-confirmation prompt.",
    )
    p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print the plan (units, batches, first-layer deps) and stop.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="crustify",
        description="Multi-agent C-to-Rust translation pipeline.",
    )
    parser.add_argument(
        "repo_root",
        help="Full path to the repository root (its artifacts live under "
             "<repo_root>/crustify/). Explicit — crustify never walks the "
             "filesystem to find it.",
    )
    parser.add_argument(
        "target",
        help="Repo-relative target subdirectory crustify is scoped to "
             "(e.g. ssl/statem), matching its crustify/targets/<target>/ "
             "config.json. Use . for the repo root; a repo-wide analysis is "
             "normally driven by --unscoped on a real target.",
    )
    parser.add_argument(
        "--no-console",
        action="store_true",
        default=False,
        help="Suppress live console output from agents.",
    )
    parser.add_argument(
        "--no-file-log",
        action="store_true",
        default=False,
        help="Disable per-agent log files under targets/<target>/logs/<session>/.",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help="Override every agent's model. Named <provider>/<model>, "
             "e.g. anthropic/claude-opus-4-8, openai/gpt-5.6, "
             "openrouter/z-ai/glm-4.6. The provider selects both the "
             "billing rates and the CLI that drives it. Default: each "
             "agent's hard-coded model.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Agent backend driving each stage. Default: config.BACKEND.",
    )
    parser.add_argument(
        "--billing",
        default=None,
        choices=["subscription", "api"],
        help="How the provider CLI authenticates: subscription (its own "
             "logged-in account) or api (an API key from the environment). "
             "Default: config.BILLING.",
    )
    parser.add_argument(
        "--override-base-prompt",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Replace the provider CLI's own base prompt with crustify's "
             "(default). --no-override-base-prompt keeps the provider's "
             "instructions underneath crustify's stage prompt.",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help="Enable command-specific parallelization. Different "
             "commands interpret this differently — see each command's "
             "help. For `analyze symbols` and `analyze types` this "
             "spawns one agent per stem-group manifest dir, capped at "
             "`--parallel-max`. Composer-only stages "
             "(`analyze extract-ql`, `analyze scope`, `analyze dag`) "
             "ignore this flag.",
    )
    parser.add_argument(
        "--parallel-max",
        type=_parallel_max_type,
        default=8,
        metavar="N",
        help="Maximum concurrent agents when --parallel is on. Must be "
             "≥ 2 (1 is meaningless — that's the serial default). "
             "Default 8.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("alloc", help="Produce alloc.json — the allocator-surface catalogue.")

    audit_p = sub.add_parser(
        "audit",
        help="Deterministic audit (no LLM): entity-seeded global scan of the "
             "ported Rust tree, printed as JSON to stdout (nothing written to "
             "disk). Per seed: its own unsafe / raw-pointer / naked-ffi surface; "
             "plus a tree-wide `global` section (outside-impl raw ptrs, the "
             "ffi:: type-surface partition, and a c_void filter) and `totals`.",
    )
    # Seed selectors (entity-seeded, global search) — mirror wrap/port. The
    # search is always global; the selector only picks the seed types/symbols.
    audit_sel = audit_p.add_mutually_exclusive_group(required=True)
    audit_sel.add_argument(
        "--all", action="store_true",
        help="Seed every wrapped type ∪ symbol (same as naming them all).",
    )
    audit_sel.add_argument(
        "--name", nargs="+", action="extend", default=None, metavar="NAME",
        help="Seed these type tags / symbol names (e.g. `--name git_oid "
             "git_oid_cpy`). Each is audited for its own surface + naked "
             "ffi:: footprint.",
    )
    audit_sel.add_argument(
        "--crate", default=None, metavar="CRATE",
        help="Seed every entity homed in this crate (e.g. `--crate libgit2`).",
    )
    audit_sel.add_argument(
        "--mod", default=None, dest="mod", metavar="MOD",
        help="Seed entities homed under a module path prefix (under the crate's "
             "src/, e.g. `--mod include` or `--mod src/libgit2`).",
    )
    audit_sel.add_argument(
        "--dir", default=None, metavar="DIR",
        help="Alias of --mod: seed entities homed under this path prefix.",
    )
    audit_sel.add_argument(
        "--file", default=None, metavar="FILE",
        help="Seed entities homed in one .rs file (by sub-path or basename, "
             "e.g. `--file oid.rs`).",
    )

    # -- analyze ---------------------------------------------------------
    analyze_p = sub.add_parser(
        "analyze",
        help=(
            "Run an analyze stage. A subject is required: "
            "'extract-ql', 'scope', 'symbols', 'types', or 'dag'."
        ),
    )
    analyze_p.add_argument(
        "--reset", action="store_true", dest="reset_stages",
        help="Delete matching entries before running so the pipeline "
             "regenerates them fresh.",
    )
    analyze_sub = analyze_p.add_subparsers(dest="subject", required=True)

    analyze_sub.add_parser(
        "extract-ql",
        help="Run the T1 (entities) + T2 (edges) .ql batches against the "
             "CodeQL database at crustify/codeql/db/ and write one CSV per "
             "query under crustify/codeql/{t1,t2}/. Composer-only, no LLM. "
             "The database is NOT created here — build the project under "
             "`codeql database create --language=cpp --command=...` "
             "yourself first. --reset wipes the existing t1/ and t2/ trees "
             "(the database is left alone).",
    )

    analyze_scope_p = analyze_sub.add_parser(
        "scope",
        help="Derive a section of scope.json (one required, mutually exclusive): "
             "--port-only (port set from config) or --wrap-only (wrap "
             "import-closure; composer-only, computed standalone from CodeQL "
             "T1/T2 + the port section).",
    )
    analyze_scope_sel = analyze_scope_p.add_mutually_exclusive_group(required=True)
    analyze_scope_sel.add_argument(
        "--port-only", action="store_true", dest="port_only",
        help="Derive the `port` section from config.json (the seed — run first).",
    )
    analyze_scope_sel.add_argument(
        "--wrap-only", action="store_true", dest="wrap_only",
        help="Derive the `wrap` import-closure section (appended alongside "
             "`port`; computed standalone from CodeQL T1/T2 + the `port` "
             "section — needs only `analyze scope --port-only` + "
             "`analyze extract-ql`).",
    )

    analyze_symbols_p = analyze_sub.add_parser(
        "symbols",
        help="Run the syms composer skeleton + symbol analyzer agent.",
    )
    _add_analyze_filter_flags(analyze_symbols_p)
    analyze_symbols_p.add_argument(
        "--lifetime-for", default=None, metavar="SPEC", dest="lifetime_for",
        help="LIFETIME-DISCOVERY mode: hand one agent the job of identifying "
             "SPEC's lifecycle primitives. There is no composed worklist -- the "
             "agent discovers candidates itself (`query symbols --taking SPEC "
             "--calling ... --hops N`), triages them to the routines that really "
             "drop/dispose/clone SPEC, and submits their arg-level lifetime "
             "flags; `query symbols --lifetime-for SPEC` then reverse-derives the "
             "type's roles. SPEC is a struct tag / typedef (its types.json entry "
             "is composed first), or the keyword `void` (raw byte-level) / "
             "`string` (NUL-terminated) -- the untyped tiers, which have no entry. "
             "Run the tiers in order: void -> string -> <tag>. Compose the symbol "
             "tree first (--all --unscoped --compose-only): scope gates emission, "
             "so an unemitted primitive is unfindable.")

    analyze_types_p = analyze_sub.add_parser(
        "types",
        help="Run the types composer skeleton + type analyzer agent.",
    )
    _add_analyze_filter_flags(analyze_types_p)
    # Cross-cutting synthesis passes, runnable standalone (each a single
    # whole-tree agent run). Without these, the full `analyze types`
    # runs the per-dir pass and then both synthesis passes.
    analyze_types_synth = analyze_types_p.add_mutually_exclusive_group()
    analyze_types_synth.add_argument(
        "--buffers", action="store_true",
        help="Run ONLY the buffer pass: synthesize string/array allocator "
             "clusters (requires alloc.json). Skips the per-dir struct pass.",
    )

    analyze_sub.add_parser(
        "dag",
        help="Emit deps-dag.json — unified types+symbols dependency DAG "
             "from the analysis tree (scope-agnostic).",
    )

    # -- scaffold (crates.json-driven .rs oracle) ------------------------
    scaffold_p = sub.add_parser(
        "scaffold",
        help=(
            "Resolve C symbols/types to their Rust .rs via crates.json. "
            "Looks up the placement oracle; on a miss spawns CrustifyScaffolder "
            "to fill it. `--all` fills the whole target; `--validate` runs the "
            "consistency gate."
        ),
    )
    # Selection is required and explicit — there is no default.
    scaffold_sel = scaffold_p.add_mutually_exclusive_group(required=True)
    scaffold_sel.add_argument(
        "--all", action="store_true",
        help="Scaffold the entire in-scope tree (every in-scope source file, "
             "both port and wrap scope).",
    )
    scaffold_sel.add_argument(
        "--dir", default=None, metavar="DIR",
        help="Scaffold the crate(s) + module tree for every in-scope source "
             "file found under DIR, a path relative to the target "
             "(e.g. `--dir .` for the target dir, `--dir ../util`).",
    )
    scaffold_sel.add_argument(
        "--file", default=None, metavar="FILE",
        help="Scaffold the crate + stub for the single in-scope source file "
             "at <target>/FILE. Matches the file's elements wherever their "
             "wrappers home (e.g. `--file odb.h` reaches git_odb even though "
             "its wrapper homes at include/git2/odb.h).",
    )
    scaffold_sel.add_argument(
        "--name", nargs="+", action="extend", default=None, metavar="NAME",
        help="Resolve the named entit(ies) — type tags and/or symbol names "
             "(e.g. `--name git_odb git_odb_read`) — to the .rs module(s) "
             "homing their `// Replaces:` (port) / `// Wraps:` (wrap) anchor. Query mode "
             "prints each homed path (or `not created`); add --create to write "
             "the stub(s). The authoritative way an agent locates a wrapper "
             "module or where a dep lives.",
    )
    scaffold_sel.add_argument(
        "--validate", action="store_true",
        help="Run the crates.json consistency gate (every entity homed in "
             "exactly one .rs; crate depends_on acyclic) and exit.",
    )
    scaffold_p.add_argument(
        "--create", action="store_true",
        help="Write the stub files + module tree for the selection. Without it, "
             "scaffold runs in QUERY mode: it prints the homed .rs path(s) of "
             "the selection (the authoritative way to locate a wrapper module / "
             "a dep's module), or a `not created` note for anything not yet on "
             "disk. Idempotent (stub files written only when absent).",
    )

    # -- bindgen (deterministic -sys FFI-crate composer) -----------------
    bindgen_p = sub.add_parser(
        "bindgen",
        help=(
            "Scaffold the <lib>-sys FFI crates from the analysis tree "
            "(deterministic; no LLM). Partitions the wrap-scope surface by "
            "owning crate (crates.json) into <target>/rust/crates/<lib>-sys/. "
            "The macro shims + cargo-check verify loop are the separate agent stage."
        ),
    )
    bindgen_p.add_argument(
        "--libs", nargs="+", default=None, metavar="LIB",
        help="Restrict to these libraries (e.g. libssl). "
             "Default: every in-scope library.",
    )
    bindgen_p.add_argument(
        "--scaffold-only", action="store_true",
        help="Run only the deterministic composer (skeletons + allowlists "
             "+ worklists); skip the macro/global shim agent stage.",
    )

    # -- query -----------------------------------------------------------
    query_p = sub.add_parser(
        "query",
        help=(
            "Read-only oracle. `types`/`symbols` enumerate (filtered, as a name "
            "list) or introspect one (--name) as the whole record. "
            "`files` lists the port / wrap scope file sets. "
            "`dag` does the graph walks (closure / layer / scc)."
        ),
    )
    query_sub = query_p.add_subparsers(dest="subject", required=True)
    _add_query_flags(
        query_sub.add_parser(
            "types", help="Types: enumerate, or introspect one (--name)."),
        facets=True)
    _add_query_flags(
        query_sub.add_parser(
            "symbols", aliases=["syms"],
            help="Symbols: enumerate, or introspect one (--name)."),
        facets=False)

    files_q = query_sub.add_parser(
        "files",
        help="Scope files: --port-only (port set) or --wrap-only (wrap closure).",
    )
    files_sel = files_q.add_mutually_exclusive_group()
    files_sel.add_argument(
        "--port-only", action="store_true", dest="port_only",
        help="Print the port-scope file set (scope.json.port).",
    )
    files_sel.add_argument(
        "--wrap-only", action="store_true", dest="wrap_only",
        help="Print the wrap closure — the import-header surface reached from "
             "port code via depends_on (cached scope.json.wrap, else computed).",
    )

    dag_q = query_sub.add_parser(
        "dag",
        help="Structural dag views: transitive deps of --name T/S (closure), "
             "all nodes at --layer N (slice), or --name X --scc hi-deps/lo-deps "
             "(flattened-cycle twins X may use naked / that used X naked).",
    )
    dag_q.add_argument(
        "--name", nargs="+", action="extend", default=None, metavar="NAME",
        help="Entity (type tag / symbol) to query (closure or --scc mode).",
    )
    dag_q.add_argument(
        "--layer", type=int, default=None, metavar="N",
        help="Slice mode: return every node (type + symbol) at layer N "
             "(mutually exclusive with --name).",
    )
    dag_q.add_argument(
        "--scc", choices=("hi-deps", "lo-deps"), default=None,
        help="With --name X: hi-deps = X's fallback (higher-layer cycle twins "
             "X may use naked); lo-deps = X's back_fill (lower-layer twins that "
             "used X naked).",
    )
    dag_q.add_argument(
        "--file", nargs="+", default=None, metavar="FILE", dest="files",
        help="Disambiguate a --name collision (pick the one defined here).",
    )
    dag_q.add_argument(
        "--depth", type=int, default=None, metavar="N",
        help="Closure mode: limit to N hops (1 = direct deps, 2 = deps of "
             "deps, …; default: full transitive closure).",
    )
    dag_q.add_argument(
        "--with-details", action="store_true", dest="with_details",
        help="Emit full records (kind, layer, depth) instead of bare names.",
    )
    dag_q.add_argument(
        "--loc", action="store_true", dest="loc",
        help="LoC view (with --name or --layer): a type seed → its struct "
             "field count + op count; a function seed → its body LoC; "
             "--layer N → the layer's total translated LoC (types valued as "
             "fields+ops; the bodies of folded type-ops are excluded — they "
             "ride their type at 1 each, not ported standalone).",
    )
    dag_scope = dag_q.add_mutually_exclusive_group()
    dag_scope.add_argument(
        "--wrap-only", action="store_true", dest="wrap_only",
        help="Restrict the node set (slice / --loc) to wrap-scope entities "
             "(scope.json wrap closure ∪ synthetic string/array clusters).",
    )
    dag_scope.add_argument(
        "--port-only", action="store_true", dest="port_only",
        help="Restrict the node set (slice / --loc) to port-scope entities "
             "(scope.json port closure; synthetics are never port).",
    )

    # -- wrap ------------------------------------------------------------
    wrap_p = sub.add_parser(
        "wrap",
        help=(
            "Wrap stage: emit Rust wrappers for the selected wrap-scope "
            "units (types AND free symbols) in dependency-layer order. "
            "Select with --name / --strings / --arrays. One unified "
            "scheduler dispatches each unit to its wrapper (type / string / "
            "array / symbol); no subject split. Requires the scaffold + "
            "bindgen stages to have run for each library being wrapped."
        ),
    )
    _add_wrap_filter_flags(wrap_p)



    # -- port ------------------------------------------------------------
    port_p = sub.add_parser(
        "port",
        help="Port stage: emit ported Rust via the --name scheduler.",
    )
    port_p.add_argument(
        "--name", nargs="+", action="extend", metavar="NAME", default=None,
        help="Select port-scope unit(s) by name. Pass all names as a "
             "space-separated list after a single --name (e.g. "
             "`--name T1 op2 op3`) to batch them; repeating the flag keeps "
             "only the last group. A type name brings its in-scope ops; free "
             "symbols / TU macros pool per file. The user supplies dependency "
             "order.",
    )
    port_p.add_argument(
        "--file", nargs="+", metavar="FILE", default=None, dest="files",
        help="Restrict the selection to entities defined in these files "
             "(disambiguates a --name collision).",
    )
    port_p.add_argument(
        "--max-syms", type=int, default=None, metavar="N", dest="max_syms",
        help="Per-batch symbol-count budget. Default: config.PORT_MAX_SYMS.",
    )
    port_p.add_argument(
        "--max-loc", type=int, default=None, metavar="N", dest="max_loc",
        help="Per-batch lines-of-code budget (Σ body span; global=1, macro=0), "
             "binds together with --max-syms. Default: config.PORT_MAX_LOC.",
    )
    port_p.add_argument(
        "--dag-layer", type=int, default=None, metavar="N", dest="dag_layer",
        help="Select every port-scope symbol at dag layer N (lifecycle ops that "
             "fold into a type are excluded). Combines with --name; e2e driver mode.",
    )
    port_p.add_argument(
        "--skip", nargs="+", action="extend", default=None, metavar="NAME",
        help="Blocklist: drop these names from the selection (manual already-done "
             "list; the driver seeds it, crustify just honours it).",
    )
    port_p.add_argument(
        "--parallel-max", type=int, default=8, metavar="N", dest="parallel_max",
        help="Max concurrent agents across disjoint files (with --parallel).",
    )
    port_p.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the dependency-confirmation prompt.",
    )
    port_p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print the plan (units, batches, first-layer deps) and stop.",
    )

    args = parser.parse_args()

    # repo_root is explicit (no marker-walking); target is repo-relative.
    from crustify.layout import set_repo_root
    repo_root = Path(args.repo_root).resolve()
    set_repo_root(repo_root)
    target_rel = (args.target or "").strip("/")
    target = repo_root if target_rel in ("", ".") else (repo_root / target_rel)
    target = target.resolve()
    args._target_path = str(target)

    if not repo_root.exists():
        print(f"error: repo_root does not exist: {repo_root}", file=sys.stderr)
        sys.exit(1)
    if not (repo_root / "crustify").is_dir():
        print(f"error: no crustify/ under repo_root: {repo_root}", file=sys.stderr)
        sys.exit(1)
    if not target.exists():
        print(f"error: target does not exist: {target}", file=sys.stderr)
        sys.exit(1)

    # -- Apply logging flags ----------------------------------------------
    from crustify import config as crustify_config

    if args.no_console:
        crustify_config.LOG_TO_CONSOLE = False
    if args.no_file_log:
        crustify_config.LOG_TO_FILE = False
    if getattr(args, "model", None):
        crustify_config.MODEL_OVERRIDE = args.model
    if getattr(args, "backend", None):
        crustify_config.BACKEND = args.backend
    if getattr(args, "billing", None):
        crustify_config.BILLING = args.billing
    if getattr(args, "override_base_prompt", None) is not None:
        crustify_config.OVERRIDE_BASE_PROMPT = args.override_base_prompt

    if args.command == "audit":
        from crustify.audit import audit as _audit
        _audit(target, all=getattr(args, "all", False),
               names=getattr(args, "name", None),
               crate=getattr(args, "crate", None), mod=getattr(args, "mod", None),
               dir=getattr(args, "dir", None), file=getattr(args, "file", None))

    elif args.command == "alloc":
        from crustify.agents.analyzer import CrustifyAllocAnalyzer
        CrustifyAllocAnalyzer(target).run()

    elif args.command == "analyze":
        _handle_analyze(args, target)

    elif args.command == "scaffold":
        _handle_scaffold(args, target)

    elif args.command == "bindgen":
        _handle_bindgen(args, target)

    elif args.command == "query":
        _handle_query(args, target)

    elif args.command == "wrap":
        _handle_wrap(args, target)

    elif args.command == "port":
        _handle_port(args, target)


# -- analyze dispatch -----------------------------------------------------

def _handle_analyze(args: argparse.Namespace, target: Path) -> None:
    """Dispatch analyze to the per-subject handler.

    The subject-level ``--all`` (``args.all``) on symbols/types processes
    every entry in the analysis tree for that subject.
    """
    from crustify import analyze as analyze_mod

    subject = args.subject
    reset_stages = getattr(args, "reset_stages", False)

    if subject == "extract-ql":
        if reset_stages:
            analyze_mod.reset_extract_ql(target)
        analyze_mod.analyze_extract_ql(target)
        return

    if subject == "scope":
        port_only = bool(getattr(args, "port_only", False))
        # --reset wipes the port seed; meaningful only for --port-only. The
        # wrap section is a derived augmentation that recomputes in place.
        if reset_stages and port_only:
            analyze_mod.reset_scope(target)
        analyze_mod.analyze_scope(
            target,
            port_only=port_only,
            wrap_only=bool(getattr(args, "wrap_only", False)),
        )
        return


    if subject == "dag":
        if reset_stages:
            analyze_mod.reset_dag(target)
        analyze_mod.analyze_dag(target)
        return

    # symbols / types — these are the agent-bearing subjects with full
    # narrowing flag support.
    _validate_narrowing(args)

    # --out-suffix isolates this run onto types_<suffix>.json / syms_<suffix>.json.
    # Export it so the composer emit, the agents' `crustify query` subprocesses
    # (env-inherited; no path is pushed to them), and reset all resolve the same
    # suffixed file. Validate as a filename-safe token.
    out_suffix = getattr(args, "out_suffix", None)
    if out_suffix:
        import os
        import re
        from crustify.layout import OUT_SUFFIX_ENV
        if not re.fullmatch(r"[A-Za-z0-9._-]+", out_suffix):
            print("error: --out-suffix must match [A-Za-z0-9._-]+",
                  file=sys.stderr)
            sys.exit(2)
        os.environ[OUT_SUFFIX_ENV] = out_suffix

    # Resolve --file basenames to repo-rel paths before building the
    # selection / filter (so composer, agent, and reset all see the
    # resolved paths).
    repo_root = analyze_mod._repo_root_for(target)
    resolved_files = _resolve_file_args(args, repo_root)
    filter_spec = _build_filter_spec(args, resolved_files)

    # Agent selection: in seed mode, the composer has already focused
    # the analysis tree to {seeds, closure}; the agent should process
    # every entry the composer emitted, not just entries matching the
    # original CLI flags (closure entries don't match --name/--dir
    # but still need annotation). Pass `all <subject>` in seed mode;
    # in filter mode, the agent's selection resolution maps cleanly
    # The agent's input is now the manifests-list contract built
    # downstream by the orchestrator from `filter_spec` + composer
    # output. The CLI no longer builds a `selection` grammar string;
    # the composer's narrowing IS the contract.

    parallel = bool(getattr(args, "parallel", False))
    parallel_max = int(getattr(args, "parallel_max", 8))

    if subject == "symbols":
        lifetime_for = getattr(args, "lifetime_for", None)
        if lifetime_for:
            analyze_mod.analyze_lifetime_for(
                target, lifetime_for,
                compose_only=getattr(args, "compose_only", False),
            )
            return
        if getattr(args, "reset", False):
            analyze_mod.reset_syms(
                target,
                all_entries=getattr(args, "all", False),
                dirs=getattr(args, "dir", None),
                files=resolved_files,
                names=getattr(args, "name", None),
            )
        analyze_mod.analyze_symbols(
            target, filter_spec=filter_spec,
            parallel=parallel, parallel_max=parallel_max,
            compose_only=getattr(args, "compose_only", False),
        )
        return

    if subject == "types":
        # Standalone cross-cutting synthesis passes.
        if getattr(args, "buffers", False):
            analyze_mod.run_buffer_pass(target)
            return
        if getattr(args, "reset", False):
            analyze_mod.reset_types(
                target,
                all_entries=getattr(args, "all", False),
                dirs=getattr(args, "dir", None),
                files=resolved_files,
                names=getattr(args, "name", None),
            )
        analyze_mod.analyze_types(
            target, filter_spec=filter_spec,
            parallel=parallel, parallel_max=parallel_max,
            compose_only=getattr(args, "compose_only", False),
        )
        return

    print(f"error: unknown analyze subject {subject!r}", file=sys.stderr)
    sys.exit(2)


def _validate_narrowing(args: argparse.Namespace) -> None:
    """Enforce mutual exclusivity of --all vs seed selectors.

    --all is exclusive with --dir / --file / --name.
    --port-only / --wrap-only may combine with either --all or seed
    selectors — they're orthogonal post-emission filters.
    """
    # The standalone cross-cutting buffer pass (types --buffers) runs over the
    # whole tree and takes no narrowing selection.
    if getattr(args, "buffers", False):
        return
    # `symbols --lifetime-for SPEC` IS its own selector: the SPEC names the
    # discovery target and the agent finds the worklist itself, so the seed
    # flags don't apply.
    if getattr(args, "lifetime_for", None):
        return
    want_all = bool(getattr(args, "all", False))
    seed = (
        bool(getattr(args, "dir", None))
        or bool(getattr(args, "file", None))
        or bool(getattr(args, "name", None))
    )
    if want_all and seed:
        print(
            "error: --all is mutually exclusive with "
            "--dir / --file / --name",
            file=sys.stderr,
        )
        sys.exit(2)
    if not want_all and not seed:
        print(
            "error: specify --all or at least one of "
            "--dir / --file / --name",
            file=sys.stderr,
        )
        sys.exit(2)


def _resolve_file_arg(file_arg: str, repo_root: Path) -> str:
    """Resolve a `--file` argument to a repo-root-relative path.

    - Arg containing `/` → treat as already repo-root-relative.
    - Bare basename → search the repo-root analysis tree for a unique
      `defined_in` or `declared_in[0]` matching the basename. If
      unique, return that repo-relative path. If ambiguous or
      missing, error.
    """
    import json
    if "/" in file_arg:
        return file_arg

    from crustify.layout import Layout
    analysis_root = Layout(repo_root).analysis
    if not analysis_root.is_dir():
        print(
            f"error: --file basename resolution requires the analysis "
            f"tree at {analysis_root} (run `analyze symbols/types` first "
            f"so the composer can populate it).",
            file=sys.stderr,
        )
        sys.exit(2)

    matches: set[str] = set()
    for p in analysis_root.rglob("syms.json"):
        for e in json.loads(p.read_text()).get("symbols", []):
            df = e.get("defined_in") or ""
            if df and Path(df).name == file_arg:
                matches.add(df)
            for dh in e.get("declared_in") or []:
                if dh and Path(dh).name == file_arg:
                    matches.add(dh)
    for p in analysis_root.rglob("types.json"):
        for e in json.loads(p.read_text()).get("types", []):
            df = e.get("defined_in") or ""
            if df and Path(df).name == file_arg:
                matches.add(df)
            dh = e.get("declared_in")
            if dh and Path(dh).name == file_arg:
                matches.add(dh)

    if not matches:
        print(
            f"error: --file {file_arg!r}: no entry in the analysis tree "
            f"has a defined_in/declared_in matching that basename.",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(matches) > 1:
        print(
            f"error: --file {file_arg!r} is ambiguous; matches "
            f"{sorted(matches)}. Specify the full repo-root-relative "
            f"path.",
            file=sys.stderr,
        )
        sys.exit(2)
    return next(iter(matches))


def _resolve_file_args(
    args: argparse.Namespace, repo_root: Path,
) -> list[str] | None:
    """Resolve every `--file` arg via `_resolve_file_arg`."""
    files = getattr(args, "file", None)
    if not files:
        return None
    return [_resolve_file_arg(f, repo_root) for f in files]


def _build_filter_spec(
    args: argparse.Namespace,
    resolved_files: list[str] | None,
):
    """Build a `FilterSpec` from the CLI narrowing flags.

    `resolved_files` is the basename-resolved file list (see
    `_resolve_file_args`); we pass it in rather than re-resolving from
    `args.file` so the composer / agent / reset all share the same
    canonical paths.

    Always returns a `FilterSpec` (never ``None``) so the composer
    can inspect `scope_json_path` / `port_only` / `wrap_only` even
    on `--all` invocations.
    """
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent.parent.parent
    _compose_parent = _root / "utils" / "codeql"
    if str(_compose_parent) not in _sys.path:
        _sys.path.insert(0, str(_compose_parent))
    from compose.filter_spec import FilterSpec

    # Scope is the target-tier `crustify/targets/<target>/scope.json`,
    # computed automatically from the target (alongside its config.json) —
    # there is no flag. Absent scope.json → scope-blind (base-shape) emit.
    scope_arg = None
    target = getattr(args, "_target_path", None) or args.repo_root
    from crustify.layout import Layout
    try:
        default_scope = Layout.discover(_Path(target)).scope(_Path(target))
    except SystemExit:
        default_scope = None
    if default_scope is not None and default_scope.exists():
        scope_arg = default_scope

    return FilterSpec(
        dirs=(
            [] if getattr(args, "all", False)
            else list(getattr(args, "dir", None) or [])
        ),
        files=(
            [] if getattr(args, "all", False)
            else list(resolved_files or [])
        ),
        names=(
            [] if getattr(args, "all", False)
            else list(getattr(args, "name", None) or [])
        ),
        scope_json_path=scope_arg if scope_arg else None,
        port_only=bool(getattr(args, "port_only", False)),
        wrap_only=bool(getattr(args, "wrap_only", False)),
        unscoped=bool(getattr(args, "unscoped", False)),
    )



# -- port dispatch --------------------------------------------------------

def _handle_port(args: argparse.Namespace, target: Path) -> None:
    from crustify.port import port
    port(
        target,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        dag_layer=getattr(args, "dag_layer", None),
        skip=getattr(args, "skip", None),
        parallel=bool(getattr(args, "parallel", False)),
        parallel_max=int(getattr(args, "parallel_max", 8)),
        max_syms=getattr(args, "max_syms", None),
        max_loc=getattr(args, "max_loc", None),
        yes=bool(getattr(args, "yes", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


# -- scaffold dispatch ----------------------------------------------------

# -- scaffold dispatch (deterministic crate-skeleton composer) -----------

def _handle_scaffold(args: argparse.Namespace, target: Path) -> None:
    from crustify.scaffold import scaffold
    scaffold(target, all=args.all, dir=args.dir, file=args.file,
             name=getattr(args, "name", None),
             create=getattr(args, "create", False),
             validate=getattr(args, "validate", False))


# -- bindgen dispatch (deterministic -sys FFI-crate composer) ------------

def _handle_bindgen(args: argparse.Namespace, target: Path) -> None:
    from crustify.bindgen import bindgen
    bindgen(target, libs=args.libs, scaffold_only=args.scaffold_only)


# -- query dispatch -------------------------------------------------------

def _handle_query(args: argparse.Namespace, target: Path) -> None:
    if args.subject == "files":
        from crustify.query import query_files
        query_files(
            target,
            port_only=bool(getattr(args, "port_only", False)),
            wrap_only=bool(getattr(args, "wrap_only", False)),
        )
        return
    if args.subject == "dag":
        from crustify.query import query_dag
        query_dag(
            target,
            names=getattr(args, "name", None),
            files=getattr(args, "files", None),
            depth=getattr(args, "depth", None),
            scc=getattr(args, "scc", None),
            layer=getattr(args, "layer", None),
            as_json=bool(getattr(args, "with_details", False)),
            loc=bool(getattr(args, "loc", False)),
            wrap_only=bool(getattr(args, "wrap_only", False)),
            port_only=bool(getattr(args, "port_only", False)),
        )
        return
    from crustify.query import query
    # `syms` is a back-compat alias for the `symbols` subject.
    subject = "symbols" if args.subject == "syms" else args.subject
    query(
        target,
        subject=subject,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        wrap_only=bool(getattr(args, "wrap_only", False)),
        port_only=bool(getattr(args, "port_only", False)),
        strings=bool(getattr(args, "strings", False)),
        arrays=bool(getattr(args, "arrays", False)),
        typegens=bool(getattr(args, "typegens", False)),
        fields=bool(getattr(args, "fields", False)),
        ops=bool(getattr(args, "ops", False)),
        methods=bool(getattr(args, "methods", False)),
        field_touchers=bool(getattr(args, "field_touchers", False)),
        update=getattr(args, "update", None),
        update_help=bool(getattr(args, "update_help", False)),
        schema=bool(getattr(args, "schema", False)),
        create=getattr(args, "create", None),
        manifest=bool(getattr(args, "manifest", False)),
        rng=getattr(args, "rng", None),
        lifetime_for=getattr(args, "lifetime_for", None),
        taking=getattr(args, "taking", None),
        calling=getattr(args, "calling", None),
        hops=int(getattr(args, "hops", 1) or 1),
        array=bool(getattr(args, "array", False)),
    )


# -- wrap dispatch --------------------------------------------------------

def _handle_wrap(args: argparse.Namespace, target: Path) -> None:
    """Run the unified wrap stage over the --name / --strings / --arrays
    selection. One scheduler emits type, string, array, and free-symbol
    wrappers alike — no subject split (the scheduler routes each unit to its
    wrapper prompt by kind)."""
    from crustify.wrap import wrap_types
    wrap_types(
        target,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        wrap_only=bool(getattr(args, "wrap_only", False)),
        port_only=bool(getattr(args, "port_only", False)),
        strings=bool(getattr(args, "strings", False)),
        arrays=bool(getattr(args, "arrays", False)),
        dag_layer=getattr(args, "dag_layer", None),
        skip=getattr(args, "skip", None),
        parallel=bool(getattr(args, "parallel", False)),
        parallel_max=int(getattr(args, "parallel_max", 8)),
        max_fields=getattr(args, "max_fields", None),
        max_syms=getattr(args, "max_syms", None),
        yes=bool(getattr(args, "yes", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


# -- helpers --------------------------------------------------------------

def _scope_from_args(args: argparse.Namespace) -> str | None:
    """Resolve the scope flag to ``"port"``, ``"wrap"``, or ``None`` (both)."""
    if getattr(args, "port", False):
        return "port"
    if getattr(args, "wrap", False):
        return "wrap"
    return None


def _check_libraries_requires_wrap(args: argparse.Namespace) -> None:
    """``--libraries`` is only meaningful with ``--wrap`` (or implicit
    wrap context). For ``analyze *`` subjects we surface this as an
    early error rather than letting the agent get confused."""
    libs = getattr(args, "libraries", None)
    if libs and not getattr(args, "wrap", False):
        # Allow it without an explicit --wrap only when no scope group
        # exists (wrap subjects' include_scope=False), so distinguish:
        # if the args namespace HAS a `port` attribute, the scope group
        # exists and --libraries needs --wrap to be set.
        if hasattr(args, "port"):
            print(
                "error: --libraries requires --wrap (port-scope entries "
                "are not library-tagged in the manifests).",
                file=sys.stderr,
            )
            sys.exit(2)


def _require_artifact(target: Path, filename: str, command: str) -> None:
    """Data-driven pipeline gate: refuse to run if a required upstream
    artifact is missing on disk (stage completion is the presence of its
    on-disk artifact, not a separate marker).
    """
    from crustify.layout import Layout
    if not (Layout.discover(target).root / filename).exists():
        print(
            f"error: missing prerequisite artifact 'crustify/{filename}'.\n"
            f"       Run: crustify {target} {command}",
            file=sys.stderr,
        )
        sys.exit(1)
