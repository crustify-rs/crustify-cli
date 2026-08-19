"""oracle.py — `crustify-oracle`, the read side of the pipeline.

Everything that ANSWERS questions about the C, split from everything that
CHANGES the Rust. `crustify-cli` keeps the stages that write — scaffold,
bindgen, wrap, audit; the oracle keeps extraction and query.

    crustify-oracle <repo_root> <target> extract-ql
    crustify-oracle <repo_root> <target> query types   --name X [...]
    crustify-oracle <repo_root> <target> query symbols --name X [...]
    crustify-oracle <repo_root> <target> query dag     [...]
    crustify-oracle <repo_root> <target> query files   [...]

There is no verb to build the derived artifacts. `scope.json` and
`deps-dag.json` materialize on first use and refresh themselves when their
fingerprint stops matching (:mod:`crustify.cache`); the type and symbol
records are composed per call. The only thing that must be run explicitly is
`extract-ql`, because it drives CodeQL and takes minutes.

"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crustify-oracle",
        description="Read-only oracle over a C codebase: extraction and query.",
    )
    p.add_argument(
        "repo_root",
        help="Full path to the repository root (its artifacts live under "
             "<repo_root>/crustify/). Explicit — crustify never walks the "
             "filesystem to find it.",
    )
    p.add_argument(
        "target",
        help="Repo-relative target subdirectory the oracle is scoped to "
             "(e.g. ssl/statem), matching its crustify/targets/<target>/ "
             "scope-config.json. Use . for the repo root.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "extract-ql",
        help="Run the T1 (entities) + T2 (edges) .ql batches against the "
             "CodeQL database at crustify/codeql/db/ and write one CSV per "
             "query under crustify/codeql/{t1,t2}/. The database is NOT "
             "created here — build the project under `codeql database create` "
             "yourself first. The one oracle command with side effects, and "
             "the only one that must be run explicitly: everything else "
             "derives from these tables on demand. Takes minutes — run it only "
             "when the extraction is genuinely stale.",
    )
    _add_query_command(sub)
    return p


def _add_query_flags(p: argparse.ArgumentParser, *, facets: bool) -> None:
    """Flags for `query types`/`query syms` — the read-only oracle, resolved
    from the manifest (dag-free). With no `--name` they enumerate (filtered by
    scope / `--file`) as a name list; with `--name T` they
    introspect one entry — always the WHOLE record (several names → several
    records). On a type, `--fields`/`--lifecycle-ops` print its windowable lists
    (`facets`). The .rs module of an entry is found via
    `crustify-cli <target> scaffold --name <X>`, not here."""
    sc = p.add_mutually_exclusive_group()
    sc.add_argument("--imported-only", action="store_true", dest="imported_only",
                    help="Narrow to the IMPORTED section — this campaign's "
                         "EXTERNAL dependencies: everything the targeted set "
                         "reaches but does not own. Always the derived closure, "
                         "under either campaign objective. Enumeration → "
                         "imported entries; --lifecycle-ops/--users → imported "
                         "functions; --fields/--field-touchers → fields touched "
                         "by imported code. (Facets are complete by default.)")
    sc.add_argument("--targeted-only", action="store_true", dest="targeted_only",
                    help="Narrow to the TARGETED section — the library this "
                         "campaign OWNS: what `scope-config.json`'s "
                         "`impl_files` + `api_headers` name, DEFINITION-anchored. "
                         "Identical under either `campaign_objective` — a `wrap` "
                         "campaign owns its library too, it merely intends "
                         "something different with it. Enumeration → targeted "
                         "entries; --lifecycle-ops/--users → targeted functions; "
                         "--fields/--field-touchers → fields touched by targeted "
                         "code. (Facets are complete by default.) Says what the "
                         "campaign COVERS, not what one wave does with it — that "
                         "is `translate --objective`.")
    # NOT in the `sc` exclusive group: `api` is an AXIS, not a section. It cuts
    # PUBLICATION (does a named header declare it) where targeted/imported cut
    # OWNERSHIP (whose body is it), so the two compose. `--api-only
    # --imported-only` is the re-export query — published by this library,
    # owned by another — and refusing it would lose the one fact that
    # distinguishes a re-export from an ordinary import.
    p.add_argument("--api-only", action="store_true", dest="api_only",
                   help="Narrow to the API view — what `scope-config.json`'s "
                        "`api_headers` PUBLISHES, selected on DECLARATION "
                        "sites (a public header publishes what it declares; "
                        "the bodies live in the .c files behind it). This is "
                        "the set a `wrap` campaign translates. INTERSECTS with "
                        "--targeted-only / --imported-only rather than "
                        "excluding them: `--api-only --targeted-only` is the "
                        "public surface this campaign also owns, and "
                        "`--api-only --imported-only` is the re-export set.")
    og = p.add_mutually_exclusive_group()
    og.add_argument("--out-of-tree", action="store_true", dest="out_of_tree",
                    help="Enumeration only. Keep entries whose home is OUTSIDE the "
                         "repository (system / toolchain headers). Combines with the "
                         "scope flags: `--imported-only --out-of-tree` is the permanent FFI "
                         "floor — code that can never move into the targeted section.")
    og.add_argument("--in-tree", action="store_true", dest="in_tree",
                    help="Enumeration only. Keep entries whose home is INSIDE the "
                         "repository. `--imported-only --in-tree` is first-party code "
                         "wrapped only because this target does not port it — the "
                         "remaining port backlog.")
    p.add_argument("--name", nargs="+", action="extend", default=None, metavar="NAME",
                   help="No --name → enumerate; one → introspect; several → batch records.")
    p.add_argument("--file", nargs="+", default=None, metavar="FILE",
                   dest="files",
                   help="Restrict/disambiguate by defining file.")
    facet = p.add_mutually_exclusive_group()
    facet.add_argument("--manifest", action="store_true",
                       help="Introspect: print the types.json/syms.json that homes this entry.")
    # `--update` is available for BOTH subjects (types AND syms): the schema
    # boundary through which a wrapper agent merges its findings.
    facet.add_argument("--update", default=None, metavar="FINDINGS",
                       help="Ingest an agent findings JSON (path or '-' for stdin) "
                            "into the named entry: validate (hard-reject only), then "
                            "partial-merge under a lock. types: lifecycle + per-field "
                            "ptr. syms: macro kind, per-arg/return ownership "
                            "(ptr_args/ptr_ret), and the symbol's lifecycle role "
                            "(lifetime). The agent never edits the manifest directly.")
    facet.add_argument("--update-help", action="store_true", dest="update_help",
                       help="Print the findings JSON schema that --update expects "
                            "for this subject (types vs syms), then exit. No --name "
                            "needed — schema discovery for the wrapper agent.")
    facet.add_argument("--schema", action="store_true", dest="schema",
                       help="Print the record's field/slot DEFINITIONS (the "
                            "_comment_* blocks, the schema authority), then exit. "
                            "No --name needed.")
    if facets:
        facet.add_argument("--fields", action="store_true",
                           help="Introspect a type: ALL declared fields with their "
                                "per-field structural + ptr detail; --targeted-only "
                                "narrows to the fields THIS CAMPAIGN's targeted code reaches "
                                "(--imported-only does not apply here); "
                                "'[]' if none.")
        facet.add_argument("--lifecycle-ops", action="store_true",
                           dest="lifecycle_ops",
                           help="Introspect a type: its LIFECYCLE surface only — "
                                "the droppers, field-disposers and cloners, "
                                "reverse-derived from the symbols whose `lifetime` "
                                "block acts on an arg of this type, ordered "
                                "lifecycle-first then alphabetically. This is the "
                                "canonical windowable list the translate scheduler "
                                "co-emits with the type. A strict subset of --users.")
        facet.add_argument("--users", action="store_true",
                           help="Introspect a type: its COMPLETE footprint — the "
                                "opaque_in ∪ non_opaque_in functions (every function "
                                "tree-wide that USES the type, as a handle or through "
                                "a field, incl. out-of-scope); "
                                "--targeted-only/--imported-only intersect with that section's "
                                "functions; '[]' if none. Wider than --lifecycle-ops, "
                                "and unordered.")
        facet.add_argument("--field-touchers", action="store_true",
                           dest="field_touchers",
                           help="Introspect a type: {field: [touchers]} — ALL "
                                "declared fields by default; --targeted-only "
                                "narrows the FIELDS to the subset this target's "
                                "code reaches; --imported-only does not apply. "
                                "each field's toucher set is the COMPLETE, unfiltered "
                                "set of functions that access it.")
    else:
        # Symbols-only REVERSE lifecycle lookup, parameterized by a TYPE (no
        # --name). Feeds the type WRAPPER: which symbols realize TYPE's Drop /
        # dispose / Clone, read off each symbol's entry-level lifetime block.
        facet.add_argument(
            "--lifetime-for", default=None, metavar="SPEC", dest="lifetime_for",
            help="Reverse lifecycle lookup (READ: roles that already exist): "
                 "every symbol whose `lifetime` block (is_dropper/is_disposer/"
                 "is_cloner) acts on an arg matching SPEC, grouped into the "
                 "type's dropped_by / fields_disposed_by / cloned_by. SPEC is "
                 "a struct tag / typedef, or the keyword `void` (raw byte-level, "
                 "untyped) or `string` (NUL-terminated; the char family or the "
                 "wrapper's own ptr.string verdict). The subject arg is the one "
                 "named by `lifetime.for`, so a symbol that merely TAKES a SPEC "
                 "arg without acting on it is not listed. No --name needed.")
        facet.add_argument(
            "--taking", default=None, metavar="SPEC", dest="taking",
            help="CANDIDATE discovery (the inverse of --lifetime-for, which reads "
                 "flags that already exist): every symbol with an ARG matching "
                 "SPEC (tag / typedef / `void` / `string`). Pair with --calling "
                 "to keep only those that reach a lifecycle primitive. No --name "
                 "needed.")
        # The raw use-graph closure around a symbol. Distinct from `query dag
        # --name F --depth N`, which walks the ORDERING graph: scope-narrowed
        # (an imported symbol contributes no callees at all) and carrying the
        # layering. These two walk the graph the C actually wrote.
        facet.add_argument(
            "--callees", action="store_true", dest="callees",
            help="Introspect a symbol: what it reaches, out to --depth hops "
                 "(default 1 = direct). The raw use graph from the composer's "
                 "depends_on.syms — codebase-wide, NOT narrowed by scope, so it "
                 "answers on a `wrap` campaign where `query dag` deliberately "
                 "shows nothing. Nodes come back as {name, defined_in}: the walk "
                 "is keyed on that pair, so same-named file-local statics never "
                 "merge. Needs --name; --file picks one of several.")
        facet.add_argument(
            "--callers", action="store_true", dest="callers",
            help="Introspect a symbol: what reaches IT, out to --depth hops — "
                 "the inverse of --callees over the same index, same output "
                 "shape. Needs --name.")
        p.add_argument(
            "--calling", default=None, metavar="FN[,FN...]", dest="calling",
            help="Narrow --taking to symbols that reach one of these routines "
                 "within --depth hops (via the composer's depends_on.syms). "
                 "A dropper/cloner must ultimately reach a raw primitive -- but "
                 "the top-level one often does so through a helper, so >1 hop is "
                 "the norm (e.g. ASN1_STRING_free -> "
                 "ossl_asn1_string_free_internal -> CRYPTO_free is 2 hops). "
                 "Matches on NAME (a caller cannot know which file a helper was "
                 "defined in); each hit is reported with the file it resolved to.")
        p.add_argument(
            "--depth", type=int, default=1, metavar="N", dest="depth",
            help="Hop depth for --callees / --callers / --calling (default 1 = "
                 "direct edges only). Cycles are safe -- the walk is an "
                 "iterative BFS over a visited set, so a recursive cluster "
                 "terminates like anything else. NOT capped: every function "
                 "transitively reaches malloc, so depth is a precision/recall "
                 "trade the caller owns, and what a large one costs is output.")
        p.add_argument(
            "--array", action="store_true", dest="array",
            help="With --lifetime-for/--taking: keep only args whose ptr carries "
                 "an `array` shape (a buffer, not a lone pointee). Only "
                 "meaningful on an analyzed record.")


def _add_query_command(sub) -> None:
    """Attach the `query` verb and its subjects."""
    query_p = sub.add_parser(
        "query",
        help=(
            "Read-only oracle. `types`/`symbols` enumerate (filtered, as a name "
            "list) or introspect one (--name) as the whole record. "
            "`files` lists the targeted / imported section file sets. "
            "`dag` does the graph walks (closure / layer / scc)."
        ),
    )
    query_sub = query_p.add_subparsers(dest="subject", required=True)
    # The semantics below are NOT derivable from the flag list, and each one is
    # a mistake an agent otherwise makes: reading `lifetime: null` as "no role",
    # guessing at an ambiguous tag, and assuming scope narrows a lookup or an
    # emitted record's edges. Stated here so `--help` is the single authority.
    _SEMANTICS = (
        " SEMANTICS. `_analysis` on every record: `submitted` says whether the "
        "ownership store holds anything for this entity — `lifetime: null` "
        "alone cannot distinguish 'nobody looked' from 'an agent found no "
        "lifecycle role'; `pending` lists the pointer slots with no ownership "
        "block, counted per section under --targeted-only/--imported-only. "
        "A type carries no lifecycle of its own: dropped_by / "
        "fields_disposed_by / cloned_by are reverse-derived from the acting "
        "symbols' `lifetime` blocks. "
        "--file disambiguates and is REQUIRED when a name is ambiguous — a tag "
        "naming two unrelated structs both in this target's scope is refused, "
        "with the --file for each candidate printed; a name shared with a type "
        "OUTSIDE the scope is not ambiguous and resolves without it. "
        "Scope gates enumeration, not lookup: a listing is this target's "
        "inventory, but --name reaches every entity the extraction saw, so a "
        "type's destructor in another scope is readable, submittable through "
        "--update, and comes back from --lifetime-for. Scope gates emission, "
        "not content: an emitted record's depends_on / used_by are "
        "codebase-wide whatever its scope, so an imported node's "
        "depends_on.syms is populated and safe to walk. "
        "Every graph walk here (--callees / --callers / --calling) is keyed on "
        "(name, defined_in), never on the bare name, and reports both halves: "
        "same-named file-local statics are distinct nodes, and merging them "
        "would step between unrelated functions at every hop. Submit through "
        "--update, never by editing a file."
    )
    _add_query_flags(
        query_sub.add_parser(
            "types", help="Types: enumerate, or introspect one (--name).",
            description=_add_query_flags.__doc__ + _SEMANTICS),
        facets=True)
    _add_query_flags(
        query_sub.add_parser(
            "symbols",
            help="Symbols: enumerate, or introspect one (--name).",
            description=_add_query_flags.__doc__ + _SEMANTICS),
        facets=False)

    files_q = query_sub.add_parser(
        "files",
        help="Scope files: --targeted-only (what the campaign owns) or "
             "--imported-only (the derived / seeded closure).",
    )
    files_sel = files_q.add_mutually_exclusive_group()
    files_sel.add_argument(
        "--targeted-only", action="store_true", dest="targeted_only",
        help="Print what the campaign owns (scope.json.targeted.files). "
             "Empty on a `wrap` campaign.",
    )
    files_q.add_argument(
        "--api-only", action="store_true", dest="api_only",
        help="Print the API header set (scope.json.api.files) — the headers "
             "that publish the library, T1-anchored.",
    )
    files_sel.add_argument(
        "--imported-only", action="store_true", dest="imported_only",
        help="Print the imported closure — the header surface the campaign "
             "reaches through depends_on, or, on a `wrap` campaign, the surface "
             "seeded off `api_headers` (scope.json.imported.files).",
    )

    dag_q = query_sub.add_parser(
        "dag",
        help="Structural dag views: transitive deps of --name T/S (closure), "
             "all nodes at --layer N (slice), or --name X --scc hi-deps/lo-deps "
             "(flattened-cycle twins X may use naked / that used X naked).",
        description=(
            "Structural dag views: transitive deps of --name T/S (closure), all "
            "nodes at --layer N (slice), or --name X --scc hi-deps/lo-deps "
            "(flattened-cycle twins X may use naked / that used X naked). "
            "OUTPUT: JSON grouped by kind — types / callbacks / functions / "
            "globals / macros, each {id, layer, defined_in} plus depth in "
            "closure mode; empty groups are omitted. The groups route the work: "
            "types to the type wrapper, callbacks and functions to the symbol "
            "wrapper, macros to nobody (bindgen owns their -sys shims). `layer` "
            "and `depth` exist only here — no other subject reports them."
        ),
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
        "--loc", action="store_true", dest="loc",
        help="LoC view (with --name or --layer): a type seed → its struct "
             "field count + op count; a function seed → its body LoC; "
             "--layer N → the layer's total translated LoC (types valued as "
             "fields+ops; the bodies of folded type-ops are excluded — they "
             "ride their type at 1 each, not ported standalone).",
    )
    dag_q.add_argument(
        "--full", action="store_true", dest="full",
        help="BODY-DEEP graph: recompose scope with `campaign_objective` "
             "forced to `port`, so every symbol homed in `impl_files` / "
             "`api_headers` contributes its body's callees and every struct "
             "defined there contributes every field; only genuinely IMPORTED "
             "items stay narrowed (signature-only symbols, "
             "touched-fields-only structs). This is how a `wrap` campaign "
             "reads the graph it would have if it owned the implementation — "
             "by default a wrap closure stops at the opaque handle, which is "
             "right for wrapping and useless for sizing a port. No-op on a "
             "`port` campaign. Caches to deps-dag.full.json, beside the "
             "default graph, so alternating between the two costs one compose "
             "each and not one per call.",
    )
    dag_q.add_argument(
        "--api-only", action="store_true", dest="api_only",
        help="Restrict the node set (slice / --loc) to the API view — what "
             "`api_headers` publishes. This is the schedulable set on a "
             "`wrap` campaign. Composes with the section flags.",
    )
    dag_scope = dag_q.add_mutually_exclusive_group()
    dag_scope.add_argument(
        "--imported-only", action="store_true", dest="imported_only",
        help="Restrict the node set (slice / --loc) to IMPORTED entities "
             "(scope.json's derived / seeded closure).",
    )
    dag_scope.add_argument(
        "--targeted-only", action="store_true", dest="targeted_only",
        help="Restrict the node set (slice / --loc) to TARGETED entities "
             "(scope.json's own file set — the library itself, under either "
             "campaign objective). Pair with --api-only for the public surface "
             "it owns.",
    )


def _dispatch_query(args: argparse.Namespace, target: Path) -> None:
    if args.subject == "files":
        from crustify.query import query_files
        query_files(
            target,
            targeted_only=bool(getattr(args, "targeted_only", False)),
            imported_only=bool(getattr(args, "imported_only", False)),
            api_only=bool(getattr(args, "api_only", False)),
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
            loc=bool(getattr(args, "loc", False)),
            imported_only=bool(getattr(args, "imported_only", False)),
            targeted_only=bool(getattr(args, "targeted_only", False)),
            api_only=bool(getattr(args, "api_only", False)),
            full=bool(getattr(args, "full", False)),
        )
        return
    from crustify.query import query
    query(
        target,
        subject=args.subject,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        imported_only=bool(getattr(args, "imported_only", False)),
        targeted_only=bool(getattr(args, "targeted_only", False)),
        out_of_tree=bool(getattr(args, "out_of_tree", False)),
        in_tree=bool(getattr(args, "in_tree", False)),
        fields=bool(getattr(args, "fields", False)),
        api_only=bool(getattr(args, "api_only", False)),
        lifecycle_ops=bool(getattr(args, "lifecycle_ops", False)),
        users=bool(getattr(args, "users", False)),
        field_touchers=bool(getattr(args, "field_touchers", False)),
        update=getattr(args, "update", None),
        update_help=bool(getattr(args, "update_help", False)),
        schema=bool(getattr(args, "schema", False)),
        create=getattr(args, "create", None),
        manifest=bool(getattr(args, "manifest", False)),
        lifetime_for=getattr(args, "lifetime_for", None),
        taking=getattr(args, "taking", None),
        calling=getattr(args, "calling", None),
        callees=bool(getattr(args, "callees", False)),
        callers=bool(getattr(args, "callers", False)),
        depth=int(getattr(args, "depth", 1) or 1),
        array=bool(getattr(args, "array", False)),
    )


def main() -> None:
    from crustify import extract as extract_mod
    from crustify.layout import set_repo_root

    args = build_parser().parse_args()

    repo_root = Path(args.repo_root).resolve()
    set_repo_root(repo_root)
    target_rel = (args.target or "").strip("/")
    target = repo_root if target_rel in ("", ".") else (repo_root / target_rel)
    target = target.resolve()
    args._target_path = str(target)

    for cond, msg in (
        (not repo_root.exists(), f"repo_root does not exist: {repo_root}"),
        (not (repo_root / "crustify").is_dir(),
         f"no crustify/ under repo_root: {repo_root}"),
        (not target.exists(), f"target does not exist: {target}"),
    ):
        if cond:
            print(f"error: {msg}", file=sys.stderr)
            sys.exit(1)

    if args.command == "extract-ql":
        extract_mod.extract_ql(target)
        return
    if args.command == "query":
        _dispatch_query(args, target)
        return
    raise SystemExit(f"crustify-oracle: unknown command {args.command!r}")


if __name__ == "__main__":
    main()
