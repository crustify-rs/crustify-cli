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
             "derives from these tables on demand.",
    )
    _add_query_command(sub)
    return p


def _add_query_flags(p: argparse.ArgumentParser, *, facets: bool) -> None:
    """Flags for `query types`/`query syms` — the read-only oracle, resolved
    from the manifest (dag-free). With no `--name` they enumerate (filtered by
    scope / `--file`) as a name list; with `--name T` they
    introspect one entry — always the WHOLE record (several names → several
    records). On a type, `--fields`/`--ops` print its windowable lists
    (`facets`). The .rs module of an entry is found via
    `crustify-cli <target> scaffold --name <X>`, not here."""
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


def _add_query_command(sub) -> None:
    """Attach the `query` verb and its subjects."""
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
            "symbols",
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
             "(scope.json wrap closure).",
    )
    dag_scope.add_argument(
        "--port-only", action="store_true", dest="port_only",
        help="Restrict the node set (slice / --loc) to port-scope entities "
             "(scope.json port closure).",
    )


def _dispatch_query(args: argparse.Namespace, target: Path) -> None:
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
            loc=bool(getattr(args, "loc", False)),
            wrap_only=bool(getattr(args, "wrap_only", False)),
            port_only=bool(getattr(args, "port_only", False)),
        )
        return
    from crustify.query import query
    query(
        target,
        subject=args.subject,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        wrap_only=bool(getattr(args, "wrap_only", False)),
        port_only=bool(getattr(args, "port_only", False)),
        fields=bool(getattr(args, "fields", False)),
        ops=bool(getattr(args, "ops", False)),
        methods=bool(getattr(args, "methods", False)),
        field_touchers=bool(getattr(args, "field_touchers", False)),
        update=getattr(args, "update", None),
        update_help=bool(getattr(args, "update_help", False)),
        schema=bool(getattr(args, "schema", False)),
        create=getattr(args, "create", None),
        manifest=bool(getattr(args, "manifest", False)),
        lifetime_for=getattr(args, "lifetime_for", None),
        taking=getattr(args, "taking", None),
        calling=getattr(args, "calling", None),
        hops=int(getattr(args, "hops", 1) or 1),
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
