"""Orchestration for the ``translate`` command — stage glue over the
``--name`` scheduler.

Codegen is driven by :mod:`crustify._schedule`: selection is ``--name``
(repeatable) or a dag layer / dependency closure. Types and free symbols are
each their own unit and pool separately under the batch budget — a type no
longer absorbs its ops. The user supplies the dependency order (the DAG is
what they read to choose it); a confirmation prompt lists first-layer deps.

This module owns only the stage-specific pieces — the selection predicate
(:func:`_selection_pred`), the wrap-bound-op index (:func:`_wrap_bound_ops`),
the bindgen gate, and the emit seam to
:class:`crustify.agents.translate.TranslateAgent`. Scope (wrap/port) is applied
**here**, never in the DAG, via the same ``compose.scope`` classifier the
manifests use — as a filter the caller opts into, not a routing decision.

Selection is filtered by ``--objective``, which is also what the prompt is told
to DO with the batch. Under the default ``wrap`` an item whose
``// crustify:todo`` placeholder is gone is dropped as already done — that
filter is what makes ``--transitive`` usable, since a closure otherwise re-runs
everything. ``review`` and ``port`` both act ON emitted work, so both keep the
filled items: the first re-examines them, the second nativizes one whose C-side
readers are gone. Beyond that the scheduler emits every selected unit
(budget-bounded), checking only that each home ``.rs`` exists.
"""
from __future__ import annotations

import re as _re
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
    # No analysis-tree check: there is no tree. The records are composed from
    # the CodeQL tables (`crustify.manifests`), and `scope.build` already
    # refuses with the exact command to run when those are missing.
    from crustify import scope as _scope_mod
    scope_json = _scope_mod.build(layout, target, stage="wrap")
    # NO scaffolding here. The wrap stage READS the scaffolded tree — its agents
    # locate their modules via `scaffold --name` (query mode) and fill anchors;
    # they never create files, and neither does this. `scaffold` is a stage the
    # operator runs, and the scheduler's plan-time placement check already fails
    # with the exact remedy when an item's home `.rs` is missing.
    #
    # It used to call `scaffold(target, all=True, create=True)` here, which
    # re-scaffolded the WHOLE target on every invocation — `--dry-run` included.
    # That made a read-only command mutate the tree: after a `crates.json` home
    # was corrected, a dry run silently regenerated the moved type's stub, which
    # then blocked the fast-forward of the very wave that had filled it.
    return scope_json


def _lifetime_by_sym(layout: "Layout") -> dict:
    """``(name, defined_in) -> lifetime block`` for every symbol that HAS one.

    Straight off the ownership store: `lifetime` is authored, so no skeleton is
    needed to answer this and the 2.2s symbol compose is skipped entirely."""
    from crustify import store as _store
    return {(r.get("name"), r.get("defined_in")): r["lifetime"]
            for r in _store.load(layout).get("symbols") or []
            if r.get("lifetime")}


def _wrap_bound_ops(scope_json, entry_pair) -> dict:
    """``op name -> the WRAP-scope type whose wrapper already binds it``.

    A wrap-scope type's droppers / disposers / cloners are emitted BY that
    type's wrapper, as the strategy a `CBox` / `CVec` selects its `CDropped` /
    `CCloned` on. Scheduling one separately would emit a second, unrelated
    surface for one C routine — different anchor, possibly different file, and
    nothing downstream looks for the duplicate.

    A PORT-scope type binds nothing: its ops are ordinary symbols that no other
    stage will take, so they stay schedulable. That asymmetry is the whole rule
    — it is about who already emits the routine, not about which scope it is in.
    """
    from crustify.dag import load_type_meta
    wrap_tags = {e["name"]
                 for e in ((scope_json.get("wrap") or {}).get("types") or [])}
    out: dict[str, str] = {}
    for tag, (_fields, lifecycle) in load_type_meta(entry_pair).items():
        if tag in wrap_tags:
            for op in lifecycle:
                out.setdefault(op, tag)
    return out


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
            f"`crustify-cli {target} bindgen --libs {' '.join(missing)}` first."
        )


