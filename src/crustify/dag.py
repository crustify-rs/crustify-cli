"""dag.py — the dependency-graph model and the readers built on it.

``deps-dag.json`` is a deterministic artifact of the C; this module is how every
consumer reads it. Split out of :mod:`crustify._schedule` because it is not
scheduling: the wrap scheduler and ``crustify-cli query`` both need the node
model, the DAG loader, the type metadata and the canonical op ordering, and
routing query's reads through the scheduler made an oracle look like it depended
on a stage it has nothing to do with.

The ordering in particular is shared ON PURPOSE — :func:`ordered_ops` is the one
definition both the scheduler (for ``--range`` windows) and ``query types --ops``
consume, so a window ``[A:B)`` means the same slice to both.
"""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable


SymKey = tuple[str, "str | None"]


# --------------------------------------------------------------------- model

@dataclass
class Node:
    id: str                       # type tag, or symbol name
    node_kind: str                # "type" | "symbol"
    subkind: str                  # struct/.../function_*/macro_*/"symbol" (bare)
    defined_in: str | None
    layer: int
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


def ordered_ops(node: Node, by_key: dict[SymKey, Node], lifecycle: set[str],
                in_scope: Callable[[Node], bool]) -> list[Node]:
    """A type's ops as the **canonical, windowable list**: the symbol nodes named
    by ``lifecycle`` that are ``in_scope``, ordered **lifecycle-first**
    (droppers/disposers/cloners) then alphabetical. This is the single ordering
    both the scheduler (for ``--range`` windows) and ``query types --name T
    --ops`` consume, so a window ``[A:B]`` means the same slice to both.

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


def load_type_meta(analysis_root: Path) -> dict[str, tuple[list[str], set[str]]]:
    """type tag -> (field names, lifecycle op names). Fields drive the
    the agent's accessor working set; the lifecycle set names its ops. Neither
    is a budget any more — a type is one batch.

    A type stores no lifecycle of its own — it is reverse-derived from the
    symbols whose ``lifetime`` acts on an arg of that type (droppers, cloners,
    field-disposers). Allocators and locking fns are deliberately not bundled;
    they reach the wrap set through the normal call graph.

    Resolved through ``manifest_name``, so under ``CRUSTIFY_OUT_SUFFIX`` this
    reads the arm's OWN fork. A suffixed run is a branch of the analysis, and
    an arm that could not see its own submissions would schedule every wave
    after the first off data that ignores what it just learned — while
    ``wrap._lifetime_by_sym``, reading the same tree for the lifecycle gate,
    already followed the suffix. The first wave is unaffected either way: the
    fork is a byte copy of canonical, so the two agree until an agent writes.

    The scheduler is the only consumer that follows the suffix. The scaffolder,
    the dag composer and the crates validator stay canonical — the Rust tree
    they feed is NOT forked by ``--out-suffix`` (session branches isolate that
    side), so two arms scaffolding from divergent field sets would contend for
    the same anchors."""
    from compose.scope import build_lifecycle_index, type_method_syms
    from crustify.layout import manifest_name

    meta: dict[str, tuple[list[str], set[str]]] = {}
    if not analysis_root.is_dir():
        return meta
    lifecycle = build_lifecycle_index(analysis_root)
    for f in analysis_root.rglob(manifest_name("types")):
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
