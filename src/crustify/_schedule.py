"""_schedule.py — the ``--name`` scheduler behind the ``translate`` stage.

Both stages select work by ``--name`` (repeatable), turn the selection into
budget-bounded batches, and run them — sequential within a source file,
parallel across disjoint files. The two stages differ only in a small
:class:`Stage` adapter (scope predicate + agent ``emit_fn`` + budget); every
piece of selection / unit-forming / packing / idempotency / prompting lives
here, once.

Model
-----
* **Node** — one entry of ``deps-dag.json`` (a type or a symbol), keyed by
  ``(id, defined_in)`` so same-named file-local statics stay distinct. A type's
  ops are NOT carried here — the dag is deterministic and stores no lifecycle;
  they are reverse-derived from the analysis tree at schedule time
  (:func:`load_type_meta` -> :func:`ordered_ops`).
* **Unit** — the agent's working set. A named **type** forms a *type-unit* =
  the type + its in-scope ops (ops are scope-filtered: a wrap run bundles the
  type's import-section ops, a port run its target-section ops, so the two stages
  partition a type's ops and both write its ``<type>.rs`` additively). A named
  **non-type** (free symbol or directly-named op) is atomic.
* **Blind scheduling** — the scheduler does NOT inspect whether an element has
  already been translated. It schedules every selected member (bounded only by
  the budget), checking only that each member's home ``.rs`` exists on disk
  (crates.json placement — see :func:`resolve_path`). Re-running a wave therefore
  re-emits; per-element idempotency (the scaffolder's ``// crustify:todo`` fill
  markers and the agent's fill-or-skip) is the agent's concern, not the
  scheduler's.

Selection is ``--name`` only; ``--all`` / ``--dir`` / ``--file`` /
``--dag-layer`` are intentionally not exposed yet. The user supplies the
dependency order (the DAG is what they read to choose it); the scheduler never
gates on whether a dep is emitted — the C/FFI bridge keeps every intermediate
state compiling — and prints the first-layer deps before running so the plan is
visible (informational; not a prompt).
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Scaffolded anchors (see scaffold_manifest._type_block / _sym_block / _file_stub).

from crustify.dag import (        # the DAG model + its readers (not scheduling)
    SymKey, Node, load_nodes, load_type_meta, ordered_ops,
)


# ----------------------------------------------------------------- selection

def resolve_names(
    names: list[str],
    by_key: dict[SymKey, Node],
    by_name: dict[str, list[SymKey]],
    in_scope: Callable[[Node], bool],
) -> tuple[list[Node], list[str]]:
    """Resolve ``--name`` values to in-scope nodes. A bare name may map to
    several keys (a same-named static in >1 file, or a type-tag/symbol clash);
    all in-scope matches are taken. Returns ``(nodes, unknown)`` where
    ``unknown`` lists names that matched nothing in scope."""
    nodes: list[Node] = []
    unknown: list[str] = []
    seen: set[SymKey] = set()
    for name in names:
        keys = by_name.get(name) or []
        hit = [by_key[k] for k in keys if in_scope(by_key[k])]
        if not hit:
            unknown.append(name)
            continue
        for n in hit:
            if n.key not in seen:
                seen.add(n.key)
                nodes.append(n)
    return nodes, unknown


def bare_gate(nodes: list[Node]) -> None:
    """Refuse to schedule any symbol left unclassified
    (``kind: null`` → ``subkind == "symbol"``). Moved here from the scaffolder:
    the bare kind only exists in the DAG, never in the fresh composer."""
    bad = sorted({(n.id, n.defined_in or "?") for n in nodes if n.is_bare})
    if bad:
        listing = "\n".join(f"  - {n}  ({f})" for n, f in bad)
        raise SystemExit(
            f"schedule: {len(bad)} selected symbol(s) are unclassified "
            f"(kind=null). Run `analyze symbols` so every symbol carries a "
            f"subkind before wrap/port:\n{listing}")


# --------------------------------------------------------------------- units

def is_generator(n) -> bool:
    """``n`` is a type-minting macro — a macro whose expansion is a whole
    aggregate, so it stands for a FAMILY of same-shaped types.

    The one predicate two stages share: :mod:`crustify.translate` uses it to exempt
    generators from "macros are bindgen's", and :func:`form_units` to route them
    to the type wrapper. Keeping one definition is the point — the two were
    written apart and are the same question ("does this macro owe Rust a type?"),
    so they must not be able to disagree about a given node.

    ``generates`` is populated by ``compose.macro_families`` for EVERY minting
    macro, one instance or twenty: whether a family is a template is decided by
    conditional compilation, not by what this build happened to extract."""
    return (n.node_kind == "symbol"
            and (n.subkind or "").startswith("macro")
            and bool(getattr(n, "generates", None)))


@dataclass
class Unit:
    kind: str                  # "type" | "sym"
    node: Node                 # the type, or the lone symbol
    ops: list[Node] = field(default_factory=list)   # in-scope ops, lifecycle-first
    fields: list[str] = field(default_factory=list)  # the type's field names

    @property
    def file(self) -> str | None:
        return self.node.defined_in

    @property
    def members(self) -> list[Node]:
        return [self.node, *self.ops]

    def label(self) -> str:
        # No field/op counts: `fields` here is the type's DECLARED list, while
        # the scaffolder anchors only the target-touched subset, so the two
        # disagree — `evp_keymgmt_st` reported 35 fields against 0 anchors on
        # disk. The agent works from the anchors, so a count taken from
        # anywhere else is at best noise and at worst an instruction to exceed
        # them.
        return self.node.id


def form_units(
    nodes: list[Node],
    by_key: dict[SymKey, Node],
    type_meta: dict[str, tuple[list[str], set[str]]] | None = None,
) -> list[Unit]:
    """Type → type-unit (type + field names); non-type → atomic unit.

    A type no longer absorbs its ops. The fold pulled a type's lifecycle
    symbols out of the WHOLE dag rather than the selection, so naming a type
    dragged in work nobody asked for, and it only worked once a `lifetime`
    record existed — empty exactly when a first wave needed it. Ops are plain
    symbol units now: selected on their own, packed with the other symbols, and
    the type agent still binds its Drop/Clone from the record, which is where it
    read them from all along.

    A **callback** (a function-pointer typedef, `subkind == "callback"`) is a
    `node_kind == "symbol"` node in the dag, so it falls through to the
    sym-unit branch on its own — the wrap stage's `symbols.md` (its
    callback section) emits the `#[repr(transparent)]` fn-pointer handle, not a
    struct wrapper.

    A **generator** (a type-minting macro, `generates` non-empty) is also a
    `node_kind == "symbol"` node, but it does NOT fall through: its deliverable
    is a struct — the Rust generic every instance it mints aliases, with a
    `CCell` impl, a layout gate and field accessors — so it forms a TYPE-unit
    and routes to `types.md`. Left as a sym-unit it pooled into a shared syms
    batch under `symbols.md`, which is written for thin safe views over an FFI
    surface and has no lifecycle/accessor recipe; the generic came out right
    anyway, but by the agent reaching past its prompt. Becoming a type-unit also
    buys the never-split guarantee in `pack`, so a generator can no longer share
    an agent with unrelated symbols."""
    type_meta = type_meta or {}
    units: list[Unit] = []
    for n in nodes:
        if n.node_kind == "type" or is_generator(n):
            fields, _lifecycle = type_meta.get(n.id, ([], set()))
            units.append(Unit("type", n, [], list(fields)))
        else:
            units.append(Unit("sym", n))
    return units




# --------------------------------------------------------------- name → file

def resolve_path(node: Node, doc: dict, layout) -> Path | None:
    """The scaffolded ``.rs`` that homes this node, per ``crates.json`` (the
    placement oracle), verified present on disk — the file the agent fills.

    Placement, NOT anchor text, is the source of truth here: this consults
    crates.json and stats the resolved file, so a hand-edited or non-canonical
    anchor (e.g. a backticked ``Replaces: `name```) never reads as unresolved.
    Returns ``None`` only when crates.json has no home for the node, or that home
    was never materialized on disk. ``def_file`` disambiguates a name collision;
    a loose by-name lookup is the fallback when ``defined_in`` doesn't line up.
    This is the scheduler's only contact with the scaffolded tree — the scheduler
    no longer parses anchors at all (it schedules blindly; see module docstring)."""
    from crustify import crates as _crates
    from crustify.scaffold import _full_rs
    hit = (_crates.lookup(doc, node.id, file=node.defined_in or None)
           or _crates.lookup(doc, node.id))
    if not hit:
        return None
    p = Path(_full_rs(layout, hit["crate_path"], hit["rs"]))
    return p if p.exists() else None


# ----------------------------------------------------------- type metadata



# ----------------------------------------------------------------- packing

@dataclass
class Batch:
    file: str | None
    units: list[Unit] = field(default_factory=list)
    members: list[Node] = field(default_factory=list)   # pending type/op nodes
    fields: list[str] = field(default_factory=list)     # pending field-accessor slice
    # Static-tiling windows for a single struct/union/enum batch (the
    # ``types.md`` pull path): half-open [lo:hi) into the type's canonical
    # field / op lists. ``None`` for family/sym batches (push path).
    op_range: tuple[int, int] | None = None
    field_range: tuple[int, int] | None = None

    @property
    def n_syms(self) -> int:
        return sum(1 for m in self.members if m.node_kind == "symbol")

    @property
    def n_fields(self) -> int:
        return len(self.fields)

    def label(self) -> str:
        head = self.units[0].label() if self.units else "?"
        f = Path(self.file).name if self.file else "*"   # `*` = cross-file sym pool
        return f"{f}: {head}" + (f" +{len(self.units)-1}" if len(self.units) > 1 else "")


def _chunk(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def pack(
    units: list[Unit],
    *,
    max_syms: int,
    max_loc: int | None = None,
    syms_by_file: bool = False,
    scope_of: "Callable[[Node], str] | None" = None,
    max_types: int = 1,
    min_fields: int = 0,
) -> list[Batch]:
    """Budget-bounded batches. A type-unit gets a batch to itself, bounded by
    neither cap — see the comment below.

    Atomic sym-units pool under ``max_syms`` — and, when ``max_loc`` is set,
    also under a per-batch ``Σ node.loc`` cap, whichever binds
    first (a lone symbol heavier than ``max_loc`` still gets its own batch — a
    function is never split).

    ``scope_of`` partitions the pool by SECTION. It was once a correctness
    requirement -- the objective used to be derived per symbol from its scope,
    so a batch mixing sections could not be handed one correct verb. That
    derivation is gone: the orchestrator supplies `--objective` and a run
    carries one verb throughout, so a mixed batch is now perfectly answerable.
    What remains is LOCALITY: target and import units are different work
    (native translation versus a view over the seam), and keeping them in
    separate batches gives an agent a coherent set. The default pool is GLOBAL
    per layer (`None` key), and the mixing is routine rather than rare: on the
    libgit2 `src` target, layer 0 carries 250 target symbols beside 101 import,
    and layer 1 carries 321 beside 148. Passing ``scope_of`` splits them;
    leaving it unset pools them together.
    Default ``False``: symbols pool by budget alone, so one agent may carry
    symbols from several sources. The defining file is not a write boundary —
    the scaffolder homes symbols by ``crates.json``, several sources routinely
    land in one ``.rs``, and an agent is handed names, not a file — so splitting
    on it bought nothing and cost agents: a layer whose symbols trail off into
    one- and two-symbol files spawned an agent per file, each paying full
    worktree setup and context load to emit a couple of wrappers. ``True``
    (``--parallel-policy per-file``) restores the partition for a run that wants
    one source per agent.

    Layer batching is orthogonal and always applies: :func:`schedule` calls this
    once per dependency layer, so a batch never spans layers whatever the policy.

    Packing is **blind**: every selected member is emitted, bounded only by the
    budget. The scheduler does not inspect whether an element has already been
    translated — re-running a wave re-emits, and per-element idempotency is the
    agent's concern (see the module docstring)."""
    batches: list[Batch] = []
    pool: dict[str | None, list[Node]] = {}

    type_pool: list[Unit] = []
    for u in units:
        if u.kind == "type":
            # Types pool among THEMSELVES, never with symbols: `_translate_emit`
            # routes on `any(u.kind == "type")`, so a mixed batch would hand a
            # symbol to the type agent. They also need caps of their own — the
            # symbol ones are denominated in the wrong units (`max_syms` counts
            # symbol wrappers; a type costs several) — hence `max_types` and
            # `min_fields`, which cannot tell `ssl_connection_st` (250 fields)
            # from an opaque handle (0) but do stop the two sharing an agent.
            type_pool.append(u)
        else:
            # `None` key = one global pool for the layer (the default): the
            # defining file is not a write boundary, so it must not bound a batch.
            # Key is (file?, scope?) — file only under `per-file`, scope
            # whenever a classifier is supplied. Both default to None, i.e. one
            # pool per layer, which is what the plain budget policy wants.
            pool.setdefault((u.file if syms_by_file else None,
                             scope_of(u.node) if scope_of else None),
                            []).append(u.node)

    # Types: close on EITHER cap — `max_types` bounds the agent's output (the
    # binding one in practice), `min_fields` keeps a fat struct off a shared
    # agent by closing the batch as soon as the floor is met. `max_types=1`,
    # the default, reproduces one-type-per-batch exactly.
    def _flush_types(chunk: list[Unit]):
        b = Batch(file=chunk[0].file, units=list(chunk),
                  op_range=(0, 0),
                  field_range=(0, sum(len(u.fields) for u in chunk)))
        b.members = [u.node for u in chunk]
        b.fields = [f for u in chunk for f in u.fields]
        batches.append(b)

    chunk_t: list[Unit] = []
    fields_sum = 0
    for u in type_pool:
        n_fields = len(u.fields)
        # Closed BEFORE the append, and `n_fields >= min_fields` is the third
        # test: a type that meets the floor on its own never shares an agent,
        # whatever it happens to follow. Closing after the append instead made
        # the split order-dependent — a 30-field struct landed with whichever
        # handle preceded it and only stood alone when it happened to be first.
        if chunk_t and (len(chunk_t) >= max_types
                        or fields_sum >= min_fields
                        or n_fields >= min_fields):
            _flush_types(chunk_t)
            chunk_t, fields_sum = [], 0
        chunk_t.append(u)
        fields_sum += n_fields
    if chunk_t:
        _flush_types(chunk_t)

    # pool atomic syms into batches bounded by count (<= max_syms) and,
    # when set, lines-of-code (Σ loc <= max_loc) — whichever cap is hit first
    # closes the batch. A single sym whose loc already exceeds max_loc still goes
    # in its own batch (we never split a function).
    def _flush(fpath, chunk):
        b = Batch(file=fpath, units=[Unit("sym", s) for s in chunk])
        b.members = list(chunk)
        batches.append(b)

    for (fpath, _scope), syms in pool.items():
        chunk: list[Node] = []
        loc_sum = 0
        for s in syms:
            s_loc = s.loc if max_loc else 0
            if chunk and (len(chunk) >= max_syms
                          or (max_loc and loc_sum + s_loc > max_loc)):
                _flush(fpath, chunk)
                chunk, loc_sum = [], 0
            chunk.append(s)
            loc_sum += s_loc
        if chunk:
            _flush(fpath, chunk)
    return batches


