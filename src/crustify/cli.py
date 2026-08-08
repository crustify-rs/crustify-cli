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


#: The untyped lifetime tiers `translate --lifetime-for` accepts, in the order they
#: must be run — a typed cluster's Drop usually delegates to the untyped one's.
#: A struct tag is deliberately NOT accepted: a type's own droppers / disposers
#: / cloners are the TYPE wrapper's job, found from its record while it wraps
#: the type, not a separate tier run. These two have no record to find them
#: from — there is no `types.json` entry for "raw bytes" or "NUL-terminated" —
#: which is the whole reason they need a mode of their own.
LIFETIME_TIERS = ("void", "string")


def _lifetime_tier(s: str) -> str:
    """argparse type for `translate --lifetime-for`: one of :data:`LIFETIME_TIERS`."""
    if s not in LIFETIME_TIERS:
        raise argparse.ArgumentTypeError(
            f"--lifetime-for: expected {' or '.join(LIFETIME_TIERS)}, got {s!r}. "
            f"Only the untyped tiers are wrapped this way; a type's lifecycle "
            f"ops are discovered by the type wrapper from the type's own record "
            f"(translate --name {s})."
        )
    return s


def _add_subject_scope_flags(
    p: argparse.ArgumentParser,
    *,
    include_names: bool = True,
    include_files: bool = True,
    include_scope: bool = True,
    include_libraries: bool = True,
) -> None:
    """Attach the unified subject + scope filter flag set to a subparser.

    Subject filter group (mutually exclusive; defaults to ``--all``
    semantics when none is set):

      ``--all``     — all matching entries
      ``--names``   — entries by name
      ``--files``   — entries defined/declared in specific files

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


def _add_wrap_filter_flags(p: argparse.ArgumentParser) -> None:
    """Selection flags for the unified `translate` command (types + free symbols,
    no subject split). Wrap is **scope-blind by default**
    — a named entity wraps regardless of port/wrap scope (no refusal).
    `--wrap-only` / `--port-only` are opt-in *narrowing* filters, and `--file`
    restricts to a defining file (disambiguating a `--name` collision).

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
        "--max-syms", type=int, default=None, metavar="N", dest="max_syms",
        help="Per-batch symbol budget for wrap: a type's op-chunk size "
             "(lifecycle + method ops counted together) AND the free-symbol "
             "pool size per file. Default: config.TRANSLATE_MAX_SYMS.",
    )
    p.add_argument(
        "--max-loc", type=int, default=None, metavar="N", dest="max_loc",
        help="Per-batch lines-of-code budget (Σ body span; global=1, macro=0), "
             "binding together with --max-syms — whichever cap is hit first "
             "closes a batch. Default: config.TRANSLATE_MAX_LOC.",
    )
    p.add_argument(
        "--dag-layer", type=int, default=None, metavar="N", dest="dag_layer",
        help="Select EVERY in-scope unit at dag layer N — types AND symbols, "
             "port- or wrap-scope alike; narrow with --wrap-only/--port-only. "
             "Lifecycle ops that fold into a type are excluded (they ride with "
             "the type). Combines with --name; e2e driver mode.",
    )
    p.add_argument(
        "--skip", nargs="+", action="extend", default=None, metavar="NAME",
        help="Blocklist: drop these names from the selection (manual "
             "already-done list; the driver seeds it, crustify just honours it).",
    )
    p.add_argument(
        "--transitive", action="store_true", dest="transitive",
        help="Expand each --name through its TRANSITIVE dependency closure "
             "(the same forward edges `query dag --name` walks), keeping the "
             "units wrap can take. Expansion crosses symbols, so a type "
             "reachable only through a function comes along -- which a "
             "hand-written name list reliably misses. Combines with --skip "
             "(policy blocklist) and --review; already-wrapped items drop out "
             "unless --review is set. Check the plan with --dry-run first: a "
             "high-layer seed can pull in a large closure.",
    )
    p.add_argument(
        "--review", action="store_true", dest="review",
        help="Also schedule items that are ALREADY wrapped (default: only "
             "those whose `// crustify:todo` placeholder survives). The "
             "wrapper prompts make a second visit a REVIEW: with agent-owned "
             "state on disk the agent assesses its quality and accuracy and "
             "corrects it through the oracle. Use to re-examine a subtree, "
             "typically with --transitive.",
    )
    p.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print the plan (units, batches, first-layer deps) and stop.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="crustify-cli",
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
             "scope-config.json. Use . for the repo root; a repo-wide analysis is "
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
        help="Replace the provider CLI's own base prompt with crustify's. "
             "Default is --no-override-base-prompt: the provider's own "
             "instructions stay underneath crustify's stage prompt. Replacing "
             "them is cheaper per invocation but measurably worse output.",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help="Enable command-specific parallelization. Different "
             "commands interpret this differently — see each command's "
             "help. The analyze subjects are composer-only and ignore it.",
    )
    parser.add_argument(
        "--parallel-policy",
        choices=("per-agent", "serialize-per-file", "per-file"),
        default="per-agent",
        metavar="POLICY",
        help="How `--parallel` treats batches sharing a home `.rs`. "
             "`per-agent` (default) gives every batch its own worktree and "
             "agent, so two types homed in one module still run concurrently "
             "and reconcile as they land. `serialize-per-file` chains them "
             "into one worktree, run back to back: no landing conflict is "
             "possible, at the cost of making the longest chain the wave's "
             "floor. `per-file` keeps every batch its own agent but pools FREE "
             "SYMBOLS per defining file, so a symbol agent never spans two "
             "sources; without it symbols pool by count alone (up to "
             "`--max-syms`) regardless of file, which packs a layer's tail of "
             "one- and two-symbol files into far fewer agents. Layer batching "
             "is unaffected by all three — a wave always runs layer by layer.",
    )
    parser.add_argument(
        "--parallel-max",
        type=_parallel_max_type,
        default=8,
        metavar="N",
        help="Maximum concurrent agents when --parallel is on. Must be "
             "≥ 2 (1 is meaningless — that's the serial default). "
             "Default 8. Declared ONCE, here: a subparser copy sharing this "
             "`dest` re-applies its own default AFTER the global value is "
             "parsed, so `--parallel-max N` before the subcommand was silently "
             "reset to 8 — a wave asked for 32 and ran at 8, reporting 32.",
    )

    sub = parser.add_subparsers(dest="command", required=True)


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

    # -- scaffold (crates.json-driven .rs oracle) ------------------------
    scaffold_p = sub.add_parser(
        "scaffold",
        help=(
            "Resolve C symbols/types to their Rust .rs via crates.json. "
            "Deterministic, no LLM: the placement oracle is authored outside "
            "this stage, and an unplaced selection is a hard error. `--all` "
            "materializes the whole target; `--validate` runs the consistency "
            "gate."
        ),
    )
    # Selection is required and explicit — there is no default.
    # Not `required=True`: `--file` alone is a valid selection but lives outside
    # the group (it doubles as `--name`'s qualifier). scaffold() raises if the
    # whole selection is empty.
    scaffold_sel = scaffold_p.add_mutually_exclusive_group()
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
        "--name", nargs="+", action="extend", default=None, metavar="NAME",
        help="Resolve the named entit(ies) — type tags and/or symbol names "
             "(e.g. `--name git_odb git_odb_read`) — to the .rs module(s) "
             "homing their `// Replaces:` (port) / `// Wraps:` (wrap) anchor. A name "
             "with several homes (one per `tu` — a tag defined privately in more "
             "than one TU, or file-local statics) is REFUSED: they are different "
             "entities sharing a spelling, so pass `--file` to say which. Query "
             "mode prints the homed path (or `not created`); add --create to "
             "write the stub(s). The authoritative way an agent locates a "
             "wrapper module or where a dep lives.",
    )
    scaffold_sel.add_argument(
        "--validate", action="store_true",
        help="Run the crates.json consistency gate (every entity homed in "
             "exactly one .rs; crate depends_on acyclic) and exit.",
    )
    scaffold_p.add_argument(
        "--file", default=None, metavar="FILE",
        help="On its own, scaffold the crate + stub for the single in-scope "
             "source file at <target>/FILE, matching the file's elements "
             "wherever their wrappers home (e.g. `--file odb.h` reaches git_odb "
             "even though its wrapper homes at include/git2/odb.h). With "
             "`--name`, it QUALIFIES the name — the defining `tu` or a header "
             "that reaches it — which is how a name with several homes is "
             "disambiguated.",
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
            "Crates come out incomplete: build.rs carries the per-kind "
            "allowlists but no fn main, and bindgen.h's shim block is "
            "empty — finishing them needs a compiler in the loop."
        ),
    )
    bindgen_p.add_argument(
        "--libs", nargs="+", default=None, metavar="LIB",
        help="Restrict to these libraries (e.g. libssl). "
             "Default: every in-scope library.",
    )
    bindgen_p.add_argument(
        "--reset", action="store_true",
        help="Recompute the composer-owned state from scratch instead of "
             "accumulating onto it: build.rs's ALLOWED_*/BLOCKLIST_FOREIGN stop "
             "being a cross-target union (so an entity that left the scope "
             "leaves the array), and bindgen.h's include block is re-seeded "
             "(discarding hand ordering). Never touches the "
             "crustify:allowlist-agent block or the crustify:shims block.",
    )

    # -- wrap ------------------------------------------------------------
    # The SCOPE keeps its own name: `--wrap-only` / `--port-only` and
    # wrap-scope / port-scope are the dichotomy in scope.json, orthogonal to
    # which stage runs. Only the stage is called `translate`.
    wrap_p = sub.add_parser(
        "translate",
        help=(
            "Translate stage: emit Rust wrappers for the selected in-scope "
            "units (types AND free symbols, port- or wrap-scope) in "
            "dependency-layer order. "
            "Select with --name. One unified scheduler dispatches each unit "
            "to its wrapper (type / symbol); no subject split. Requires the "
            "scaffold + bindgen stages to have run for each library being "
            "wrapped."
        ),
    )
    _add_wrap_filter_flags(wrap_p)
    wrap_p.add_argument(
        "--lifetime-for", default=None, metavar="TIER", dest="lifetime_for",
        type=_lifetime_tier,
        help="UNTYPED-TIER mode: hand one SYMBOL wrapper the job of turning a "
             "tier's lifecycle primitives into a Rust lifetime contract (the "
             "strategy ZST + the smart-pointer Drop/Clone impls a reference to "
             "it needs to be owned in Rust). One agent does both halves: it "
             "reads back the `lifetime` blocks that exist (`query symbols "
             "--lifetime-for TIER`) and, when none do, discovers the routines "
             "that drop/dispose/clone the tier and submits their blocks first, "
             "over a wrap-scope candidate set. TIER is `void` (raw byte-level) "
             "or `string` (NUL-terminated) -- ONLY those two, and in that order, "
             "since a typed cluster's Drop usually delegates to the untyped "
             "one's. A struct tag is NOT accepted: a type's own droppers / "
             "disposers / cloners are found by the TYPE wrapper from the type's "
             "record, as part of wrapping it (`translate --name <tag>`). IS its own "
             "selector -- no --name.")

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


    elif args.command == "scaffold":
        _handle_scaffold(args, target)

    elif args.command == "bindgen":
        _handle_bindgen(args, target)


    elif args.command == "translate":
        _handle_wrap(args, target)



