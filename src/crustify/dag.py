"""dag.py — the dependency-graph model and the readers built on it.

``deps-dag.json`` is a deterministic artifact of the C; this module is how every
consumer reads it. Split out of :mod:`crustify._schedule` because it is not
scheduling: the wrap scheduler and ``crustify-oracle query`` both need the node
model, the DAG loader, the type metadata and the canonical op ordering, and
routing query's reads through the scheduler made an oracle look like it depended
on a stage it has nothing to do with.

The ordering in particular is shared ON PURPOSE — :func:`ordered_ops` is the one
definition both the scheduler and ``query types --ops`` consume, so the two
never disagree on a type's op set or its order.
"""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable


#: Every node's identity, types and symbols alike: ``(name, defined_in)``. A
#: bare name is ambiguous on BOTH sides — same-named file-local statics, and
#: TU-local aggregates (a `struct version_info` per `.c` that declares one).
NodeKey = tuple[str, "str | None"]

#: Historical alias. Kept because the scheduler's public signatures read
#: `SymKey` throughout and the two are the same tuple.
SymKey = NodeKey


# --------------------------------------------------------------------- model

@dataclass
class Node:
    id: str                       # type tag, or symbol name
    node_kind: str                # "type" | "symbol"
    subkind: str                  # struct/.../function_*/macro_*/"symbol" (bare)
    defined_in: str | None
    layer: int
    dep_types: list[NodeKey]
    dep_syms: list[NodeKey]
    # Cut cycle back-edges (FAS): types this node depends on that aren't wrapped
    # yet (render raw `ffi::T`); and the reverse — nodes that render *this* type
    # raw and should switch to its wrapper once it lands.
    fallback: list[NodeKey] = field(default_factory=list)
    back_fill: list[NodeKey] = field(default_factory=list)
    # Per-symbol lines-of-code (CodeQL body span; global=1, macro=0, 0 when the
    # `loc` column is absent — for a type node it is the struct field count).
    # Summed per batch against the port LoC budget.
    loc: int = 0
    #: Types this macro mints, when it is a template generator (else empty).
    #: Non-empty is what exempts it from the wrap stage's macro exclusion.
    generates: list[str] = field(default_factory=list)

    @property
    def key(self) -> SymKey:
        return (self.id, self.defined_in)

    @property
    def is_bare(self) -> bool:
        # the DAG emits "symbol" when nothing has classified `kind` yet
        return self.node_kind == "symbol" and self.subkind == "symbol"


def build(layout, target: Path, *, stage: str) -> dict:
    """Compose this target's DAG **in memory** and return it.

    There is no on-disk `deps-dag.json` in the read path any more. The graph is
    a pure function of the CodeQL CSVs and `scope.json`, so a cached copy is
    only ever as good as its last recompose — and the failure it produces is
    silent, not loud: a stale file schedules a wave against yesterday's layering
    and nothing says so. Composing costs well under a second against the several
    minutes any stage that needs it goes on to spend.

    `analyze dag --dump` still writes the JSON, for reading and diffing. Nothing
    reads it back.

    Both sides come from :mod:`crustify.manifests` -- there is no analysis tree
    to walk. The graph is a function of the CodeQL tables and `scope.json`
    alone; the store overlay contributes nothing to it, by design (a
    submission must never move a layer).
    """
    from compose.deps_dag import compose as _compose
    from crustify import cache as _cache, manifests as _manifests, scope as _scope

    # The dag cache is the valuable one: a hit skips scope AND both manifest
    # composes, not just the layering — 5.8s down to a 4.7 MB parse. Its
    # fingerprint is the same input set, since the graph is a function of the
    # CodeQL tables and scope alone (an agent submission never moves a layer).
    fp = _cache.fingerprint(layout, target)
    disk = _cache.load(layout.deps_dag(target), fp)
    if disk is not None:
        return disk

    dag = _compose(
        (_manifests.entries(layout, target, "types", stage=stage),
         _manifests.entries(layout, target, "symbols", stage=stage)),
        _scope.build(layout, target, stage=stage),
        codeql_dir=layout.codeql,
    )
    return _cache.store(layout.deps_dag(target), dag, fp)