# --------------------------------------------------------------- deps + prompt

def bundle_deps(
    unit: Unit, by_key: dict[SymKey, Node],
) -> tuple[list[str], list[SymKey]]:
    """First-layer deps of the whole bundle: the type's field-type deps ∪ every
    member's deps, minus members of the unit itself."""
    inside = {m.key for m in unit.members}
    dt: set[SymKey] = set()
    ds: set[SymKey] = set()
    for m in unit.members:
        dt.update(m.dep_types)
        ds.update(m.dep_syms)
    ds = {k for k in ds if k not in inside}
    bykey = lambda k: (k[0], k[1] or "")
    return sorted(dt, key=bykey), sorted(ds, key=bykey)


def _scope_label(key: SymKey, by_key, in_scope) -> str:
    """`(wrap)` / `(port)` / `(ext)` tag for a dep, for the prompt. Both sides
    are `(name, defined_in)` now, so this is a plain lookup — no name-scan, and
    no chance of labelling one TU's `version_info` from another's node."""
    n = by_key.get(key)
    return "ext" if n is None else ("wrap" if in_scope(n) else "port")


def show_plan(
    units: list[Unit], batches: list[Batch], by_key, in_scope, verb: str,
    verb_of: dict[tuple[str, str | None], str] | None = None,
) -> None:
    """Show what's about to run + its first-layer deps (informational only —
    the scheduler runs in dependency-layer order, so the deps are a heads-up,
    not a gate). **Type** deps are listed in full (the "wrap/port these first"
    signal); the long tail of symbol deps (libc, macros, sibling calls) is
    summarised by scope so the listing stays legible.

    `verb_of` maps a unit to the objective its BATCH will be handed, which is
    not always `verb` — see `Stage.objective_of`. When a wave is mixed each
    item is tagged, because "About to wrap" over an item the scheduler ports is
    the one thing a plan must not say."""
    mixed = bool(verb_of) and len(set(verb_of.values())) > 1
    print(f"\nAbout to {'translate' if mixed else verb}:")
    width = max((len(u.label()) for u in units), default=0) if mixed else 0
    for u in units:
        tag = ""
        if mixed:
            tag = f"  {verb_of.get((u.node.id, u.node.defined_in), verb)}"
        print(f"  • {u.label():<{width}}{tag}" if mixed else f"  • {u.label()}")

    dt: set[SymKey] = set()
    ds: set[SymKey] = set()
    for u in units:
        a, b = bundle_deps(u, by_key)
        dt.update(a)
        ds.update(b)
    inside = {m.key for u in units for m in u.members}
    bykey = lambda k: (k[0], k[1] or "")
    type_deps = sorted((k for k in dt if k not in inside), key=bykey)
    sym_deps = sorted((k for k in ds if k not in inside), key=bykey)

    if type_deps:
        print("\nFirst-layer TYPE deps (emit these first, in your order):")
        for t, df in type_deps:
            where = f" [{df}]" if df else ""
            print(f"  - {t}{where} ({_scope_label((t, df), by_key, in_scope)})")
    if sym_deps:
        by_scope: dict[str, int] = {}
        for k in sym_deps:
            lbl = _scope_label(k, by_key, in_scope)
            by_scope[lbl] = by_scope.get(lbl, 0) + 1
        tally = ", ".join(f"{v} {k}" for k, v in sorted(by_scope.items()))
        print(f"\n+ {len(sym_deps)} symbol dep(s) ({tally}) — the C/FFI bridge "
              f"covers any not-yet-emitted.")

    print(f"\n{len(batches)} batch(es) across {len({b.file for b in batches})} file(s).")