# ---------------------------------------------------------------------------
# Manifest indexers + budget (shared with the port stage)
# ---------------------------------------------------------------------------

def _translate_eligible_pred(scope_json):
    """Predicate: is this node something `translate` may take? **Anything in
    scope** — port or wrap, type or symbol. Only an out-of-scope entity is
    rejected, by the gate in :func:`translate_types`.

    Scope no longer routes: a port-scope symbol used to be refused as the port
    stage's, but that stage is retired, so refusing it left the entity with no
    stage at all. Both halves now reach the same type and symbol agents; scope
    remains a *filter* the caller opts into (`--port-only` / `--wrap-only`),
    not a gate the stage imposes."""
    from compose import scope
    is_wrap = scope.in_scope_pred(scope_json, "wrap")
    is_port = scope.in_scope_pred(scope_json, "port")

    def pred(n) -> bool:
        return is_wrap(n) or is_port(n)
    return pred


def _selection_pred(scope_json, *, files: set[str]):
    """Selection predicate over a :class:`_schedule.Node`: in-scope (port or
    wrap), optionally restricted to a defining file (which disambiguates a
    `--name` collision).

    Scope does NOT narrow here any more. It stopped being a selector when the
    two halves merged into one stage: what an agent DOES is the objective, and
    an item's scope is something the agent reads from the oracle to decide how
    to satisfy that objective. A caller who wants to see a layer split by scope
    asks the oracle (`query dag --layer N --port-only`) and passes the names."""
    eligible = _translate_eligible_pred(scope_json)

    def pred(n) -> bool:  # n: _schedule.Node
        if files and (n.defined_in or "") not in files:
            return False
        return eligible(n)
    return pred



def batch_objective(batch, objective: str, scope_of=None) -> str:
    """One objective per batch, because the prompt has one `{objective}`.

    A TYPE takes the caller's: a port-scope type is legitimately either
    wrapped (layout-compatible while C still reads it) or nativized, and
    which one depends on the opacification burn-down -- live state only the
    orchestrator tracks.

    A SYMBOL takes its SCOPE's, and the caller does not get a say: principles.md
    settles it -- wrap-scope symbols get a safe view over the FFI surface,
    port-scope ones are translated to native Rust. A wrap-scope symbol can
    never be ported (it is foreign code) and a port-scope one has no reason
    to stay wrapped for good, so exposing the choice would only be a way to
    get it wrong. `_schedule.pack` keys the free-symbol pool by scope so the
    question has a single answer here.

    `review` crosses both: it is a second visit to emitted work, whatever
    that work was, so scope has nothing to say about it.

    MODULE-LEVEL, and wired into `Stage.objective_of` as well as the emit seam,
    so `--dry-run` reports the objective each batch will actually be handed.
    While this lived inside the emit closure the plan could only print the
    CALLER's verb, which for a port-scope symbol batch is not what runs: a wave
    invoked with the default `wrap` showed `About to wrap:` over items the
    scheduler then ported."""
    if objective == "review" or not scope_of:
        return objective
    if any(u.kind == "type" for u in batch.units):
        return objective
    return scope_of(batch.members[0]) if batch.members else objective