# -- analyze dispatch -----------------------------------------------------

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
    bindgen(target, libs=args.libs, reset=args.reset)


# -- query dispatch -------------------------------------------------------

def _handle_wrap(args: argparse.Namespace, target: Path) -> None:
    """Run the unified wrap stage over the --name
    selection. One scheduler emits type, string, array, and free-symbol
    wrappers alike — no subject split (the scheduler routes each unit to its
    wrapper prompt by kind)."""
    # Before any agent spawns: the suffix reaches them through the environment.
    spec = getattr(args, "lifetime_for", None)
    if spec:
        # `--lifetime-for SPEC` IS the selection: no --name, no DAG layer, no
        # batching. The SPEC is the whole selection.
        from crustify.wrap import wrap_lifetime_for
        wrap_lifetime_for(target, spec,
                          dry_run=bool(getattr(args, "dry_run", False)))
        return
    from crustify.wrap import wrap_types
    wrap_types(
        target,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        wrap_only=bool(getattr(args, "wrap_only", False)),
        port_only=bool(getattr(args, "port_only", False)),
        dag_layer=getattr(args, "dag_layer", None),
        skip=getattr(args, "skip", None),
        transitive=bool(getattr(args, "transitive", False)),
        review=bool(getattr(args, "review", False)),
        parallel=bool(getattr(args, "parallel", False)),
        chain_policy=getattr(args, "parallel_policy", "per-agent"),
        parallel_max=int(getattr(args, "parallel_max", 8)),
        max_syms=getattr(args, "max_syms", None),
        max_loc=getattr(args, "max_loc", None),
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
            f"       Run: crustify-cli {target} {command}",
            file=sys.stderr,
        )
        sys.exit(1)