# ----------------------------------------------------------------- runner

EmitFn = Callable[[Batch], None]


@dataclass
class Stage:
    verb: str                                    # stage label for messages
    in_scope: Callable[[Node], bool]             # type-SELECTION predicate (bound to port_paths)
    emit_fn: EmitFn                              # agent seam (serial / non-isolated)
    max_syms: int
    # Lines-of-code budget, binding with `max_syms` on the free-symbol pool
    # (None = no LoC cap).
    max_loc: int | None = None
    # `Node -> "wrap" | "port"`. Partitions the free-symbol pool so a batch is
    # homogeneous in scope, which is what lets the emit seam derive one correct
    # objective for it. Unset = no scope partition (single-objective callers).
    scope_of: Callable[[Node], str] | None = None
    # Type-batch budgets, denominated in types and declared fields — the symbol
    # caps measure the wrong things for a type. Defaults reproduce the old
    # one-type-per-batch behaviour for callers that do not set them.
    max_types: int = 1
    min_fields: int = 0
    # `Batch -> verb`. The objective the emit seam will hand this batch. Wired
    # by the caller to the same function the emit seam calls, so `--dry-run`
    # cannot drift from what runs. Unset = everything takes `verb`.
    objective_of: Callable[["Batch"], str] | None = None
    shared_artifact_fn: Callable[[], None] | None = None  # serialized post-step
    # Worktree-isolation seam. When wired, EVERY agent runs in its own worktree,
    # serial or parallel alike: isolation is what makes an agent's scoped
    # `cargo check` mean anything, not a parallelism optimisation. Builds an emit
    # bound to that worktree (target + Layout rooted there). Leaving it unset (a
    # caller-supplied `emit_fn`, e.g. a test double) opts out and writes in place.
    emit_factory: Callable[[Path, Any], EmitFn] | None = None
    target: Path | None = None
    layout: Any = None


