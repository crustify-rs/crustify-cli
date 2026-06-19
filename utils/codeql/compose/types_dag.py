#!/usr/bin/env python3
"""types_dag.py — build the scope-agnostic type dependency DAG.

Reads every `<analysis_root>/**/types.json` in the analysis tree and builds
a directed graph over **all** type entries (port + wrap, every kind), where

    A -> B  means  "A's wrapper names B's type"

There is a single, uniform edge rule (see `docs/WRAP_STAGE_PLAN.md` §2.1):

  > For each entry A, add `A -> B` for every non-scalar type B that A
  > references — regardless of ownership, mutability, nullability, or kind.

The references are read structurally from the schema, the same way for
every kind (no entry is assumed to be a leaf a priori):

  - struct/union (and any kind with `fields[]`): each field that carries a
    `type` string (i.e. a non-scalar field — scalars are bare `{name}`),
    with the C type resolved to its canonical struct/union/enum tag;
  - `typegen_instance`: its `arg_type` and `generator`.

Ownership (`ptr.owned`), inheritance (`polymorphic.base`, which is an
embedded by-value first member and therefore already a `fields[].type`),
and the const/mut/nullable flags do NOT create separate edges.

The graph is cyclic (C types are freely mutually recursive), so the output
is computed with Tarjan SCC condensation + longest-path layering:

  - layer 0  = leaves (entries that reference no in-universe type);
  - layer N  depends only on layers < N;
  - an SCC of size > 1 is emitted as a single node (its members must be
    wrapped together).

Output `types-dag.json` is **scope-agnostic**. The wrap/port orchestrators
apply the scope.json filter at schedule time; this composer never reads
scope.json. Nodes are identified by stable manifest fields (`type`,
`defined_in`/`declared_in[0]`, `linked_in`, `kind`, `deps`) — never by a
resolved crate/`.rs` path, which is derived later and may change.

Read-only on the analysis tree. No CodeQL, no LLM.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------- model

class _Node:
    """Aggregated per-tag record (deduped across stem-group manifests)."""

    __slots__ = ("tag", "kind", "defined_in", "declared_in", "linked_in",
                 "type_refs", "deps")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.kind: str | None = None
        self.defined_in: str | None = None
        self.declared_in: str | None = None
        self.linked_in: str | None = None
        # raw C-type-reference strings collected from the entry (resolved to
        # tags in a second pass once the whole universe is known).
        self.type_refs: set[str] = set()
        # resolved out-edges (tags), filled after resolution.
        self.deps: set[str] = set()

    def absorb(self, entry: dict[str, Any]) -> None:
        """Merge one on-disk entry for this tag into the aggregate. Prefer
        non-null scalar metadata; union the type references."""
        if self.kind is None:
            self.kind = entry.get("kind")
        if self.defined_in is None:
            self.defined_in = entry.get("defined_in")
        if self.declared_in is None:
            dh = entry.get("declared_in")
            if isinstance(dh, list):
                self.declared_in = dh[0] if dh else None
            elif isinstance(dh, str):
                self.declared_in = dh
        if self.linked_in is None:
            self.linked_in = entry.get("linked_in")
        for ref in _entry_type_refs(entry):
            self.type_refs.add(ref)

    def origin(self) -> str | None:
        """defined_in, falling back to declared_in[0] (the node's source
        anchor for later path/crate resolution)."""
        return self.defined_in or self.declared_in


# ------------------------------------------------------------------- parsing

_DROP_TOKENS = frozenset({
    "const", "volatile", "struct", "union", "enum",
    "unsigned", "signed", "*", "",
})
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")


def _base_type_name(type_str: str | None) -> str | None:
    """Reduce a C type string to its candidate user-type identifier, or
    None when it names no user type (primitive, void, function pointer).

    Examples::

        "OSSL_LIB_CTX *"      -> "OSSL_LIB_CTX"
        "const BIO_METHOD *"  -> "BIO_METHOD"
        "stack_st_SSL_COMP *" -> "stack_st_SSL_COMP"
        "unsigned char"       -> "char"   (primitive -> dropped downstream)
        "void *"              -> "void"   (dropped downstream)
        "..(*)(..)"           -> None     (function pointer)
        "const void **"       -> "void"
    """
    if not type_str:
        return None
    # Function pointers / unnameable composite types.
    if "(" in type_str or ")" in type_str:
        return None
    # Drop array subscripts.
    s = re.sub(r"\[.*?\]", "", type_str)
    toks = re.split(r"[\s*]+", s)
    cand = [t for t in toks if t and t not in _DROP_TOKENS]
    if not cand:
        return None
    name = cand[-1]
    return name if _IDENT_RE.match(name) else None


def _entry_type_refs(entry: dict[str, Any]) -> set[str]:
    """All raw C-type-reference strings an entry carries (pre-resolution).

    Uniform across kinds: every non-scalar field's `type`, plus a
    typegen_instance's `arg_type` / `generator`.
    """
    refs: set[str] = set()
    for fld in entry.get("fields") or []:
        # Scalar-single fields are bare {name}; non-scalar fields carry a
        # `type`. That `type` key IS the non-scalar discriminator.
        t = fld.get("type")
        if t:
            refs.add(t)
    # typegen_instance back-references (already bare tags, not C strings,
    # but they round-trip through the resolver harmlessly).
    for key in ("arg_type", "generator"):
        v = entry.get(key)
        if v:
            refs.add(v)
    return refs


def _is_entry(entry: dict[str, Any]) -> bool:
    tag = entry.get("type")
    return bool(tag) and not str(tag).startswith("_")


# ------------------------------------------------------------------ collect

def _collect(analysis_root: Path) -> dict[str, _Node]:
    """Walk every types.json; aggregate one _Node per type tag."""
    nodes: dict[str, _Node] = {}
    for f in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for entry in doc.get("types", []):
            if not _is_entry(entry):
                continue
            tag = entry["type"]
            node = nodes.get(tag)
            if node is None:
                node = nodes[tag] = _Node(tag)
            node.absorb(entry)
    return nodes


def _build_typedef_map(nodes: dict[str, _Node],
                       analysis_root: Path) -> dict[str, str]:
    """alias -> canonical tag, built from every entry's `typedef[]` plus the
    identity `tag -> tag`. Re-reads the tree once for aliases (cheap; keeps
    _Node slim)."""
    alias_map: dict[str, str] = {tag: tag for tag in nodes}
    for f in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for entry in doc.get("types", []):
            if not _is_entry(entry):
                continue
            tag = entry["type"]
            for alias in entry.get("typedef") or []:
                # Don't let an alias clobber a real tag of the same spelling.
                alias_map.setdefault(alias, tag)
    return alias_map


def _resolve_edges(nodes: dict[str, _Node],
                   alias_map: dict[str, str]) -> None:
    """Resolve each node's raw type refs to in-universe tags → `deps`."""
    for node in nodes.values():
        for ref in node.type_refs:
            name = _base_type_name(ref) if _looks_like_ctype(ref) else ref
            if not name:
                continue
            tag = alias_map.get(name)
            if tag is not None and tag != node.tag:
                node.deps.add(tag)


