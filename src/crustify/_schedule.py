"""_schedule.py — the shared ``--name`` scheduler for the wrap and port stages.

Both stages select work by ``--name`` (repeatable), turn the selection into
budget-bounded batches, and run them — sequential within a source file,
parallel across disjoint files. The two stages differ only in a small
:class:`Stage` adapter (scope predicate + agent ``emit_fn`` + budget); every
piece of selection / unit-forming / packing / idempotency / prompting lives
here, once.

Model
-----
* **Node** — one entry of ``deps-dag.json`` (a type or a symbol), keyed by
  ``(id, defined_in)`` so same-named file-local statics stay distinct. A type
  node carries its ``ops`` as ``{name, defined_in}`` (Phase-0 DAG change).
* **Unit** — the agent's working set. A named **type** forms a *type-unit* =
  the type + its in-scope ops (ops are scope-filtered: a wrap run bundles the
  type's wrap-scope ops, a port run its port-scope ops, so the two stages
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

SymKey = tuple[str, "str | None"]


# --------------------------------------------------------------------- model

@dataclass
class Node:
    id: str                       # type tag, or symbol name
    node_kind: str                # "type" | "symbol"
    subkind: str                  # struct/.../function_*/macro_*/"symbol" (bare)
    defined_in: str | None
    layer: int
    ops: list[SymKey]             # type's owned ops, as (name, defined_in)
    dep_types: list[str]
    dep_syms: list[SymKey]
    # Cut cycle back-edges (FAS): types this node depends on that aren't wrapped
    # yet (render raw `ffi::T`); and the reverse — nodes that render *this* type
    # raw and should switch to its wrapper once it lands.
    fallback: list[str] = field(default_factory=list)
    back_fill: list[str] = field(default_factory=list)
    # Per-symbol lines-of-code (CodeQL body span; global=1, macro=0, 0 when the
    # `loc` column is absent — for a type node it is the struct field count).
    # Summed per batch against the port LoC budget.
    loc: int = 0

    @property
    def key(self) -> SymKey:
        return (self.id, self.defined_in)

    @property
    def is_bare(self) -> bool:
        # the DAG emits "symbol" when nothing has classified `kind` yet
        return self.node_kind == "symbol" and self.subkind == "symbol"


def load_nodes(dag: dict) -> tuple[dict[SymKey, Node], dict[str, list[SymKey]]]:
    """Flatten ``deps-dag.json`` into ``(by_key, by_name)``. SCC super-nodes are
    flattened to their members (each member keeps its own deps/layer)."""
    by_key: dict[SymKey, Node] = {}
    by_name: dict[str, list[SymKey]] = {}

    def add(rec: dict, layer: int) -> None:
        deps = rec.get("deps") or {}
        n = Node(
            id=rec["id"],
            node_kind=rec["node_kind"],
            subkind=str(rec.get("subkind") or "symbol"),
            defined_in=rec.get("defined_in"),
            layer=layer,
            ops=[(o["name"], o.get("defined_in")) for o in rec.get("ops") or []],
            dep_types=list(deps.get("types") or []),
            dep_syms=[(d["name"], d.get("defined_in")) for d in deps.get("syms") or []],
            fallback=list((rec.get("fallback") or {}).get("types") or []),
            back_fill=list((rec.get("back_fill") or {}).get("types") or []),
            loc=int(rec.get("loc") or 0),
        )
        by_key[n.key] = n
        by_name.setdefault(n.id, []).append(n.key)

    for layer, entries in enumerate(dag.get("layers", [])):
        for rec in entries:
            if "scc" in rec:
                deps = rec.get("deps")
                for m in rec["scc"]:
                    m = dict(m)
                    m.setdefault("deps", deps)
                    add(m, layer)
            else:
                add(rec, layer)
    return by_key, by_name


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
    """Refuse to schedule a port-scope symbol left unclassified
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
        if self.kind == "type":
            return f"{self.node.id}(+{len(self.fields)}f/{len(self.ops)}ops)"
        return self.node.id