def _chains_by_home(batches: list[Batch], doc: dict, layout) -> dict[str, list[Batch]]:
    """Group batches into write-disjoint chains keyed by scaffolded home ``.rs``.

    Write-safety, not C-source layout, defines a chain: two batches that write
    the same ``.rs`` MUST be in one chain (run serially in one worktree), else
    with ``--parallel`` their separate worktrees both edit that file and collide
    at merge. The file-grained scaffolder homes multiple C sources into one
    ``.rs`` (e.g. ``oid.c`` + ``oid.h`` → ``oid.rs``), so chaining by source
    ``defined_in`` races them; chaining by home ``.rs`` serializes them.

    Union-find over batches sharing any home ``.rs`` — so a type batch whose ops
    home across several files joins every chain those files touch."""
    def homes(b: Batch) -> set[str]:
        hs = {str(p) for m in b.members for p in (resolve_path(m, doc, layout),) if p}
        return hs or {b.file or "?"}        # unresolved → fall back to source file

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bh = [homes(b) for b in batches]
    claim: dict[str, int] = {}              # home .rs → first batch that claimed it
    for i, hs in enumerate(bh):
        find(i)
        for h in hs:
            if h in claim:
                parent[find(i)] = find(claim[h])
            else:
                claim[h] = i
    chains: dict[int, list[Batch]] = {}
    for i, b in enumerate(batches):
        chains.setdefault(find(i), []).append(b)
    # key each chain by a representative home (stable; used only for slug/len)
    return {(sorted(bh[r])[0] if bh[r] else (batches[r].file or f"chain{r}")): ch
            for r, ch in chains.items()}