def _translate_emit(
    target: Path, layout, *, max_syms: int, objective: str = "wrap",
    scope_of=None,
):
    """Production emit: one :class:`TranslateAgent` per scheduled batch. A type
    batch carries the type + this batch's op slice; a syms batch carries the
    pooled free symbols. The agent resolves each module's path itself via
    `scaffold --name` (query mode); nothing is pre-resolved here."""
    from crustify.agents.translate import TranslateAgent

    def emit(batch) -> None:  # batch: _schedule.Batch
        obj = batch_objective(batch, objective, scope_of)
        type_units = [u for u in batch.units if u.kind == "type"]
        if type_units:
            # type-pull: the batch's tags plus their field-accessor sets; the
            # agent pulls lifecycle and the cast graph from each record itself.
            # `_schedule.pack` gives a type-unit a batch to ITSELF, bounded by
            # neither cap, so this list holds one tag in practice. It stays a
            # list — and `types.md` stays written for a target SET — because
            # nothing here depends on the count, so a packing change that pooled
            # types would not need a second edit at this seam.
            TranslateAgent(
                target, batch_kind="type",
                tags=[u.node.id for u in type_units],
                kinds=[u.node.subkind for u in type_units],
                objective=obj,
                repo_root=layout.repo_root,
            ).run()
            return
        # syms-pull: hand the batch's pooled symbol names; the agent pulls each
        # one's record/deps/.rs via `crustify-cli query sym`/`query dag`.
        syms = [{"name": m.id, "defined_in": m.defined_in} for m in batch.members]
        TranslateAgent(target, batch_kind="syms", syms=syms,
                       objective=obj, repo_root=layout.repo_root).run()

    return emit


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

#: The untyped tiers this mode covers — mirrors ``cli.LIFETIME_TIERS``. A struct
#: tag is not one of them: the TYPE wrapper finds a type's own lifecycle ops from
#: its record while wrapping it.
LIFETIME_TIERS = ("void", "string")


def translate_lifetime_for(
    target: Path, spec: str, *, objective: str = "wrap",
    dry_run: bool = False,
) -> None:
    """Untyped-tier mode: hand ONE **symbol** wrapper the job of wrapping
    ``spec``'s lifecycle primitives.

    Discovery AND emission, in one agent. It reads back whatever `lifetime`
    blocks already exist (``query symbols --lifetime-for <spec>``); when none
    do, it scouts the codebase for the routines that drop/dispose/clone
    ``spec`` and submits their blocks through the oracle first (see
    `prompts/symbols.md`). Then it emits the Rust that turns
    them into a lifetime contract — the strategy ZST plus the smart-pointer
    Drop/Clone impls a reference to ``spec`` needs to be owned in Rust.

    Not routed through the DAG scheduler: there is no worklist to select,
    batch or layer — the SPEC *is* the selection. It still runs in its own
    worktree and lands on the session branch like every other wrap agent,
    because it emits code.

    ``spec`` is ``void`` (raw byte-level) or ``string`` (NUL-terminated) —
    :data:`LIFETIME_TIERS`, and nothing else. Run them in that order, since a
    typed cluster's Drop often delegates to the untyped one's. A struct tag is
    rejected rather than accepted-and-routed: those two tiers exist because
    there is no ``types.json`` record to discover them from, whereas a type HAS
    one, and its droppers / disposers / cloners are found by the TYPE wrapper
    while it wraps the type. Routing a tag here would run a symbol wrapper over
    a job the type wrapper owns, and land it in the wrong module.

    The ordinary scope-only analysis tree is the right input: the agent's
    candidate set is wrap-scope by instruction
    (`prompts/symbols.md`), so a primitive the target never
    reaches is not a gap.
    """
    if spec not in LIFETIME_TIERS:
        raise SystemExit(
            f"wrap --lifetime-for: expected {' or '.join(LIFETIME_TIERS)}, "
            f"got {spec!r}; a type's lifecycle ops are the type wrapper's job "
            f"(wrap --name {spec})."
        )
    import crustify._schedule as S
    from crustify.agents.translate import TranslateAgent
    from crustify.layout import Layout

    layout = Layout.discover(target)
    if dry_run:
        print(f"[translate dry-run] --lifetime-for {spec}: one agent, "
              f"no composed worklist (the agent discovers the primitives).")
        return

    def emit_factory(t_, l_):
        def emit(_batch) -> None:
            TranslateAgent(t_, batch_kind="syms", lifetime_for=spec,
                           objective=objective, repo_root=l_.repo_root).run()
        return emit

    batch = S.Batch(file=f"lifetime-for-{spec}", units=[], members=[],
                    fields=[], op_range=None, field_range=None)
    stage = S.Stage(
        verb=objective, in_scope=lambda n: True, emit_fn=lambda b: None,
        max_syms=1, emit_factory=emit_factory, target=target, layout=layout,
    )
    failures = S._isolated_wave({batch.file: [batch]}, stage, False, 1)
    if failures:
        raise SystemExit(
            f"wrap --lifetime-for {spec}: agent failed: {failures[0][1]}")
    print("[crustify-cli translate] done.")


