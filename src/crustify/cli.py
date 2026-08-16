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


def _add_wrap_filter_flags(p: argparse.ArgumentParser) -> None:
    """Selection flags for the unified `translate` command (types + free symbols,
    no subject split). Selection is **section-blind**: a named entity is
    translated whichever scope.json section it sits in, and `--objective` says
    what to DO with it. There is no section filter here — `crustify-oracle`
    carries `--target-only` / `--import-only` for inspecting a slice. `--file`
    restricts to a defining file (disambiguating a `--name` collision).

    A per-agent **effort budget** (`--max-syms` / `--max-loc`) caps each unit's
    workload so a "god object" can't blow one agent's context; the orchestrator
    slices the surface and hands the agent a fixed worklist.
    """
    p.add_argument(
        "--name", nargs="+", action="extend", metavar="NAME", default=None,
        help="Select unit(s) by name (either section). Pass all names as a "
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
        "--max-types", type=int, default=None, metavar="N", dest="max_types",
        help="Per-batch TYPE budget — how many types ride one type agent. The "
             "symbol caps measure the wrong things for a type, so it has its "
             "own; the ceiling is the agent's output, not its input. "
             "Default: config.TRANSLATE_MAX_TYPES.",
    )
    p.add_argument(
        "--min-fields", type=int, default=None, metavar="N", dest="min_fields",
        help="Per-batch DECLARED-field floor: a type batch closes once it holds "
             "this many, and a type meeting it alone never shares an agent. "
             "Binds together with --max-types — whichever is hit first closes "
             "the batch. Default: config.TRANSLATE_MIN_FIELDS.",
    )
    p.add_argument(
        "--dag-layer", type=int, default=None, metavar="N", dest="dag_layer",
        help="Select EVERY in-scope unit at dag layer N — types AND symbols, "
             "from either section. The section is not a selector here: the "
             "OBJECTIVE says what to do, and one run carries one objective. "
             "Use `crustify-oracle query dag --layer N --target-only` to "
             "inspect a slice by section. "
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
             "(policy blocklist); already-wrapped items drop out unless "
             "--objective review|port is set. Check the plan with --dry-run first: a "
             "high-layer seed can pull in a large closure.",
    )
    p.add_argument(
        "--objective", dest="objective", default=None,
        choices=("wrap", "port", "review"),
        help="What the agent is being asked to DO with the selection; handed "
             "to the prompt as `{objective}`. `wrap` (default) emits safe "
             "wrappers. `port` nativizes an item whose C-side readers are "
             "gone — for a type, its layout and possibly its storage. "
             "`review` re-examines emitted work as LLM-as-a-Judge. `port` "
             "and `review` both also BYPASS the already-done gate, since "
             "both act on items whose anchors are already filled. A fourth, "
             "`raw`, is not selectable here and is not overridable either: it "
             "is the discovery arm of a lifetime tier, set automatically by "
             "--lifetime-for, whose marker the arm is gated on. NOTE: this "
             "is the objective, NOT a section filter — `translate` has none; "
             "use `crustify-oracle query … --target-only` to inspect a slice "
             "by section. It is taken as given: nothing downstream substitutes "
             "a per-unit verb.",
    )
    p.add_argument(
        "--force", action="store_true", dest="force",
        help="Re-schedule items whose anchor is already FILLED. Without it "
             "they are warned about and dropped, since re-running an agent "
             "over finished work usually means the selection was wrong, not "
             "that the work needs redoing. Use --skip to drop them silently.",
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


    # Each stage binds ONE blurb and passes it to both `help=` (which renders in
    # the parent's subcommand listing) and `description=` (which renders on the
    # stage's own --help). Without the second, `crustify-cli … <stage> --help`
    # prints a usage line and a flag list and never says what the stage does.
    _audit_blurb = (
        "Deterministic audit (no LLM): entity-seeded global scan of the "
        "ported Rust tree, printed as JSON to stdout (nothing written to "
        "disk). Per seed: its own unsafe / raw-pointer / naked-ffi surface; "
        "plus a tree-wide `global` section (outside-impl raw ptrs, the "
        "ffi:: type-surface partition, and a c_void filter) and `totals`.")
    audit_p = sub.add_parser(
        "audit", help=_audit_blurb, description=_audit_blurb,
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
    _scaffold_blurb = (
        "Resolve C symbols/types to their Rust .rs via crates.json. "
        "Deterministic, no LLM: the placement oracle is authored outside "
        "this stage, and an unplaced selection is a hard error. `--all` "
        "materializes the whole target; `--validate` runs the consistency "
        "gate.")
    scaffold_p = sub.add_parser(
        "scaffold", help=_scaffold_blurb, description=_scaffold_blurb,
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
    _bindgen_blurb = (
        "Scaffold the <lib>-sys FFI crates from the analysis tree "
        "(deterministic; no LLM). Partitions the wrap-scope surface by "
        "owning crate (crates.json) into <target>/rust/crates/<lib>-sys/. "
        "Crates come out incomplete: build.rs carries the per-kind "
        "allowlists but no fn main, and bindgen.h's shim block is "
        "empty — finishing them needs a compiler in the loop.")
    bindgen_p = sub.add_parser(
        "bindgen", help=_bindgen_blurb, description=_bindgen_blurb,
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

    # -- translate ---------------------------------------------------------
    # SCOPE and OBJECTIVE are orthogonal and named apart. scope.json's sections
    # are `target` / `import` (crustify-oracle's `--target-only` /
    # `--import-only`) and say what the target COVERS; the objective says what
    # to do with a selection. Only the stage is called `translate`.
    _translate_blurb = (
        "Translate stage: emit Rust wrappers for the selected in-scope "
        "units (types AND free symbols, from either scope.json section) in "
        "dependency-layer order. "
        "Select with --name. One unified scheduler dispatches each unit "
        "to its wrapper (type / symbol); no subject split. Requires the "
        "scaffold + bindgen stages to have run for each library being "
        "wrapped. What an agent DOES with a selection is --objective, "
        "and it is taken as given: nothing downstream substitutes a per-unit "
        "verb, so one run carries one objective. Select the units that share "
        "one, run them, then select the next set. --dry-run prints the plan.")
    wrap_p = sub.add_parser(
        "translate", help=_translate_blurb, description=_translate_blurb,
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
             "over a wrap-scope candidate set. SETS the objective to `raw` "
             "-- the discovery arm, which this flag is the only way to reach "
             "and which --objective cannot select or override. TIER is `void` "
             "(raw byte-level) "
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
        #
        # It also SETS the objective, and --objective has no say: the tier is
        # the selection AND the mode. `raw` is the discovery arm of
        # `prompts/symbols.md`, gated there on the very marker this flag
        # plants in the target set, so the two are one decision -- which is
        # why `raw` is absent from --objective's choices and unreachable any
        # other way.
        from crustify.translate import translate_lifetime_for
        translate_lifetime_for(
            target, spec, dry_run=bool(getattr(args, "dry_run", False)))
        return
    from crustify.translate import translate_types
    translate_types(
        target,
        names=getattr(args, "name", None),
        files=getattr(args, "files", None),
        dag_layer=getattr(args, "dag_layer", None),
        skip=getattr(args, "skip", None),
        transitive=bool(getattr(args, "transitive", False)),
        max_types=getattr(args, "max_types", None),
        min_fields=getattr(args, "min_fields", None),
        objective=getattr(args, "objective", None) or "wrap",
        parallel=bool(getattr(args, "parallel", False)),
        chain_policy=getattr(args, "parallel_policy", "per-agent"),
        parallel_max=int(getattr(args, "parallel_max", 8)),
        max_syms=getattr(args, "max_syms", None),
        max_loc=getattr(args, "max_loc", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force", False)),
    )


# -- helpers --------------------------------------------------------------

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