def run(
    batches: list[Batch], stage: Stage, *,
    parallelize: bool, parallel_max: int,
    chain_policy: str = "per-agent",
) -> list[tuple[Batch, BaseException]]:
    """Sequential within a file; disjoint files in parallel when requested.

    With the isolation seam (``emit_factory``/``target``/``layout``) wired, every
    file-chain runs in its **own git worktree** (finding F3) — with or without
    ``--parallel``, and even when there is only one chain. Isolation is not a
    parallelism optimisation: it is what makes a chain's scoped `cargo check`
    mean anything, and what gives the agent a branch to land. Each agent commits
    and merges its own work back into the session base; nothing here integrates,
    validates or tears down. Only a caller-supplied ``emit_fn`` (a test double)
    takes the in-place path."""
    failures: list[tuple[Batch, BaseException]] = []
    # `serialize-per-file` chains by scaffolded home `.rs` (write-disjoint), NOT
    # by C source file — multiple sources (oid.c + oid.h) home into one `.rs`, so
    # source chaining would race their parallel worktrees on that file. Falls
    # back to source grouping only when no rust tree is available (layout-less
    # callers).
    #
    # `per-agent` gives every batch its own chain, so two types homed in one
    # `.rs` run concurrently. Nothing is lost by that: an agent already lands by
    # rebasing onto the session branch and retrying its push, so a sibling that
    # got there first is handled — the chain only made the case impossible
    # rather than survivable. What it cost was the wave's floor: layer 0 of the
    # 45-type run packed 38 types into 23 chains, and the two longest
    # (`evp/evp_local.rs` and `evp/evp.rs`, 6 types apiece) set the entire
    # layer's wall clock at ~129m while everything else idled.
    #
    # The trade is real, though: two agents editing one `.rs` can land a textual
    # conflict the second one has to resolve mid-rebase, where chaining never
    # produced one. Type wrappers mostly append their own `impl` block, so the
    # regions rarely overlap — but "rarely" is doing work in that sentence.
    if chain_policy == "serialize-per-file" and stage.layout is not None:
        from crustify import crates as _crates
        by_file = _chains_by_home(batches, _crates.load(stage.layout), stage.layout)
    elif chain_policy == "serialize-per-file":
        by_file = {}
        for b in batches:
            by_file.setdefault(b.file, []).append(b)
    else:
        by_file = {f"{i}:{b.label()}": [b] for i, b in enumerate(batches)}

    # Unconditional when the seam is wired: one worktree per agent regardless of
    # `--parallel` or chain count. (It used to also require `parallelize` and
    # more than one chain, so a serial run wrote in place — which meant a serial
    # agent had no branch to land and validated against a tree it shared with
    # nobody, two different contracts for the same stage.)
    if (stage.emit_factory is not None and stage.target is not None
            and stage.layout is not None):
        return _isolated_wave(by_file, stage, parallelize, parallel_max)

    def run_chain(chain: list[Batch]) -> None:
        for b in chain:
            stage.emit_fn(b)

    if not parallelize or len(by_file) <= 1:
        for chain in by_file.values():
            try:
                run_chain(chain)
            except BaseException as e:                  # noqa: BLE001
                failures.append((chain[0], e))
    else:
        with ThreadPoolExecutor(max_workers=parallel_max) as ex:
            futs = {ex.submit(run_chain, chain): chain for chain in by_file.values()}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except BaseException as e:              # noqa: BLE001
                    failures.append((futs[fut][0], e))

    if stage.shared_artifact_fn is not None:
        stage.shared_artifact_fn()
    return failures


def _place_batch_anchors(b: "Batch", layout, target, stage) -> None:
    """Lay this batch's `// crustify:todo:` anchors, in ITS worktree.

    Deliberately after the fork, not before. An anchor is a placeholder for work
    one agent owes, so it belongs on that agent's branch and nowhere else: place
    them up front in the shared tree and every sibling sees placeholders it is
    not going to fill, which is both misleading context and a merge conflict
    waiting on a file two agents now both have a reason to touch. Placed here,
    the anchor lands on the branch that fills it.

    `review` writes nothing. It re-examines emitted work, so an item with no
    anchor has nothing to review — creating one would invite an agent to wrap it
    under an objective that is not `wrap`, which is exactly the confusion the
    objective split exists to prevent. Such items are reported and skipped.

    A type brings its ACCESSOR anchors, from `_field_map` — the fields TARGET
    code actually touches, not the declared layout. `Unit.fields` is the
    declared list and is the wrong set: it is what the batch BUDGET counts, and
    on an opaque type it would lay placeholders for accessors nobody owes
    (`bio_st`: 16 declared, 0 touched), which then hold the type open forever.
    """
    from crustify.scaffold import place_anchors, _field_map
    names = [u.node.id for u in b.units]
    if not names:
        return
    review = getattr(stage, "verb", None) == "review"
    fmap = _field_map(layout, target)
    fields = {nm: f for nm in names if (f := fmap.get(nm))}
    n, unanchored = place_anchors(layout, target, names, fields=fields,
                                  emit=not review)
    if review and unanchored:
        print(f"[crustify-cli {stage.verb}] {len(unanchored)} item(s) in this "
              f"batch have no anchor — nothing emitted to review: "
              + ", ".join(sorted(unanchored)[:8])
              + (" …" if len(unanchored) > 8 else ""))
    elif unanchored:
        print(f"[crustify-cli {stage.verb}] {len(unanchored)} item(s) have no "
              f"home in crates.json and were left unanchored: "
              + ", ".join(sorted(unanchored)[:8])
              + (" …" if len(unanchored) > 8 else ""))