def _looks_like_ctype(ref: str) -> bool:
    """arg_type/generator come in as bare tags; field types as C strings.
    Anything with whitespace, `*`, or brackets is a C string needing
    `_base_type_name`; a bare identifier is used as-is."""
    return bool(re.search(r"[\s*\[\](]", ref))


# ------------------------------------------------------- Tarjan SCC + layers

def _tarjan_scc(nodes: dict[str, _Node]) -> list[list[str]]:
    """Iterative Tarjan. Returns SCCs in reverse-topological order
    (each SCC appears before the SCCs it depends on)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    # Explicit stack of (tag, iterator-over-deps) to avoid recursion limits
    # on deep type graphs.
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [
            (root, sorted(nodes[root].deps))
        ]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            tag, deps = work[-1]
            progressed = False
            while deps:
                w = deps.pop(0)
                if w not in nodes:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, sorted(nodes[w].deps)))
                    progressed = True
                    break
                elif w in on_stack:
                    low[tag] = min(low[tag], index[w])
            if progressed:
                continue
            # All deps of `tag` processed; settle low-links up the stack.
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[tag])
            if low[tag] == index[tag]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == tag:
                        break
                sccs.append(sorted(comp))
    return sccs


def _layer(nodes: dict[str, _Node],
           sccs: list[list[str]]) -> list[list[list[str]]]:
    """Condense SCCs and assign longest-path layers (layer 0 = leaves;
    layer N depends only on layers < N)."""
    scc_of: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for tag in comp:
            scc_of[tag] = i

    # Condensation out-edges (scc -> set of scc it depends on).
    cond_deps: list[set[int]] = [set() for _ in sccs]
    for i, comp in enumerate(sccs):
        for tag in comp:
            for dep in nodes[tag].deps:
                j = scc_of.get(dep)
                if j is not None and j != i:
                    cond_deps[i].add(j)

    # Longest-path layer via memoised DFS over the acyclic condensation.
    layer_of: dict[int, int] = {}

    def depth(i: int, seen: frozenset[int]) -> int:
        if i in layer_of:
            return layer_of[i]
        if not cond_deps[i]:
            layer_of[i] = 0
            return 0
        d = 1 + max(depth(j, seen | {i}) for j in cond_deps[i])
        layer_of[i] = d
        return d

    for i in range(len(sccs)):
        depth(i, frozenset())

    max_layer = max(layer_of.values(), default=-1)
    buckets: list[list[list[str]]] = [[] for _ in range(max_layer + 1)]
    for i, comp in enumerate(sccs):
        buckets[layer_of[i]].append(comp)
    # Stable order within a layer.
    for b in buckets:
        b.sort(key=lambda comp: comp[0])
    return buckets


# ---------------------------------------------------------------- emit shape

def _node_json(nodes: dict[str, _Node], comp: list[str]) -> dict[str, Any]:
    if len(comp) == 1:
        n = nodes[comp[0]]
        return {
            "type": n.tag,
            "kind": n.kind,
            "defined_in": n.origin(),
            "linked_in": n.linked_in,
            "deps": sorted(n.deps),
        }
    # SCC super-node: members emitted together.
    members = [nodes[t] for t in comp]
    member_set = set(comp)
    ext_deps = sorted(
        {d for m in members for d in m.deps if d not in member_set}
    )
    return {
        "scc": comp,
        "kind": members[0].kind,
        "defined_in": [m.origin() for m in members],
        "linked_in": sorted({m.linked_in for m in members if m.linked_in}) or None,
        "deps": ext_deps,
    }


def compose(analysis_root: Path) -> dict[str, Any]:
    """Build the full DAG dict (scope-agnostic). Importable by orchestrators."""
    nodes = _collect(analysis_root)
    alias_map = _build_typedef_map(nodes, analysis_root)
    _resolve_edges(nodes, alias_map)
    sccs = _tarjan_scc(nodes)
    buckets = _layer(nodes, sccs)
    layers = [[_node_json(nodes, comp) for comp in layer] for layer in buckets]
    n_scc = sum(1 for comp in sccs if len(comp) > 1)
    return {
        "_comment": (
            "Scope-agnostic type dependency DAG built by "
            "compose/types_dag.py. A->B in `deps` means A's wrapper names "
            "B's type. Topo-sorted into layers: layer 0 = leaves; layer N "
            "depends only on layers < N. SCC super-nodes (cyclic type "
            "clusters) carry `scc:[...]` and must be wrapped together. "
            "Scope (port/wrap) is applied by the orchestrator via scope.json, "
            "not here."
        ),
        "stats": {
            "types": len(nodes),
            "edges": sum(len(n.deps) for n in nodes.values()),
            "layers": len(layers),
            "sccs_nontrivial": n_scc,
        },
        "layers": layers,
    }


# ----------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--analysis-root", required=True, type=Path,
                    help="<repo_root>/crustify/analysis")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output path (default: <analysis_root>/types-dag.json). "
                         "Use --stdout to print instead.")
    ap.add_argument("--stdout", action="store_true",
                    help="Print the DAG to stdout instead of writing a file.")
    args = ap.parse_args()

    if not args.analysis_root.is_dir():
        print(f"error: analysis root not found: {args.analysis_root}",
              file=sys.stderr)
        return 2

    dag = compose(args.analysis_root)
    text = json.dumps(dag, indent=2)

    if args.stdout:
        print(text)
    else:
        out = args.out or (args.analysis_root / "types-dag.json")
        out.write_text(text + "\n")
        print(f"[types_dag] {dag['stats']} -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