#: `// Wraps: <name>` / `// Replaces: <name>`, at any comment depth and with an
#: optional trailing gloss the wrapper may have added.
_ANCHOR_RE = _re.compile(r"^\s*//+\s*(?:Wraps|Replaces):\s*([A-Za-z_]\w*)")
#: `// Field: <owner>.<field>` — the per-accessor placeholder, OWNER-QUALIFIED
#: (`prompts/principles.md`). Both halves are captured: the owner disambiguates a
#: module that homes several types (37 of 75 in the openssl tree) which share
#: field names, and the field half stays dotted for a flattened anonymous
#: member (`ssl_session_st` . `ext.hostname`) — a C tag carries no dot, so the
#: split is on the FIRST one.
_FIELD_RE = _re.compile(r"^\s*//+\s*Field:\s*([A-Za-z_]\w*)\.(\S+)")


def _closure_names(seeds: list[str], by_key, by_name, keep) -> list[str]:
    """``--transitive``: every dep of every seed, transitively, that wrap can take.

    BFS over ``dep_types`` + ``dep_syms``, the same forward edges `query dag
    --name` walks. Expansion goes through EVERY node so a type reachable only
    via a symbol is still collected (nothing in `ssl/` names `evp_rand_ctx_st`,
    but `RAND_bytes_ex` traffics in it) -- a types-only walk misses those, which
    is the whole reason a hand-written name list keeps coming up short. What is
    KEPT is narrowed by `keep`, so a port-scope dep is traversed but never
    scheduled: wrap must not take one (the scope gate below would refuse it)."""
    out, seen = [], set()
    stack = list(seeds)
    while stack:
        nm = stack.pop()
        if nm in seen:
            continue
        seen.add(nm)
        for k in (by_name.get(nm) or []):
            n = by_key[k]
            if keep(n):
                out.append(nm)
            stack.extend(d[0] for d in n.dep_types)
            stack.extend(d[0] for d in n.dep_syms)
    return sorted(set(out))


def _pending_names(names: list[str], layout, target: Path) -> tuple[list[str], list[str]]:
    """Split into (pending, already-wrapped) on the per-item `crustify:todo`.

    `scaffold` lays every item as ``// Wraps: <name>`` followed by a
    ``// crustify:todo`` placeholder; the wrapper deletes the placeholder when
    it fills the item, so a SURVIVING one is the on-disk record that the item
    is still open. Cheaper and more honest than tracking state elsewhere: it
    lives next to the code it describes and cannot drift from it."""
    from crustify import crates as _crates
    from crustify.scaffold import _TODO, _entries_for_names
    doc = _crates.load(layout)
    pending, done = [], []
    for nm in names:
        entries, missing = _entries_for_names(doc, [nm])
        if missing:                      # never scaffolded -> nothing filled
            pending.append(nm)
            continue
        open_ = False
        for e in entries:
            # `crate_path` is repo-root-relative (`crustify/rust/<crate>`), not
            # relative to `layout.rust` — joining it there double-prefixes.
            cp = Path(e["crate_path"] or "")
            p = (cp if cp.is_absolute() else layout.repo_root / cp) / e["rs"]
            try:
                lines = p.read_text().splitlines()
            except OSError:
                open_ = True             # home not on disk yet
                break
            # The anchor is matched loosely on purpose. `scaffold` lays it as
            # `// Wraps: <name>`, but a wrapper routinely promotes it to a doc
            # comment with a trailing gloss — `/// Wraps: stack_st
            # (crypto/stack/stack.c) — the element-ownership-agnostic surface`
            # — so an exact-line test reports every filled item as pending.
            # What is load-bearing is the placeholder BELOW the anchor, not the
            # anchor's own spelling.
            hit = False
            for i, ln in enumerate(lines):
                m = _ANCHOR_RE.match(ln)
                if m and m.group(1) == nm:
                    hit = True
                    if any(_TODO in l for l in lines[i + 1:i + 3]):
                        open_ = True
                    break
            if not hit:
                open_ = True             # no anchor here -> nothing emitted yet
            # A type is not done when its DEFINITION anchor is filled but an
            # accessor it owes is not.
            #
            # The ANCHOR'S EXISTENCE is the authorization -- no scope query
            # here. The scaffolder lays a `// Field:` anchor only for a field
            # port-scope code touches, so every anchor present is one the
            # wrapper owes. This used to intersect with a port-scope field set
            # because the scaffolder anchored every DECLARED field, and without
            # that filter an opaque type (`evp_pkey_st`: 21 anchors, 0
            # port-scope) stayed pending forever on placeholders nobody would
            # fill. Narrowing emission removed the reason for it.
            #
            # Matching is OWNER-QUALIFIED: `f.group(1) == nm` keeps a sibling
            # type's identically-named field in the same module from holding
            # THIS type open, which the previous unqualified match could not
            # distinguish.
            if not open_:
                for i, ln in enumerate(lines):
                    f = _FIELD_RE.match(ln)
                    if (f and f.group(1) == nm
                            and any(_TODO in l for l in lines[i + 1:i + 3])):
                        open_ = True
                        break
        (pending if open_ else done).append(nm)
    return pending, done