def _isolated_wave(
    by_file: dict[str | None, list[Batch]], stage: Stage,
    parallelize: bool, parallel_max: int,
) -> list[tuple[Batch, BaseException]]:
    """Give every BATCH its own worktree off the session base, run its agent,
    and stop there.

    The scheduler's whole involvement in worktree management is in this function:
    materialize the session base once, fork one child per agent, symlink the
    shared read-only artifacts. It does not integrate, validate, commit on an
    agent's behalf, or tear anything down — the agent commits its own work and
    lands it by pushing to the session branch, rebasing and retrying when a
    sibling got there first. That is race-free without a lock because a push is
    one atomic ref update; see :mod:`crustify.worktree` for why the branch has no
    checkout and what fails if it does.

    One worktree per batch, created LAZILY — immediately before that batch's
    agent, not upfront for the whole wave. Two reasons, both learned the hard
    way (finding F15):

    * A chain's batches used to SHARE one worktree, which made the scheduler
      depend on the agent leaving it intact. The wrapper prompts tell an agent
      to purge its worktree once it has landed, so every chain past its first
      batch died on a missing tree — silently, since the failure is attributed
      to the chain and the later batches simply never run. Purge-on-success is
      the agent's contract to keep; nothing may be reused across agents.
    * Forking at spawn time rather than at wave start means a batch inherits
      every sibling that has landed meanwhile, not just the wave's starting
      point — so concurrent agents are as fresh as the session branch allows
      and the push-rebase path is exercised only for genuine overlap.

    A batch that fails aborts the REST OF ITS CHAIN: same-file batches are
    ordered, and a later one forking from a branch that never got its
    predecessor would emit against a half-wrapped module. Sibling chains are
    unaffected. The failure is reported against the batch that actually failed,
    not the chain head.

    Nothing is torn down here. A successful agent purges its own worktree, so
    what survives the wave is exactly the failures — which is the inspection
    surface a partial wave needs (finding F12); a successful agent's work is on
    the session branch, which is where it is meant to be read from.
    """
    from crustify import config as _cfg
    from crustify import worktree as W
    from crustify.worktree import _WT_DIR as _WT
    from crustify.layout import Layout

    repo = Path(stage.layout.repo_root)
    rel = stage.layout.rel_target(stage.target)
    chains = list(by_file.values())
    failures: list[tuple[Batch, BaseException]] = []

    # Once per session, adopted if it already exists — so a later dependency
    # layer forks from a branch that already holds the earlier layers' landed
    # work. No checkout: that is what lets agents push to it concurrently.
    base = W.session_base(repo, f"{stage.verb}-{_cfg.SESSION_ID}")

    def _slug(i: int, b: Batch) -> str:
        stem = Path(b.file).stem if b.file else "batch"
        # Unique by construction: session + index + a random suffix. Slugs used to
        # be `<verb>-<NN>-<stem>`, which collides across waves, and `add_worktree`
        # then force-removed the stale directory — silently destroying an earlier
        # agent's unlanded branch. With a random tail there is nothing to clear,
        # so a collision is impossible rather than papered over.
        return (f"{stage.verb}-{_cfg.SESSION_ID}-{i:02d}-"
                + re.sub(r"[^A-Za-z0-9]+", "_", stem)[:24]
                + "-" + secrets.token_hex(4))

    # `git worktree add` takes a repo-level lock and is NOT concurrency-safe;
    # creating worktrees in parallel raced and silently dropped a chain (finding
    # F14). Setup used to be serialized by living in the main thread; now that it
    # happens lazily inside the workers, this lock is what keeps that guarantee.
    # Only the setup is serialized — the agents themselves still run concurrently.
    wt_lock = threading.Lock()
    made = 0

    def _fork(i: int, b: Batch) -> Path:
        nonlocal made
        with wt_lock:
            wt = W.add_worktree(repo, base.branch, _slug(i, b))
            W.link_shared(wt, repo)
            made += 1
        return wt

    def run_chain_wt(i0: int, chain: list[Batch]) -> list[tuple[Batch, BaseException]]:
        for j, b in enumerate(chain):
            try:
                wt = _fork(i0 + j, b)
                _place_batch_anchors(b, Layout(wt), wt / rel, stage)
                stage.emit_factory(wt / rel, Layout(wt))(b)   # bound to the worktree
            except BaseException as e:                        # noqa: BLE001
                # Abort the rest of THIS chain (its later batches are ordered
                # behind this one) and report the batch that actually failed.
                return [(b, e)]
        return []

    _cfg.SESSION_BASE = base.branch
    try:
        with ThreadPoolExecutor(max_workers=parallel_max if parallelize else 1) as ex:
            futs, i0 = {}, 0
            for ch in chains:
                futs[ex.submit(run_chain_wt, i0, ch)] = ch
                i0 += len(ch)                # slug indices stay unique per batch
            for fut in as_completed(futs):
                try:
                    failures.extend(fut.result())
                except BaseException as e:   # noqa: BLE001
                    failures.append((futs[fut][0], e))
    finally:
        _cfg.SESSION_BASE = ""

    # Only what the scheduler itself did. It deliberately makes NO claim about
    # what landed on the base — it does not read the base tip, diff it, or check
    # whether an agent committed. Integration is the agents' business, and any
    # summary here would be a guess that reads as a report.
    print(f"[{stage.verb}] {made} agent worktree(s) forked under "
          f"{repo / _WT}; session branch {base.branch}")

    if stage.shared_artifact_fn is not None:
        stage.shared_artifact_fn()
    return failures