def form_units(
    nodes: list[Node],
    by_key: dict[SymKey, Node],
    in_scope: Callable[[Node], bool],
    type_meta: dict[str, tuple[list[str], set[str]]] | None = None,
) -> list[Unit]:
    """Type → type-unit (type + its **in-scope** ops + field names); non-type →
    atomic unit. Ops are scope-filtered so wrap and port partition a type's ops,
    and ordered **lifecycle-first** (droppers/disposers/cloners) so the
    shape-bearing surface always lands in the first, type-def-bearing batch.

    A **callback** (a function-pointer typedef, `subkind == "callback"`) is a
    `node_kind == "symbol"` node in the dag, so it falls through to the
    sym-unit branch on its own — the wrap stage's `symbol_wrapper.md` (its
    callback section) emits the `#[repr(transparent)]` fn-pointer handle, not a
    struct wrapper."""
    type_meta = type_meta or {}
    units: list[Unit] = []
    for n in nodes:
        if n.node_kind == "type":
            fields, lifecycle = type_meta.get(n.id, ([], set()))
            ops = ordered_ops(n, by_key, lifecycle, in_scope)
            units.append(Unit("type", n, ops, list(fields)))
        else:
            units.append(Unit("sym", n))
    return units


def ordered_ops(node: Node, by_key: dict[SymKey, Node], lifecycle: set[str],
                in_scope: Callable[[Node], bool]) -> list[Node]:
    """A type's ops as the **canonical, windowable list**: those resolvable to a
    node and ``in_scope``, ordered **lifecycle-first** (droppers/disposers/
    cloners) then alphabetical. This is the single ordering both the scheduler
    (for ``--range`` windows) and ``query types --name T --ops`` consume, so a window
    ``[A:B]`` means the same slice to both."""
    ops = [by_key[k] for k in node.ops if k in by_key and in_scope(by_key[k])]
    ops.sort(key=lambda o: (o.id not in lifecycle, o.id))
    return ops


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

def load_type_meta(analysis_root: Path) -> dict[str, tuple[list[str], set[str]]]:
    """type tag -> (field names, lifecycle op names). Fields drive the
    ``max_fields`` accessor budget; the lifecycle set lets the packer hoist the
    shape-bearing ops into the first batch.

    A type stores no lifecycle of its own — it is reverse-derived from the
    symbols whose ``lifetime`` acts on an arg of that type (droppers, cloners,
    field-disposers). Allocators and locking fns are deliberately not bundled;
    they reach the wrap set through the normal call graph."""
    from compose.scope import build_lifecycle_index, type_method_syms

    meta: dict[str, tuple[list[str], set[str]]] = {}
    if not analysis_root.is_dir():
        return meta
    lifecycle = build_lifecycle_index(analysis_root)
    for f in analysis_root.rglob("types.json"):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("types", []):
            tag = e.get("name") or e.get("type")
            if not tag or tag in meta:
                continue
            fields = [x["name"] for x in (e.get("fields") or []) if x.get("name")]
            meta[tag] = (fields, set(type_method_syms(e, lifecycle)))
    return meta


# ----------------------------------------------------------------- packing

@dataclass
class Batch:
    file: str | None
    units: list[Unit] = field(default_factory=list)
    members: list[Node] = field(default_factory=list)   # pending type/op nodes
    fields: list[str] = field(default_factory=list)     # pending field-accessor slice
    # Static-tiling windows for a single struct/union/enum batch (the
    # ``type_wrapper`` pull path): half-open [lo:hi) into the type's canonical
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
        f = Path(self.file).name if self.file else "?"
        return f"{f}: {head}" + (f" +{len(self.units)-1}" if len(self.units) > 1 else "")