def translate_types(
    target: Path,
    *,
    names: list[str] | None = None,
    files: list[str] | None = None,
    dag_layer: int | None = None,
    skip: list[str] | None = None,
    transitive: bool = False,
    objective: str = "wrap",
    parallel: bool = False,
    chain_policy: str = "per-agent",
    parallel_max: int = 8,
    max_syms: int | None = None,
    max_loc: int | None = None,
    dry_run: bool = False,
    emit_fn=None,
) -> None:
    """Translate the selected in-scope units via the ``--name`` scheduler.

    Selection is ``--name`` (repeatable), a dag layer, or a dependency
    closure. Types and free symbols are each their own unit and pool
    separately under the batch budget. The scheduler runs in dependency-layer
    order and prints the first-layer deps as a heads-up (no prompt).
    """
    from compose import scope
    from crustify import config as _cfg
    from crustify import _schedule as S
    from crustify import manifests as _manifests
    from crustify.layout import Layout

    if max_syms is None:
        max_syms = _cfg.TRANSLATE_MAX_SYMS
    if max_loc is None:
        max_loc = _cfg.TRANSLATE_MAX_LOC

    layout = Layout.discover(target)
    scope_json = _preflight(target, layout)
    from crustify import dag as _dag
    dag = _dag.build(layout, target, stage="wrap")
    print(f"[crustify-cli translate] deps DAG: {dag.get('stats')}")

    by_key, by_name = S.load_nodes(dag)

    # A `--name` that names two nodes would put two unrelated entities on one
    # agent's worklist. `--dag-layer` is exempt: it selects keys, not names.
    _dag.require_unambiguous(names or [], by_key, by_name, set(files or []),
                             stage="wrap")

    base_in_scope = _selection_pred(scope_json, files=set(files or []))

    entry_pair = (_manifests.entries(layout, target, "types", stage="wrap"),
                  _manifests.entries(layout, target, "symbols", stage="wrap"))
    # Ops a wrap-scope type's own wrapper already emits — never scheduled
    # separately. See :func:`_wrap_bound_ops`.
    bound_ops = _wrap_bound_ops(scope_json, entry_pair)

    sel_names = list(names or [])
    if dag_layer is not None:
        # e2e driver mode: EVERY in-scope unit at dag layer N — types (any
        # in-scope) and wrap-scope free syms, minus macros and lifecycle
        # primitives.
        #
        # The primitive filter reads the `lifetime` blocks directly rather than
        # the composer's folded-op set. The fold is derived from those same
        # blocks, but the WRAP AGENT is what submits them: before a type is
        # wrapped its ops carry no role, so the fold is empty exactly when the
        # scheduler needs it and complete only afterwards. Reading the blocks at
        # selection time has no such ordering problem, and catches the untyped
        # tiers too (`CRYPTO_free` acts on `void`, so it belongs to no type's
        # method surface and the fold never held it).
        #
        # `base_in_scope`, not the bare eligibility predicate: it carries the
        # `--file` narrowing.
        sel_names += sorted({
            n.id for n in by_key.values()
            if n.layer == dag_layer and base_in_scope(n)
            and not (n.node_kind == "symbol"
                     and ((n.subkind or "").startswith("macro")
                          or n.id in bound_ops))})
    if transitive:
        _before = len(sel_names)
        sel_names = _closure_names(sel_names, by_key, by_name, base_in_scope)
        print(f"[crustify-cli translate] --transitive: {_before} seed(s) → "
              f"{len(sel_names)} unit(s) in the closure.")
    if skip:
        _sk = set(skip)
        sel_names = [s for s in sel_names if s not in _sk]
    # Already-wrapped items are dropped unless the objective asks for them.
    # The prompts define what a second visit IS: `review` has the agent assess
    # quality and accuracy against the principles and correct through the
    # oracle rather than re-emit; `port` has it nativize an item whose C-side
    # readers are gone. Dropping the already-done by default is what makes
    # --transitive usable at all -- else a closure selection re-runs everything.
    # `port` and `review` both act ON already-emitted work, so both bypass the
    # gate: `review` re-examines it, `port` escalates a wrapped item whose
    # C-side readers are now gone. Only the default `wrap` objective filters
    # to items whose `// crustify:todo` placeholder still survives.
    if objective == "wrap":
        sel_names, _done = _pending_names(sel_names, layout, target)
        if _done:
            print(f"[crustify-cli translate] skipping {len(_done)} already-wrapped "
                  f"item(s); --objective review|port to act on them: "
                  f"{', '.join(sorted(_done)[:8])}"
                  + (" …" if len(_done) > 8 else ""))
    if not sel_names:
        raise SystemExit(
            "wrap: nothing selected — pass --name / --dag-layer N "
            "(a --skip blocklist, or every item being wrapped already, may "
            "have emptied the selection; --objective review|port acts on "
            "filled items).")

    # Macros are header-defined: bindgen owns their <lib>-sys shims, so no stage
    # facades them. Exclude macro_* symbols from selection, and drop any --name
    # that resolves ONLY to macros so it neither schedules a wrap job nor
    # reports as "unknown".
    # ...with ONE exception: a macro that MINTS TYPES. `bindgen owns the shim`
    # is true of a constant or a function-like macro -- there is nothing to
    # facade. A generator is different: its expansion is a whole aggregate, so
    # the family wants one generic Rust type that its instances alias, and that
    # generic is code this stage has to write. `generates` carries EVERY minting
    # macro (`compose.macro_families` sets no count threshold: a family of one
    # here can be a family of three under another cmake flag), so the judgement
    # of whether a family earns a generic is the wrapper agent's, not this gate's.
    #
    # `_schedule.is_generator` is the same test from the other side — it routes
    # these to `types.md`. Shared so the two cannot disagree about a node.
    def _is_macro(n) -> bool:
        return ((n.subkind or "").startswith("macro")
                and n.node_kind == "symbol"
                and not S.is_generator(n))
    in_scope = lambda n: base_in_scope(n) and not _is_macro(n)
    skipped, kept = [], []
    for nm in sel_names:
        hits = [by_key[k] for k in (by_name.get(nm) or []) if base_in_scope(by_key[k])]
        (skipped if (hits and all(_is_macro(n) for n in hits)) else kept).append(nm)
    if skipped:
        print(f"[crustify-cli translate] skipping {len(skipped)} macro(s) — bindgen owns "
              f"their -sys shims: {', '.join(sorted(skipped))}")
    sel_names = kept
    if not sel_names:
        raise SystemExit(
            "wrap: nothing to wrap — selection resolved only to macros "
            "(bindgen owns their -sys shims).")

    # Scope gate. Out of scope is the ONLY refusal: both halves route to the
    # same agents, so scope no longer decides which stage takes an entity —
    # only whether this target owns it at all. Resolve scope-blind so we can
    # *see* the rejected ones instead of dropping them as "unknown".
    loose = {n.id: n for nm in sel_names
             for n in (by_key[k] for k in (by_name.get(nm) or []))
             if not _is_macro(n)}
    bad_oos = sorted(i for i, n in loose.items() if not in_scope(n))
    named_bound = sorted({i for i in loose if i in bound_ops})
    if named_bound:
        listing = "\n".join(f"  - {i}  (bound by `{bound_ops[i]}`)"
                            for i in named_bound)
        raise SystemExit(
            f"translate: {len(named_bound)} selected symbol"
            f"{'' if len(named_bound) == 1 else 's'} "
            f"{'is' if len(named_bound) == 1 else 'are'} a lifecycle op of a "
            f"WRAP-scope type — its wrapper emits it as the CDropped/CCloned "
            f"strategy, so wrapping it here would be a second surface for one "
            f"C routine:\n{listing}\n"
            f"  Wrap the owning type instead. A PORT-scope type's ops carry no "
            f"such binding and schedule normally.")
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

    # `Node -> "wrap" | "port"`, for the free-symbol pool key and the emit
    # seam's objective. Wrap wins a tie: the one entity in this target that is
    # in BOTH closures (`git_transport_cb`, a wrap-closure callback declared in
    # the port header include/git2/transport.h) is reached through a
    # function-pointer field, which is wrap work.
    _is_wrap = scope.in_scope_pred(scope_json, "wrap")
    def scope_of(n) -> str:
        return "wrap" if _is_wrap(n) else "port"

    # Worktree isolation engages whenever the production emit is in play (a
    # caller-supplied emit_fn, e.g. a test double, opts out).
    emit_factory = None if emit_fn else (
        lambda t, l: _translate_emit(t, l, max_syms=max_syms,
                                     objective=objective, scope_of=scope_of))
    stage = S.Stage(
        verb=objective, in_scope=in_scope,
        emit_fn=emit_fn or _translate_emit(target, layout, max_syms=max_syms,
                                           objective=objective,
                                           scope_of=scope_of),
        max_syms=max_syms, max_loc=max_loc, scope_of=scope_of,
        max_types=_cfg.TRANSLATE_MAX_TYPES, min_fields=_cfg.TRANSLATE_MIN_FIELDS,
        objective_of=lambda b: batch_objective(b, objective, scope_of),
        emit_factory=emit_factory, target=target, layout=layout,
    )
    failures = S.schedule(
        dag=dag,
        entry_pair=entry_pair,
        names=sel_names, stage=stage, parallelize=parallel,
        chain_policy=chain_policy, parallel_max=parallel_max, dry_run=dry_run,
        # `--transitive` is what closes the selection under dependencies, and
        # `--skip` re-opens it: a blocklisted unit that is NOT already emitted
        # leaves a hole a merged wave would step into.
        closed_selection=bool(transitive) and not skip,
    )
    if failures:
        # Print the EXCEPTION, not just the label. A batch that fails before its
        # agent starts (backend refuses, spawn errors, prompt/template raises)
        # writes no per-agent log at all, so pointing at the log directory was
        # the only thing said about it and the reason was unrecoverable
        # afterwards -- 11 of 17 batches in one wave left nothing but a pristine
        # worktree. The scheduler has carried the exception all along
        # (`_schedule` returns `list[tuple[Batch, BaseException]]`); it was
        # discarded here.
        lines = "\n".join(
            f"  - {b.label()}: {type(e).__name__}: {e}" for b, e in failures)
        raise SystemExit(
            f"wrap stage failed for {len(failures)} batch(es):\n{lines}\n"
            f"An agent that started also has a log under "
            f"crustify/targets/<rel>/logs/<session>/; one that never started "
            f"does not, and the line above is all there is.")
    if not dry_run:
        print("[crustify-cli translate] done.")