# -------------------------------------------------------------- orchestration

def _wave_label(w: list[int]) -> str:
    """`4` for a lone layer, `4-7` for a merged run — the same string in the
    plan, the console and the session log, so the three cannot disagree."""
    return str(w[0]) if len(w) == 1 else f"{w[0]}-{w[-1]}"


def coalesce_waves(
    by_layer: dict[int, list[Unit]],
    layers: list[int],
    *,
    closed: bool,
    **pack_kw,
) -> list[list[int]]:
    """Group consecutive dependency layers into WAVES — the unit a barrier
    actually falls between.

    A wave is a maximal run of consecutive layers whose UNION packs to exactly
    ONE batch. That single test is the entire safety argument: one batch is one
    agent, which emits its units in an order it controls, so a cross-layer edge
    inside the wave is satisfied by construction. Two batches would run
    concurrently in sibling worktrees and the higher layer's dep would not have
    landed.

    Deferring to :func:`pack` is what makes the test sufficient rather than
    merely conservative — it already enforces the three rules a hand-written
    check would have to rediscover:

    * a type-unit gets a batch to itself, so any union holding a type and
      anything else packs to >=2 and the run stops there;
    * ``scope_of`` partitions the pool, so a port+wrap union packs to >=2 —
      which is the case that matters, since a target-section unit above a
      import one has a real edge to it and co-scheduling would break it;
    * ``max_syms`` / ``max_loc`` are already the split condition, so an
      oversized union cannot merge.

    It also self-limits to the TAIL. A wide layer packs to several batches
    already, so nothing can absorb it and the genuinely parallel base is left
    alone; what collapses is the run of one-unit layers at the top, where each
    agent otherwise pays a full worktree build to emit a single function.

    Whole layers only, never a slice: a layer-N unit's closure lies entirely in
    layers < N, so a contiguous run of COMPLETE layers is closed under
    dependencies and a partial one is not.

    ``closed`` gates the whole thing on the selection being closed under
    dependencies (``--transitive``, and no ``--skip`` to re-open it). Without
    that a unit's deps may sit outside the selection entirely and the prefix
    argument does not hold, so every layer stays its own wave. There is no
    opt-out beyond that: where the merge is legal it is also strictly better —
    the alternative is paying a worktree build per unit to emit one function.
    The cost is blast radius, one failure taking the whole merged wave, and a
    failed batch is re-runnable where a wasted hour is not.
    """
    if not closed:
        return [[li] for li in layers]
    waves: list[list[int]] = []
    i = 0
    while i < len(layers):
        group, j = [layers[i]], i + 1
        while j < len(layers):
            cand = group + [layers[j]]
            merged = [u for li in cand for u in by_layer[li]]
            if len(pack(merged, **pack_kw)) != 1:
                break
            group, j = cand, j + 1
        waves.append(group)
        i = j
    return waves