def _chunk(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def pack(
    units: list[Unit],
    *,
    max_syms: int,
    max_fields: int,
    max_loc: int | None = None,
) -> list[Batch]:
    """Per-file, budget-bounded batches. A type-unit splits into **sequential**
    sub-batches: ops chunked by ``max_syms`` (lifecycle-first, so they ride the
    first batch with the type def) and field accessors chunked by ``max_fields``,
    the i-th field chunk paired with the i-th op chunk. Atomic sym-units of one
    file pool under ``max_syms`` — and, when ``max_loc`` is set (the port LoC
    budget), also under a per-batch ``Σ node.loc`` cap, whichever binds first
    (a lone symbol heavier than ``max_loc`` still gets its own batch — a function
    is never split).

    Packing is **blind**: every selected member is emitted, bounded only by the
    budget. The scheduler does not inspect whether an element has already been
    translated — re-running a wave re-emits, and per-element idempotency is the
    agent's concern (see the module docstring)."""
    batches: list[Batch] = []
    pool: dict[str | None, list[Node]] = {}

    for u in units:
        if u.kind == "type" and u.node.subkind in (
                "struct", "union", "enum"):
            # Pull path: STATIC range-tiling over the *full* canonical lists, so
            # window [A:B) means the same to scheduler and `query types --range`.
            full_ops, full_fields = list(u.ops), list(u.fields)
            n = max(1,
                    -(-len(full_ops) // max_syms) if full_ops else 1,
                    -(-len(full_fields) // max_fields) if full_fields else 1)
            for i in range(n):
                o_lo = min(i * max_syms, len(full_ops))
                o_hi = min(o_lo + max_syms, len(full_ops))
                f_lo = min(i * max_fields, len(full_fields))
                f_hi = min(f_lo + max_fields, len(full_fields))
                win_ops, win_fields = full_ops[o_lo:o_hi], full_fields[f_lo:f_hi]
                b = Batch(file=u.file, units=[u],
                          op_range=(o_lo, o_hi), field_range=(f_lo, f_hi))
                if i == 0:
                    b.members.append(u.node)
                b.members.extend(win_ops)      # for confirm/labels (agent pulls names)
                b.fields = win_fields
                batches.append(b)
        elif u.kind == "type":
            # Push path — budget-chunked.
            ops, fields = list(u.ops), list(u.fields)
            op_chunks = _chunk(ops, max_syms) or [[]]
            field_chunks = _chunk(fields, max_fields) or [[]]
            for i in range(max(len(op_chunks), len(field_chunks))):
                b = Batch(file=u.file, units=[u])
                if i == 0:
                    b.members.append(u.node)
                if i < len(op_chunks):
                    b.members.extend(op_chunks[i])
                if i < len(field_chunks):
                    b.fields = field_chunks[i]
                if b.members or b.fields:
                    batches.append(b)
        else:
            pool.setdefault(u.file, []).append(u.node)

    # pool atomic syms per file into batches bounded by count (<= max_syms) and,
    # when set, lines-of-code (Σ loc <= max_loc) — whichever cap is hit first
    # closes the batch. A single sym whose loc already exceeds max_loc still goes
    # in its own batch (we never split a function).
    def _flush(fpath, chunk):
        b = Batch(file=fpath, units=[Unit("sym", s) for s in chunk])
        b.members = list(chunk)
        batches.append(b)

    for fpath, syms in pool.items():
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
    inside = {m.key for m in unit.members} | {(t, None) for t in []}
    dt: set[str] = set()
    ds: set[SymKey] = set()
    for m in unit.members:
        dt.update(m.dep_types)
        ds.update(m.dep_syms)
    ds = {k for k in ds if k not in inside}
    return sorted(dt), sorted(ds, key=lambda k: (k[0], k[1] or ""))


def _scope_label(key: SymKey | str, by_key, in_scope) -> str:
    """`(wrap)` / `(port)` / `(ext)` tag for a dep, for the prompt."""
    if isinstance(key, str):
        cands = [n for k, n in by_key.items() if k[0] == key and n.node_kind == "type"]
    else:
        cands = [by_key[key]] if key in by_key else []
    if not cands:
        return "ext"
    return "wrap" if in_scope(cands[0]) else "port"


def show_plan(
    units: list[Unit], batches: list[Batch], by_key, in_scope, verb: str,
) -> None:
    """Show what's about to run + its first-layer deps (informational only —
    the scheduler runs in dependency-layer order, so the deps are a heads-up,
    not a gate). **Type** deps are listed in full (the "wrap/port these first"
    signal); the long tail of symbol deps (libc, macros, sibling calls) is
    summarised by scope so the listing stays legible."""
    print(f"\nAbout to {verb}:")
    for u in units:
        print(f"  • {u.label()}")

    dt: set[str] = set()
    ds: set[SymKey] = set()
    for u in units:
        a, b = bundle_deps(u, by_key)
        dt.update(a)
        ds.update(b)
    inside = {m.id for u in units for m in u.members}
    type_deps = sorted(t for t in dt if t not in inside)
    sym_deps = [(n, df) for n, df in sorted(ds) if n not in inside]

    if type_deps:
        print("\nFirst-layer TYPE deps (emit these first, in your order):")
        for t in type_deps:
            print(f"  - {t} ({_scope_label(t, by_key, in_scope)})")
    if sym_deps:
        by_scope: dict[str, int] = {}
        for n, df in sym_deps:
            by_scope[_scope_label((n, df), by_key, in_scope)] = \
                by_scope.get(_scope_label((n, df), by_key, in_scope), 0) + 1
        tally = ", ".join(f"{v} {k}" for k, v in sorted(by_scope.items()))
        print(f"\n+ {len(sym_deps)} symbol dep(s) ({tally}) — the C/FFI bridge "
              f"covers any not-yet-emitted.")

    print(f"\n{len(batches)} batch(es) across {len({b.file for b in batches})} file(s).")


# ----------------------------------------------------------------- runner

EmitFn = Callable[[Batch], None]


@dataclass
class Stage:
    verb: str                                    # "wrap" | "port"
    in_scope: Callable[[Node], bool]             # type-SELECTION predicate (bound to port_paths)
    emit_fn: EmitFn                              # agent seam (serial / non-isolated)
    max_syms: int
    # Per-type field-accessor cap — only the WRAP stage windows type fields;
    # PORT schedules free symbols only (no type units), so it leaves this at the
    # unbounded default. `max_loc` is the PORT lines-of-code budget that binds
    # together with `max_syms` on the free-symbol pool (None = no LoC cap, e.g.
    # for wrap).
    max_fields: int = 10**9
    max_loc: int | None = None
    # OP-FACADING predicate — which of a type's ops THIS stage owns. Decoupled
    # from `in_scope` so wrap can select types scope-blind yet facade only
    # wrap-scope ops (port-scope ops go to the port stage), and port windows
    # only port-scope ops. Defaults to `in_scope` when unset.
    op_in_scope: Callable[[Node], bool] | None = None
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
    # Chain by scaffolded home `.rs` (write-disjoint), NOT by C source file —
    # multiple sources (oid.c + oid.h) home into one `.rs`, so source chaining
    # would race their parallel worktrees on that file. Falls back to source
    # grouping only when no rust tree is available (layout-less callers).
    if stage.layout is not None:
        from crustify import crates as _crates
        by_file = _chains_by_home(batches, _crates.load(stage.layout), stage.layout)
    else:
        by_file = {}
        for b in batches:
            by_file.setdefault(b.file, []).append(b)

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

def schedule(
    *,
    dag: dict,
    analysis_root: Path,
    names: list[str],
    stage: Stage,
    parallelize: bool = False,
    parallel_max: int = 4,
    yes: bool = False,
    dry_run: bool = False,
) -> list[tuple[Batch, BaseException]]:
    """End-to-end: resolve --names → units → budget batches → confirm → run.
    ``dry_run`` stops after printing the plan."""
    by_key, by_name = load_nodes(dag)
    nodes, unknown = resolve_names(names, by_key, by_name, stage.in_scope)
    if unknown:
        print(f"schedule: no in-scope match for: {', '.join(unknown)}", file=sys.stderr)
    if not nodes:
        raise SystemExit("schedule: nothing selected in scope.")

    bare_gate(nodes)
    type_meta = load_type_meta(analysis_root)
    # Type selection uses `in_scope`; op-facading uses `op_in_scope` (a type may
    # be selected scope-blind yet only own its same-stage ops).
    units = form_units(nodes, by_key, stage.op_in_scope or stage.in_scope, type_meta)
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

    all_batches = pack(units, max_syms=stage.max_syms, max_fields=stage.max_fields, max_loc=stage.max_loc)

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
        print(f"\n[{stage.verb} dry-run] {len(units)} unit(s) across "
              f"{len(layers)} dependency layer(s) (lower → higher):")
        for li in layers:
            lb = pack(by_layer[li], max_syms=stage.max_syms,
                      max_fields=stage.max_fields, max_loc=stage.max_loc)
            print(f"  L{li}: {len(by_layer[li])} unit(s) → {len(lb)} batch(es)"
                  f"{' (parallel)' if len(lb) > 1 else ''}")
        show_plan(units, all_batches, by_key, stage.in_scope, stage.verb)
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
        for li in layers:
            lb = pack(by_layer[li], max_syms=stage.max_syms,
                      max_fields=stage.max_fields, max_loc=stage.max_loc)
            if not lb:
                continue
            if len(layers) > 1:
                print(f"\n[{stage.verb}] dependency layer {li}: {len(by_layer[li])} "
                      f"unit(s) → {len(lb)} batch(es) (lower layers already landed)")
            show_plan(by_layer[li], lb, by_key, stage.in_scope, stage.verb)
            before = len(failures)
            failures += run(lb, stage, parallelize=parallelize,
                            parallel_max=parallel_max)
            slog.checkpoint(
                f"layer {li}: {len(by_layer[li])} unit(s), {len(lb)} batch(es), "
                f"{len(failures) - before} failure(s)")
        slog.line(f"[crustify] {len(failures)} failure(s) over "
                  f"{len(all_batches)} batch(es)")
    return failures