def load_nodes(dag: dict) -> tuple[dict[SymKey, Node], dict[str, list[SymKey]]]:
    """Flatten ``deps-dag.json`` into ``(by_key, by_name)``. SCC super-nodes are
    flattened to their members (each member keeps its own deps/layer)."""
    by_key: dict[SymKey, Node] = {}
    by_name: dict[str, list[SymKey]] = {}

    def keys(group: dict | None, side: str) -> list[NodeKey]:
        return [(d["name"], d.get("defined_in")) for d in (group or {}).get(side) or []]

    def add(rec: dict, layer: int) -> None:
        deps = rec.get("deps") or {}
        n = Node(
            id=rec["id"],
            node_kind=rec["node_kind"],
            subkind=str(rec.get("subkind") or "symbol"),
            defined_in=rec.get("defined_in"),
            layer=layer,
            dep_types=keys(deps, "types"),
            dep_syms=keys(deps, "syms"),
            fallback=keys(rec.get("fallback"), "types"),
            back_fill=keys(rec.get("back_fill"), "types"),
            loc=int(rec.get("loc") or 0),
            generates=list(rec.get("generates") or ()),
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


def require_unambiguous(
    names: list[str],
    by_key: dict[NodeKey, Node],
    by_name: dict[str, list[NodeKey]],
    files: set[str] | None = None,
    *,
    stage: str,
) -> None:
    """Refuse a ``--name`` that still resolves to more than one node after
    ``--file`` has been applied.

    Both sides of the graph are keyed ``(name, defined_in)``, so a bare name is
    a QUERY, not an identity: `ring_buf` is two unrelated structs (the QUIC
    stream buffer in `include/internal/ring_buf.h` and a private one in
    `crypto/bio/bss_dgram_pair.c`), and `stat` is a type and a syscall. Taking
    every match silently unions two closures, and — for wrap — schedules an
    agent whose worklist spans types that share nothing but a spelling. Making
    the caller say WHICH costs one flag and removes the whole class of error.

    Bulk selectors (``--all``, ``--dag-layer N``) do not come through here:
    they resolve keys directly, never names, so there is nothing to disambiguate.
    """
    file_set = set(files or [])
    bad: list[tuple[str, list[str]]] = []
    for nm in dict.fromkeys(names or []):
        homes = [by_key[k].defined_in or "" for k in by_name.get(nm, [])
                 if not file_set or (by_key[k].defined_in or "") in file_set]
        if len(homes) > 1:
            bad.append((nm, sorted(homes)))
    if not bad:
        return
    lines = []
    for nm, homes in bad:
        lines.append(f"  - {nm}  ({len(homes)} nodes)")
        lines.extend(f"      --file {h or '<no defining file>'}" for h in homes)
    narrowed = " (already narrowed by --file)" if file_set else ""
    raise SystemExit(
        f"{stage}: {len(bad)} name(s) resolve to more than one node{narrowed} — "
        f"pass --file to pick one:\n" + "\n".join(lines))


def ordered_ops(node: Node, by_key: dict[SymKey, Node], lifecycle: set[str],
                in_scope: Callable[[Node], bool]) -> list[Node]:
    """A type's ops as the **canonical, windowable list**: the symbol nodes named
    by ``lifecycle`` that are ``in_scope``, ordered **lifecycle-first**
    (droppers/disposers/cloners) then alphabetical. This is the single ordering
    both the scheduler and ``query types --name T --ops`` consume.

    Membership comes from ``lifecycle`` — the op-name set reverse-derived from
    the analysis tree's ``lifetime`` records (:func:`load_type_meta` for the
    scheduler, ``_resolve`` for query) — and NOT from the dag. ``deps-dag.json``
    used to carry an ``ops`` list per type node, which made a deterministic
    artifact of the C a function of agent output, and left two sources to drift
    apart the moment a wave submitted a lifetime without a recompose. Reading it
    at schedule time also means a submission takes effect on the next wave with
    no ``analyze dag`` in between.

    A lifetime record names a FUNCTION, not a ``(name, defined_in)`` key, so
    membership is by id over ``by_key``; ``node_kind`` guards the case of a type
    tag colliding with an op name. Same-named statics in several files all
    qualify, as the dag's null-``defined_in`` fallback did."""
    ops = [n for n in by_key.values()
           if n.node_kind == "symbol" and n.id in lifecycle and in_scope(n)]
    ops.sort(key=lambda o: (o.id not in lifecycle, o.id))
    return ops


def load_type_meta(entry_pair) -> dict[str, tuple[list[str], set[str]]]:
    """type tag -> (field names, lifecycle op names). Fields drive the
    the agent's accessor working set; the lifecycle set names its ops. Neither
    is a budget any more — a type is one batch.

    A type stores no lifecycle of its own — it is reverse-derived from the
    symbols whose ``lifetime`` acts on an arg of that type (droppers, cloners,
    field-disposers). Allocators and locking fns are deliberately not bundled;
    they reach the wrap set through the normal call graph.

    Takes the composed ``(types, syms)`` pair, so a wave schedules against the
    same records `query` reports and the same store an agent just submitted to
    — one source, no fork to keep in step. Isolating an experimental arm is a
    git branch now, which is what ``--out-suffix`` was hand-rolling with
    filenames."""
    from compose.scope import build_lifecycle_index, type_method_syms

    meta: dict[str, tuple[list[str], set[str]]] = {}
    types, syms = entry_pair
    lifecycle = build_lifecycle_index((types, syms))
    for e in types:
        tag = e.get("name") or e.get("type")
        if not tag or tag in meta:
            continue
        fields = [x["name"] for x in (e.get("fields") or []) if x.get("name")]
        meta[tag] = (fields, set(type_method_syms(e, lifecycle)))
    return meta