def schedule(
    *,
    dag: dict,
    entry_pair: tuple[list, list],
    names: list[str],
    stage: Stage,
    parallelize: bool = False,
    chain_policy: str = "per-agent",
    parallel_max: int = 4,
    dry_run: bool = False,
    closed_selection: bool = False,
) -> list[tuple[Batch, BaseException]]:
    """End-to-end: resolve --names → units → budget batches → run.
    ``dry_run`` stops after printing the plan.

    ``closed_selection`` says the selection is closed under dependencies
    (``--transitive``), which is what lets :func:`coalesce_waves` merge a run of
    consecutive layers into one barrier. Left false, every layer is its own
    wave."""
    by_key, by_name = load_nodes(dag)
    nodes, unknown = resolve_names(names, by_key, by_name, stage.in_scope)
    if unknown:
        print(f"schedule: no in-scope match for: {', '.join(unknown)}", file=sys.stderr)
    if not nodes:
        raise SystemExit("schedule: nothing selected in scope.")

    bare_gate(nodes)
    type_meta = load_type_meta(entry_pair)
    units = form_units(nodes, by_key, type_meta)
    # ---- Dependency-layer scheduling --------------------------------------
    # Partition the selected units by their dag layer and run ascending: same
    # layer runs as one wave (batched per home .rs + effort budget, one worktree
    # per agent). A higher layer must fork from a base that already holds the
    # lower layers' output, which now holds because every layer's agents land on
    # the SAME session branch and `session_base` adopts it rather than
    # re-snapshotting. A single-layer selection is exactly one wave.
    from collections import defaultdict
    by_layer: dict[int, list[Unit]] = defaultdict(list)
    for u in units:
        by_layer[u.node.layer].append(u)
    layers = sorted(by_layer)

    syms_by_file = chain_policy == "per-file"
    pack_kw = dict(max_syms=stage.max_syms, max_loc=stage.max_loc,
                   syms_by_file=syms_by_file, scope_of=stage.scope_of,
                   max_types=stage.max_types, min_fields=stage.min_fields)
    # Packed PER WAVE and concatenated — never `pack(units)` over the whole
    # selection. A wave is the unit a barrier falls between, so packing across
    # one reports a plan that cannot happen: with the free-symbol pool no longer
    # partitioned by file, it merges symbols from different waves into one batch
    # and undercounts the run (9 units over 2 waves read as 1 batch where the
    # run does 2). `coalesce_waves` merges layers only where doing so is exactly
    # equivalent to one batch, so this stays honest.
    waves = coalesce_waves(by_layer, layers, closed=closed_selection, **pack_kw)
    wave_units = {tuple(w): [u for li in w for u in by_layer[li]] for w in waves}
    all_batches = [b for w in waves for b in pack(wave_units[tuple(w)], **pack_kw)]

    # Plan-time placement check: every batch member must have its home `.rs`
    # materialized on disk, or emit would fail mid-run — after parallel siblings
    # already ran. Surface it HERE (covers dry-run too), before any agent spawns.
    #
    # The check is deliberately BARE: it asks crates.json (the placement oracle)
    # for the item's home `.rs` and verifies the file EXISTS — it does NOT parse
    # anchors. An item whose anchor was hand-edited or written with a non-canonical
    # form (e.g. a backticked ``Replaces: `name```) still has its file on disk, so
    # it must not read as "unplaced"; only a genuinely un-scaffolded item (no
    # crates.json home, or its `.rs` never materialized) trips this. Skipped for
    # layout-less callers (tests) that cannot resolve crates.json.
    layout = getattr(stage, "layout", None)
    if layout is not None:
        from crustify import crates as _crates
        doc = _crates.load(layout)
        missing: set[tuple[str, str]] = set()
        for b in all_batches:
            for m in b.members:
                if resolve_path(m, doc, layout) is None:
                    missing.add((m.id, m.defined_in or "?"))
        if missing:
            listing = "\n".join(f"  - {n}  ({f})" for n, f in sorted(missing))
            raise SystemExit(
                f"{stage.verb}: {len(missing)} selected item(s) have no home "
                f"`.rs` on disk — the scaffolder never materialized them (no "
                f"crates.json home, or a stale tree). Re-run "
                f"`crustify-cli <target> scaffold` and retry:\n{listing}")

    if not all_batches:
        print("schedule: no batches produced (nothing to do).")
        return []

    if dry_run:
        # The objective each batch will be handed. Resolved through
        # `stage.objective_of` — the same function the emit seam calls — so the
        # plan cannot drift from what runs.
        obj_of = stage.objective_of or (lambda _b: stage.verb)
        verb_of = {(u.node.id, u.node.defined_in): obj_of(b)
                   for b in all_batches for u in b.units}
        tally: dict[str, int] = {}
        for v in verb_of.values():
            tally[v] = tally.get(v, 0) + 1
        split = (" — " + " · ".join(f"{n} {v}" for v, n in sorted(tally.items()))
                 if len(tally) > 1 else "")
        print(f"\n[{stage.verb} dry-run] {len(units)} unit(s) across "
              f"{len(layers)} dependency layer(s) (lower → higher){split}:")
        # Chains, not just batches. Batches are what gets packed; CHAINS are
        # what runs concurrently, and under `serialize-per-file` the two differ
        # whenever a layer homes several batches in one `.rs`. Reporting only
        # batches made the policy invisible in the one place you would check it.
        doc = None
        if chain_policy == "serialize-per-file" and stage.layout is not None:
            from crustify import crates as _crates
            doc = _crates.load(stage.layout)
        for w in waves:
            wu = wave_units[tuple(w)]
            lb = pack(wu, **pack_kw)
            n_chain = (len(_chains_by_home(lb, doc, stage.layout))
                       if doc is not None else len(lb))
            extra = (f" → {n_chain} chain(s)" if n_chain != len(lb) else "")
            merged = " (merged)" if len(w) > 1 else ""
            print(f"  L{_wave_label(w)}: {len(wu)} unit(s) → {len(lb)} batch(es)"
                  f"{extra}{' (parallel)' if n_chain > 1 else ''}{merged}")
        print(f"  policy: {chain_policy}")
        show_plan(units, all_batches, by_key, stage.in_scope, stage.verb, verb_of)
        return []

    # Run each layer in turn, lower → higher; a layer's agents land on the
    # session branch before the next layer forks its worktrees from it.
    #
    # `session.log` brackets THIS loop, so it captures what no agent record
    # can: worktree forking, the barrier between layers, and the land tail.
    # Its checkpoints flush per layer, so a killed run still accounts for the
    # layers that completed.
    from crustify import config as _cfg
    from crustify.agentlog import open_session_log

    log_root = None
    if layout is not None and stage.target is not None:
        log_root = layout.logs(stage.target) / _cfg.SESSION_ID

    failures: list[tuple[Batch, BaseException]] = []
    with open_session_log(log_root, stage.verb) as slog:
        slog.line(f"[crustify] {len(units)} unit(s), {len(layers)} layer(s), "
                  f"parallel={parallelize} max={parallel_max}")
        for w in waves:
            wu = wave_units[tuple(w)]
            lb = pack(wu, **pack_kw)
            if not lb:
                continue
            label = _wave_label(w)
            if len(waves) > 1:
                print(f"\n[{stage.verb}] dependency layer {label}: {len(wu)} "
                      f"unit(s) → {len(lb)} batch(es) (lower layers already landed)")
            show_plan(wu, lb, by_key, stage.in_scope, stage.verb)
            before = len(failures)
            failures += run(lb, stage, parallelize=parallelize,
                            parallel_max=parallel_max,
                            chain_policy=chain_policy)
            slog.checkpoint(
                f"layer {label}: {len(wu)} unit(s), {len(lb)} batch(es), "
                f"{len(failures) - before} failure(s)")
        slog.line(f"[crustify] {len(failures)} failure(s) over "
                  f"{len(all_batches)} batch(es)")
    return failures
