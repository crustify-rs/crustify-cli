#!/usr/bin/env python3
"""deps_dag.py — unified dependency DAG over types AND symbols.

Generalises ``types_dag.py``: builds one scope-agnostic directed graph whose
nodes are **types** and **all symbols** (functions / macros / globals,
including a type's ctors / dtor / ops), where ``A -> B`` means "A needs B
emitted first". Topo-sorted (Tarjan SCC → longest-path layers) it drives both
the wrap stage (wrap-scope subset) and the port stage (the whole graph, in
order). See ``docs/WRAP_STAGE_PLAN.md``.

Relationships come straight from the analysis tree:

  - **type → type**   non-scalar ``fields[].type`` (struct layout) PLUS a
                      **cast-centrality** edge from ``casted.{to,from}``: an
                      ``X -> T`` for each ``T in X.casted.to`` that is strictly
                      more cast-central (cast to/from more types) than ``X``.
                      High degree marks the hub — a generic engine erased to by
                      many instances, or a polymorphic base downcast to by many
                      derived — so the edge runs low-degree (instance/derived)
                      UP to the high-degree engine/base it depends on, never the
                      reverse. Strict ``>`` keeps it acyclic (low→high only), so
                      the ambiguous bidirectional cast relation resolves to a
                      correct-direction ordering edge without manufacturing an
                      SCC.
  - **symbol → type / symbol**  every emitted symbol carries a codebase-wide
                      ``depends_on`` (the composer applies no port/wrap shape
                      fork); ``ptr_args``/``ptr_ret`` types fold in as the
                      fallback for symbols without one.

**Callbacks are symbols, not types.** A function-pointer typedef is a
signature — it carries ``ptr_args``/``ptr_ret``, not ``fields[]`` — so it mints
an ordinary ``SymNode`` keyed ``(name, canonical-decl)`` like any other
declaration-only symbol, and gets no special handling here: consumers reach it
through their own ``depends_on.syms``, exactly as they reach a direct callee.
The composer (``syms_manifest._callback_deps``) puts it there, from both the
signature relation and the indirect-call relation. The one place a callback is
still resolved by name is a struct FIELD of function-pointer type, which the
type-side ``fields[].type`` string cannot route on its own (see ``cb_keys``).

**Ops are NOT folded into their types.** Every op (ctor/dtor/up_ref/clone/
locking/method) is its own symbol node; its dependency on its type falls out
of the op's signature (the receiver / return type). Folding ops would inherit
their *body*-level deps onto the type and project the dense function-call
graph onto the types, manufacturing huge artificial SCCs (e.g. the libgit2
ODB/pack subsystem collapsed into one 82-node cycle). Unfolded, the type
graph is acyclic (only field edges) and only genuine recursion remains as
SCCs. The type node still *lists* its ``ops``/``ctors`` so the wrap scheduler
can co-emit a type with its methods — it just doesn't inherit their deps.

Nothing is dropped: external/libc symbols and builtins referenced by
``depends_on.syms`` become symbol nodes too (subkind ``external`` /
``builtin``) so the topo order is complete. The builtin→Rust lowering table
is deferred — builtins are only *tagged* here.

Scope (port/wrap) is NOT stored — it is derived by the orchestrators from
``scope.json`` at schedule time. Read-only, deterministic, no CodeQL.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any

try:                                  # package import (analyze.py)
    from . import scope as _scope
except ImportError:                   # script execution (python3 deps_dag.py)
    import scope as _scope            # type: ignore


def _canonical_decl(declared_in: Any) -> str | None:
    """Pick the canonical declaration file (priority, not list position) via
    ``scope.canonical_decl``; tolerate a str / None / list shape."""
    if isinstance(declared_in, str):
        return declared_in
    if isinstance(declared_in, list) and declared_in:
        return _scope.canonical_decl(declared_in)
    return None


# A symbol node is identified by (name, file): the defining file, or — for a
# declaration-only / external symbol — its canonical declaration file. This
# disambiguates same-named file-local statics (`function_static` /
# `function_inline_tu` / `global_static`) that the old name-only key collapsed.
SymKey = tuple[str, "str | None"]


def _sym_filekey(defined_in: Any, declared_in: Any) -> str | None:
    return defined_in or _canonical_decl(declared_in)


# --------------------------------------------------------------------- model

class TypeNode:
    __slots__ = ("tag", "kind", "defined_in", "declared_in",
                 "ctype_refs", "ops", "ctors", "drop_syms", "dep_types", "dep_syms",
                 "cast_to", "cast_from", "elem_refs", "nfields")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.kind: str | None = None
        self.defined_in: str | None = None
        self.declared_in: str | None = None
        self.ctype_refs: set[str] = set()   # raw C type strings (field types)
        self.ops: set[str] = set()          # folded op/lifecycle symbol names
        self.ctors: list[str] = []
        self.drop_syms: set[str] = set()    # dtor storage/fields fn names (sig-fold only)
        self.dep_types: set[str] = set()    # resolved canonical tags
        self.dep_syms: set[str] = set()     # resolved free-symbol names
        self.cast_to: set[str] = set()      # casted.to tags (this -> T)
        self.cast_from: set[str] = set()    # casted.from tags (T -> this)
        self.elem_refs: set[str] = set()    # array cluster: raw elem type strings
        self.nfields: int = 0               # full struct field count (its port LoC)

    def cast_degree(self) -> int:
        """Cast-graph centrality: how many distinct types this one is cast
        to/from. A generic engine / polymorphic base is a high-degree hub;
        an instance / derived is low-degree. Drives the erasure ordering edge."""
        return len(self.cast_to | self.cast_from)

    def origin(self) -> str | None:
        return self.defined_in or self.declared_in


class SymNode:
    __slots__ = ("name", "file", "kind", "defined_in", "declared_in",
                 "has_dep", "dep_on_types", "dep_on_syms",
                 "sig_type_refs", "subkind", "dep_types", "dep_syms", "loc")

    def __init__(self, name: str, file: str | None) -> None:
        self.name = name
        # The node's identifying file (defining file, or canonical decl file
        # for a decl-only / external symbol). Part of the node key, so
        # same-named file-local statics stay distinct.
        self.file = file
        self.kind: str | None = None
        self.defined_in: str | None = None
        self.declared_in: str | None = None
        # depends_on is unioned only across entries that share this *same*
        # (name, file) key — i.e. the same definition — never across distinct
        # definitions of a colliding name.
        self.has_dep: bool = False
        self.dep_on_types: set[str] = set()       # raw canonical tags (depends_on)
        self.dep_on_syms: set[SymKey] = set()     # (name, file) dep keys
        self.sig_type_refs: set[str] = set()      # raw C type strings (ptr_args/ret)
        self.subkind: str = "symbol"              # symbol|external|builtin
        self.dep_types: set[str] = set()
        self.dep_syms: set[SymKey] = set()        # resolved (name, file) keys
        self.loc: int = 0                         # body line span (port LoC budget)

    def origin(self) -> str | None:
        return self.defined_in or self.declared_in


# ------------------------------------------------------------------- parsing

_DROP_TOKENS = frozenset({
    "const", "volatile", "struct", "union", "enum",
    "unsigned", "signed", "*", "",
})
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")
_SYNTH_MARKERS = frozenset({"(routine)", "(array)", "void"})


def _base_type_name(type_str: str | None) -> str | None:
    """Reduce a C type string to its candidate user-type identifier, or None
    (primitive, void, function pointer, synthetic marker)."""
    if not type_str or type_str in _SYNTH_MARKERS:
        return None
    if "(" in type_str or ")" in type_str:
        return None
    s = re.sub(r"\[.*?\]", "", type_str)
    toks = re.split(r"[\s*]+", s)
    cand = [t for t in toks if t and t not in _DROP_TOKENS]
    if not cand:
        return None
    name = cand[-1]
    return name if _IDENT_RE.match(name) else None


def _is_real(entry: dict, key: str = "name") -> bool:
    v = entry.get(key) or entry.get("type")  # `type` fallback: un-migrated records
    # Reject empty and anonymous type tags (`(unnamed struct/union/enum)`):
    # the latter share one synthetic name across hundreds of distinct
    # file-local definitions, are not referenceable by name (a field of anon
    # type carries the `(unnamed …)` string, which `_base_type_name` already
    # drops), so as nodes they are pure collision noise.
    #
    # `_`-prefixed names are NOT rejected: those are real reserved-namespace
    # system/glibc entities (`__S_IFDIR`, `__ctype_b_loc`, `__bswap_32`, …)
    # that the composer emits with proper kinds in the on-disk manifests.
    # Excluding them only mislabelled them as `subkind: external` leaves even
    # though their kind is known. (Compiler builtins `__builtin_*` are never
    # emitted to a manifest, so they still mint as `subkind: builtin` leaves.)
    return bool(v) and not str(v).startswith("(")


def _field_ctype_refs(entry: dict) -> set[str]:
    refs: set[str] = set()
    for fld in entry.get("fields") or []:
        t = fld.get("type")
        if t:
            refs.add(t)
    return refs


def _sig_type_refs(entry: dict) -> set[str]:
    """User-type refs from a symbol's signature (ptr_args + ptr_ret)."""
    refs: set[str] = set()
    for a in entry.get("ptr_args") or []:
        t = a.get("type")
        if t:
            refs.add(t)
    pr = entry.get("ptr_ret")
    if isinstance(pr, dict) and pr.get("type"):
        refs.add(pr["type"])
    return refs


# ------------------------------------------------------------------- collect

def _collect(analysis_root: Path):
    types: dict[str, TypeNode] = {}
    syms: dict[SymKey, SymNode] = {}

    for f in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get("types", []):
            if not _is_real(e):
                continue
            tag = e.get("name") or e["type"]
            n = types.get(tag) or types.setdefault(tag, TypeNode(tag))
            if n.kind is None:
                n.kind = e.get("kind")
            if n.defined_in is None:
                n.defined_in = e.get("defined_in")
            if n.declared_in is None:
                dh = e.get("declared_in")
                n.declared_in = dh[0] if isinstance(dh, list) and dh else (
                    dh if isinstance(dh, str) else None)
            n.ctype_refs |= _field_ctype_refs(e)
            # Array clusters are fieldless but carry `elems` — the concrete
            # element types their typed CVec<T> aliases reference. The generic
            # `CVec<T, S>` does NOT depend on T (the strategy frees by byte
            # length, never touching elements), and a synthetic cluster has no
            # natural dependents, so the cluster is a foundational LEAF. The
            # alias→elem coupling is therefore INVERTED below: the elem depends
            # on the cluster (so the cluster wraps first), and the cluster's
            # alias renders the elem raw (`ffi::T`) as a `fallback`, back-filled
            # once the elem lands. Stash elems apart from field refs for that
            # inversion pass. (Strings have no elems.)
            n.elem_refs |= {r["type"] for r in (e.get("elems") or [])
                            if isinstance(r, dict) and r.get("type")}
            cst = e.get("casted") or {}
            if isinstance(cst, dict):
                n.cast_to |= {t for t in (cst.get("to") or []) if t}
                n.cast_from |= {t for t in (cst.get("from") or []) if t}
            # Bundle op-set is exactly the analyzer's `ops` (proven to already
            # contain every genuine ctor/dtor/up_ref/clone). Generic lifetime
            # primitives — e.g. a shared `git_mutex_lock` named only in the
            # `locking` block — are deliberately NOT folded in: the op whose
            # body uses the primitive already carries the edge (that is the
            # rule that fills `locking`), so the bundle depends on it
            # naturally. `locking` stays type metadata for the agent.
            # A concrete type's method surface is DERIVED — lifecycle only (no
            # stored `ops`, no field accessors); synthetic clusters use their
            # explicit `ops`. type_method_syms() unifies both.
            n.ops |= set(_scope.type_method_syms(e))
            for c in _scope.alloc_fns(_scope.lifetime(e)):
                if c not in n.ctors:
                    n.ctors.append(c)
            # Dtor sym names, for the signature-fold only (NOT the owned method
            # surface). A concrete type's dtor is already in `ops` via
            # type_method_syms; a synthetic cluster's is NOT (its `ops` is the
            # explicit op list, sans ctor/dtor) — so its dtor's signature types
            # (e.g. `git_pool_clear(git_pool*)`) would otherwise never fold in.
            n.drop_syms |= set(_scope.drop_op_names(_scope.type_dropped_by(e)))

    for f in sorted(analysis_root.rglob("syms.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get("symbols", []):
            if not _is_real(e, "name"):
                continue
            name = e["name"]
            key: SymKey = (name, _sym_filekey(e.get("defined_in"),
                                              e.get("declared_in")))
            n = syms.get(key) or syms.setdefault(key, SymNode(name, key[1]))
            if n.kind is None:
                n.kind = e.get("kind")
            if n.defined_in is None:
                n.defined_in = e.get("defined_in")
            if n.declared_in is None:
                n.declared_in = _canonical_decl(e.get("declared_in"))
            if e.get("loc"):
                n.loc = max(n.loc, int(e["loc"]))
            if "depends_on" in e:
                n.has_dep = True
                dep = e["depends_on"] or {}
                for d in dep.get("types") or []:
                    if d.get("type"):
                        n.dep_on_types.add(d["type"])
                for d in dep.get("syms") or []:
                    if d.get("name"):
                        n.dep_on_syms.add(
                            (d["name"], _sym_filekey(d.get("defined_in"),
                                                     d.get("declared_in"))))
            n.sig_type_refs |= _sig_type_refs(e)
    return types, syms


def _alias_map(analysis_root: Path, types: dict[str, TypeNode]) -> dict[str, str]:
    """typedef alias -> canonical tag (+ identity), for C-string resolution."""
    amap: dict[str, str] = {t: t for t in types}
    for f in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get("types", []):
            if not _is_real(e):
                continue
            for alias in e.get("typedef") or []:
                amap.setdefault(alias, e.get("name") or e["type"])
    return amap


# -------------------------------------------------------------- edge building

def _resolve_ctype(ref: str, amap: dict[str, str],
                   types: dict[str, TypeNode]) -> str | None:
    """A C type string OR a bare tag -> canonical in-universe tag, else None."""
    if ref in types:
        return ref
    name = _base_type_name(ref) if re.search(r"[\s*\[\]]", ref) else ref
    if not name:
        return None
    return amap.get(name)


def _build_edges(types, syms, amap):
    """Fill dep_types / dep_syms on every node; return external sym/type names
    discovered (so we can mint nodes for them).

    Ops are **not folded** into their types: every symbol (functions / macros
    / globals — including a type's ctors / dtor / ops) is its own node. A type
    depends only on its non-scalar **field** types; an op's dependency on its
    type falls out naturally from the op's **signature** (receiver / return
    type). This keeps the type graph acyclic and leaves only genuine
    recursion as SCCs. The type node still *lists* its ops (for the wrap
    scheduler to co-emit a type + its methods), but doesn't inherit their
    deps.
    """
    ext_syms: dict[SymKey, str] = {}   # (name, file) -> subkind (external|builtin)
    ext_types: set[str] = set()

    # name -> all in-tree (name, file) keys, for resolving a dep whose own
    # file is missing/ambiguous (over-approximate only in that rare case).
    syms_by_name: dict[str, list[SymKey]] = {}
    for key in syms:
        syms_by_name.setdefault(key[0], []).append(key)

    def classify_ext(name: str) -> str:
        return "builtin" if name.startswith("__builtin") else "external"

    # Callback name -> its symbol key, for resolving a struct FIELD whose type
    # is a function-pointer typedef (`OSSL_FUNC_cipher_update_fn *cupdate;`).
    # `_resolve_ctype` cannot: a callback is a symbol, so it is neither in
    # `types` nor in the type-alias map, and the ref would resolve to None and
    # be dropped. This is the type-side counterpart of the composer's
    # `_callback_deps` — on the symbol side the composer already emits the
    # callback under `depends_on.syms`, so nothing here special-cases it.
    cb_keys: dict[str, SymKey] = {
        key[0]: key for key, n in syms.items() if n.kind == "callback"
    }

    def res_type_tag(tag: str, dt: set[str]):
        """A depends_on.types tag (already canonical) -> node."""
        if tag in types:
            dt.add(tag)
        elif tag:
            ext_types.add(tag)
            dt.add(tag)

    def res_ctype(ref: str, dt: set[str], ds: set[SymKey] | None = None):
        # Checked before `_resolve_ctype` — see `cb_keys`.
        if ds is not None:
            cb = cb_keys.get(ref) or cb_keys.get(_base_type_name(ref) or "")
            if cb is not None:
                ds.add(cb)
                return
        t = _resolve_ctype(ref, amap, types)
        if t is None:
            return
        if t in types:
            dt.add(t)
        else:
            ext_types.add(t)
            dt.add(t)

    def res_sym(depkey: SymKey, dt: set[str], ds: set[SymKey]):
        name = depkey[0]
        if name in types:                # name collides with a type tag (C's
            dt.add(name)                 # separate namespaces) -> the type node
            return
        if depkey in syms:               # exact (name, file) match
            ds.add(depkey)
            return
        cands = syms_by_name.get(name)   # same name, different/absent dep file
        if cands:
            if len(cands) == 1:          # unambiguous in-tree symbol
                ds.add(cands[0])
            else:                        # file-less ambiguous dep -> over-approx
                ds.update(cands)
            return
        ext_syms[depkey] = classify_ext(name)   # external / libc / builtin
        ds.add(depkey)

    # Types: their non-scalar field types PLUS the
    # **signature** type-refs of their ops/ctors/dtor — the types a wrapper's
    # method signatures mention, which must be wrapped first (`from_str(&mut
    # GitStr)`; `Drop` calling `git_pool_clear(git_pool*)`). Body-level op deps
    # stay unfolded (they cause the dense call-graph SCCs); signatures are sparse.
    # `wedge[(T1,T2)]` keeps the reference multiplicity so the genuine cycles
    # (parent↔child backrefs) can be flattened by weighted feedback-arc-set, not
    # collapsed into a co-scheduled blob.
    name2syms: dict[str, list] = collections.defaultdict(list)
    for skey, sn in syms.items():
        name2syms[sn.name].append(sn)
    wedge: dict[tuple[str, str], int] = collections.defaultdict(int)
    for tag, n in types.items():
        for ref in n.ctype_refs:                     # field refs (hard layout)
            t = _resolve_ctype(ref, amap, types)
            # `n.dep_syms` passed so a field of function-pointer-typedef type
            # lands on the callback's SYMBOL node. This edge is load-bearing:
            # a function that invokes a callback it reached through a struct
            # field never names the typedef in its own signature, so its
            # ordering runs struct -> callback, and it depends on the struct.
            res_ctype(ref, n.dep_types, n.dep_syms)
            if t in types and t != tag:
                wedge[(tag, t)] += 1
        for opname in (set(n.ops) | set(n.ctors) | n.drop_syms):   # op/ctor/dtor SIGNATURE refs
            for sn in name2syms.get(opname, ()):
                for ref in sn.sig_type_refs:
                    t = _resolve_ctype(ref, amap, types)
                    if t in types and t != tag:
                        n.dep_types.add(t)
                        wedge[(tag, t)] += 1
        # Cast-graph ordering edge (classifies the otherwise-ambiguous `casted`
        # relation into a correct-direction dep). For each T this type is cast
        # TO, add `tag -> T` ONLY when T is strictly more cast-central — i.e. T
        # is cast to/from more types than `tag`. High cast-degree marks the hub:
        # a generic engine erased to by many instances, or a polymorphic base
        # downcast to by many derived. So the edge always points from the
        # low-degree instance/derived UP to the high-degree engine/base it
        # depends on — never the reverse — and the strict `>` keeps it acyclic
        # (edges run low-degree → high-degree only, so no cast cycle can form).
        my_deg = n.cast_degree()
        for t in n.cast_to:
            tn = types.get(t)
            if tn is not None and t != tag and tn.cast_degree() > my_deg:
                n.dep_types.add(t)
                wedge[(tag, t)] += 1
        n.dep_types.discard(tag)         # no self-edge
        wedge.pop((tag, tag), None)

    # Array-cluster element inversion. A typed `CVec<T>` alias on cluster A
    # references element wrapper T, but the cluster is a foundational leaf (the
    # generic container is T-agnostic; the synthetic tag has no dependents).
    # Folding A->T forward would sink the allocator below the transitive closure
    # of every type ever heap-allocated. Instead invert: emit T->A (the element
    # depends on the cluster, so A wraps first) and hand back (A, T) as a forced
    # back-edge — A's alias renders T raw (`fallback`), and T back-fills it once
    # T's wrapper lands, strictly after A's module is complete (no same-wave
    # writer race on A's file).
    forced_back: list[tuple[str, str]] = []
    for tag, n in types.items():
        for ref in n.elem_refs:
            t = _resolve_ctype(ref, amap, types)
            if t in types and t != tag:
                types[t].dep_types.add(tag)      # elem -> cluster (layering)
                wedge[(t, tag)] += 1
                forced_back.append((tag, t))      # cluster -> elem (fallback)

    # Symbols (ALL — ops and callbacks included, never folded). Two sources:
    #   - `depends_on` (types + syms) — codebase-wide, no port/wrap shape fork
    #   - `ptr_args`/`ptr_ret` signature pointer types
    # `depends_on` is authoritative when present (it already unions the
    # signature types, and carries the by-value + callback identity that
    # `ptr_args` collapses to `(routine)`). The `sig_type_refs` fold-in is then
    # redundant, but it is the SOLE source for symbols carrying no
    # `depends_on` at all (function-like macros with no typed signature).
    # Unioning all sources can only add a genuine edge, never drop one.
    for key, n in syms.items():
        if n.name in types:              # collision -> represented by the type
            continue
        for t in n.dep_on_types:
            res_type_tag(t, n.dep_types)
        for dk in n.dep_on_syms:
            res_sym(dk, n.dep_types, n.dep_syms)
        for ref in n.sig_type_refs:
            res_ctype(ref, n.dep_types, n.dep_syms)
        n.dep_syms.discard(key)          # no self-edge

    return ext_syms, ext_types, dict(wedge), forced_back


# ------------------------------------------------------- node id + adjacency

def _tid(tag: str) -> str:
    return "t:" + tag


def _sid(key: SymKey) -> str:
    # Graph id encodes (name, file) so same-named file-local statics are
    # distinct nodes; the emitted ``id`` stays the bare name (+ ``defined_in``).
    name, file = key
    return "s:" + name + "\x00" + (file or "")


def _build_graph(types, syms, ext_syms, ext_types):
    """Return (nodes, adj): nodes id -> record dict; adj id -> set(dep ids).
    Every type and every symbol is a node (ops are not folded). Type nodes
    still carry their ``ops``/``ctors`` lists so the wrap scheduler can
    co-emit a type with its methods."""
    nodes: dict[str, dict] = {}
    adj: dict[str, set[str]] = {}

    # name -> in-tree defining files, to resolve each op to its symbol node's
    # (name, defined_in) key. Same-named statics in different files keep the
    # scheduler from mis-binding an op; an ambiguous/absent name yields a null
    # `defined_in` (scheduler falls back to name-only).
    op_files: dict[str, list[str]] = {}
    for (nm, fl) in syms:
        op_files.setdefault(nm, []).append(fl)

    def _op_objs(names: set[str]) -> list[dict]:
        out = []
        for nm in sorted(names):
            fls = op_files.get(nm)
            out.append({"name": nm,
                        "defined_in": fls[0] if fls and len(fls) == 1 else None})
        return out

    def add(nid, rec):
        nodes[nid] = rec
        adj.setdefault(nid, set())

    for tag, n in types.items():
        # `ops`/`ctors` are kept for scheduler grouping (which symbol nodes
        # are this type's methods). Ops carry `defined_in` so the scheduler
        # binds the right same-named static (mirrors `deps.syms`).
        add(_tid(tag), {
            "id": tag, "node_kind": "type", "subkind": n.kind,
            "defined_in": n.origin(),
            "ops": _op_objs(n.ops),
            "ctors": list(n.ctors),
            "loc": n.nfields,           # struct field count (a type's own LoC)
            "_dt": n.dep_types, "_ds": n.dep_syms,
        })
    for key, n in syms.items():
        if n.name in types:              # collision -> represented by the type
            continue
        add(_sid(key), {
            "id": n.name, "node_kind": "symbol", "subkind": n.kind or "symbol",
            "defined_in": n.origin(), "loc": n.loc,
            "_dt": n.dep_types, "_ds": n.dep_syms,
        })
    for tag in ext_types:
        if _tid(tag) not in nodes:
            add(_tid(tag), {"id": tag, "node_kind": "type", "subkind": "external",
                            "defined_in": None,
                            "ops": [], "ctors": [], "_dt": set(), "_ds": set()})
    for key, sub in ext_syms.items():
        if _sid(key) not in nodes:
            add(_sid(key), {"id": key[0], "node_kind": "symbol", "subkind": sub,
                            "defined_in": key[1],
                            "_dt": set(), "_ds": set()})

    for nid, rec in nodes.items():
        for tag in rec.pop("_dt"):
            if _tid(tag) in nodes:
                adj[nid].add(_tid(tag))
        for skey in rec.pop("_ds"):
            if _sid(skey) in nodes:
                adj[nid].add(_sid(skey))
    return nodes, adj


# ------------------------------------------------------- Tarjan SCC + layers

def _tarjan(adj: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in adj:
        if root in index:
            continue
        work = [(root, sorted(adj[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            tag, deps = work[-1]
            advanced = False
            while deps:
                w = deps.pop(0)
                if w not in adj:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, sorted(adj[w])))
                    advanced = True
                    break
                if w in on:
                    low[tag] = min(low[tag], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[tag])
            if low[tag] == index[tag]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == tag:
                        break
                out.append(sorted(comp))
    return out


def _layer(adj, sccs):
    scc_of = {nid: i for i, comp in enumerate(sccs) for nid in comp}
    cond = [set() for _ in sccs]
    for i, comp in enumerate(sccs):
        for nid in comp:
            for dep in adj[nid]:
                j = scc_of.get(dep)
                if j is not None and j != i:
                    cond[i].add(j)
    memo: dict[int, int] = {}

    def depth(i):
        if i in memo:
            return memo[i]
        memo[i] = 0
        memo[i] = (1 + max((depth(j) for j in cond[i]), default=-1)) if cond[i] else 0
        return memo[i]

    for i in range(len(sccs)):
        depth(i)
    maxl = max(memo.values(), default=-1)
    buckets = [[] for _ in range(maxl + 1)]
    for i, comp in enumerate(sccs):
        buckets[memo[i]].append(comp)
    for b in buckets:
        b.sort(key=lambda c: c[0])
    return buckets


# --------------------------------------------- weighted feedback-arc-set break

def _break_sccs(sccs, adj, nodes, wedge) -> set[tuple[str, str]]:
    """Flatten each non-trivial SCC with a **weighted feedback-arc-set**: order
    its members so the heaviest dependencies are satisfied (the more-referenced
    type is emitted first), and return the minimal set of **back-edges** — the
    weaker reverse references that the wrap stage must render as raw `ffi::T`
    (they point at a not-yet-wrapped cycle sibling).

    The order is the greedy net-weight heuristic (a node depended-upon more than
    it depends is placed earlier); an edge ``u -> v`` (``u`` needs ``v``) is a
    back-edge when ``v`` ends up *after* ``u``."""
    def w(u: str, v: str) -> int:
        if u.startswith("t:") and v.startswith("t:"):
            return wedge.get((nodes[u]["id"], nodes[v]["id"]), 1)
        return 1

    back: set[tuple[str, str]] = set()
    for comp in sccs:
        if len(comp) < 2:
            continue
        s = set(comp)
        inw = collections.defaultdict(int)
        outw = collections.defaultdict(int)
        for u in comp:
            for v in adj[u] & s:
                ww = w(u, v)
                outw[u] += ww
                inw[v] += ww
        # depended-upon (high in-weight) first; tie-break stable by id.
        order = sorted(comp, key=lambda x: (inw[x] - outw[x], x), reverse=True)
        pos = {nid: i for i, nid in enumerate(order)}
        for u in comp:
            for v in adj[u] & s:
                if pos[v] > pos[u]:          # dep emitted AFTER its user → cut
                    back.add((u, v))
    return back


# ---------------------------------------------------------------- emit

def _emit_node(nodes, comp):
    def grouped(ids):
        types = sorted(nodes[d]["id"] for d in ids if d.startswith("t:"))
        syms = sorted(nodes[d]["id"] for d in ids if d.startswith("s:"))
        return {"types": types, "syms": syms}

    if len(comp) == 1:
        rec = dict(nodes[comp[0]])
        out = {"id": rec["id"], "node_kind": rec["node_kind"],
               "subkind": rec["subkind"], "defined_in": rec["defined_in"]}
        if rec["node_kind"] == "type":
            out["ops"] = rec.get("ops", [])
            out["ctors"] = rec.get("ctors", [])
            if rec.get("loc"):
                out["loc"] = rec["loc"]
        elif rec.get("loc"):
            out["loc"] = rec["loc"]
        return out  # deps filled by caller
    def member(m):
        d = {"id": m["id"], "node_kind": m["node_kind"], "subkind": str(m["subkind"]),
             "defined_in": m.get("defined_in")}
        if m["node_kind"] == "type":
            d["ops"] = m.get("ops", [])
            d["ctors"] = m.get("ctors", [])
            if m.get("loc"):
                d["loc"] = m["loc"]
        elif m.get("loc"):
            d["loc"] = m["loc"]
        return d
    return {"scc": [member(nodes[c]) for c in comp]}


def _populate_nfields(analysis_root: Path, types: dict[str, TypeNode]) -> None:
    """Set each type's ``nfields`` to its **full** struct field count from the
    T1 ``fields.csv`` (``<crustify>/codeql/t1/fields.csv``, a sibling of the
    analysis tree). This is the whole struct, NOT the port-accessed subset that
    ``types.json``'s ``fields[]`` narrows to — a struct's translated surface
    (``define_type!`` + accessors) scales with its field layout, so a type's
    own LoC is its field count. fields.csv attributes anonymous-struct fields to
    the naming typedef, so ``struct_name`` matches the type tag. Missing CSV →
    every ``nfields`` stays 0 (still deterministic, no CodeQL)."""
    fcsv = analysis_root.parent / "codeql" / "t1" / "fields.csv"
    if not fcsv.is_file():
        return
    by_key: dict[tuple[str, str], int] = collections.Counter()
    by_name: dict[str, int] = collections.Counter()
    for row in _scope.load_csv(fcsv):
        sn = row.get("struct_name") or ""
        if not sn:
            continue
        by_key[(sn, row.get("struct_def_file") or "")] += 1
        by_name[sn] += 1
    for tag, n in types.items():
        cnt = by_key.get((tag, n.defined_in or ""))
        if cnt is None:
            cnt = by_key.get((tag, n.declared_in or ""))
        n.nfields = cnt if cnt is not None else by_name.get(tag, 0)


def compose(analysis_root: Path) -> dict[str, Any]:
    types, syms = _collect(analysis_root)
    _populate_nfields(analysis_root, types)
    amap = _alias_map(analysis_root, types)
    ext_syms, ext_types, wedge, forced_back = _build_edges(types, syms, amap)
    nodes, adj = _build_graph(types, syms, ext_syms, ext_types)
    sccs = _tarjan(adj)
    # Flatten genuine cycles (parent↔child backrefs) with a weighted FAS: the
    # back-edges become per-node `fallback` deps (the wrap stage renders those as
    # raw `ffi::T` — their target is a not-yet-wrapped cycle sibling), and the
    # remaining graph is acyclic, so every node layers individually.
    back = _break_sccs(sccs, adj, nodes, wedge)
    # Inject the array-cluster alias inversions as forced back-edges (cluster ->
    # elem): the elem -> cluster layering edge already lives in `adj` (from
    # dep_types), so the cluster stays a leaf while its alias renders the elem
    # raw + back-fills. Injected AFTER FAS so it never manufactures a spurious
    # SCC for the cycle breaker to arbitrate.
    for a_tag, e_tag in forced_back:
        a_id, e_id = _tid(a_tag), _tid(e_tag)
        if a_id in adj and e_id in nodes:
            adj[a_id].add(e_id)
            back.add((a_id, e_id))
    n_cyclic = sum(1 for c in sccs if len(c) > 1)
    adj_dag = {nid: {d for d in deps if (nid, d) not in back}
               for nid, deps in adj.items()}
    sccs = _tarjan(adj_dag)
    buckets = _layer(adj_dag, sccs)

    def _grp(ids):
        # Symbol deps carry `defined_in` so a same-named collision is
        # unambiguous (consumers key on (name, defined_in)); type deps stay bare.
        sym = [{"name": nodes[d]["id"], "defined_in": nodes[d]["defined_in"]}
               for d in ids if d.startswith("s:")]
        sym.sort(key=lambda x: (x["name"], x["defined_in"] or ""))
        return {"types": sorted(nodes[d]["id"] for d in ids if d.startswith("t:")),
                "syms": sym}

    # Reverse of the cut back-edges: for a node `v`, who referenced it raw while
    # it was a not-yet-wrapped cycle sibling. Once `v` is wrapped, those users
    # (`back_fill`) switch their `ffi::v` to the wrapper — the work order the
    # depended-upon type's wrap job carries.
    rev_back: dict[str, set] = collections.defaultdict(set)
    for u, v in back:
        rev_back[v].add(u)

    layers = []
    for layer in buckets:
        emitted = []
        for comp in layer:                 # singletons — adj_dag is acyclic
            rec = _emit_node(nodes, comp)
            internal = set(comp)
            fwd, fbk, bfl = set(), set(), set()
            for nid in comp:
                fwd |= (adj_dag[nid] - internal)
                fbk |= {d for d in adj[nid] if (nid, d) in back}
                bfl |= rev_back.get(nid, set())
            rec["deps"] = _grp(fwd)
            if fbk:
                rec["fallback"] = _grp(fbk)
            if bfl - internal:
                rec["back_fill"] = _grp(bfl - internal)
            emitted.append(rec)
        layers.append(emitted)

    n_types = sum(1 for r in nodes.values() if r["node_kind"] == "type")
    n_syms = len(nodes) - n_types
    return {
        "_comment": (
            "Scope-agnostic unified dependency DAG (types + symbols) by "
            "compose/deps_dag.py. A's `deps` are emitted before A. A type "
            "depends on its non-scalar field types AND its ops'/ctors' "
            "**signature** types (what the wrapper's method signatures need); "
            "body-level op deps stay unfolded (a type lists its ops as "
            "{name, defined_in} for the scheduler but doesn't inherit their "
            "call-graph deps). Genuine cycles (parent↔child backrefs) are "
            "flattened by weighted feedback-arc-set: the resulting graph is "
            "acyclic (no `scc:[...]` super-nodes), and a node's `fallback` "
            "(when present) lists the cycle back-edges its wrapper must render "
            "as raw `ffi::T` because their target is a not-yet-wrapped sibling; "
            "`back_fill` (the reverse) lists nodes that already render *this* "
            "type raw and should switch to its wrapper once it lands. "
            "Symbols are keyed by (name, defined_in|canonical-decl), so "
            "`deps.syms`/`fallback.syms` carry `defined_in` to disambiguate "
            "same-named statics; type deps stay bare tags. Topo-layered: layer "
            "0 = leaves; layer N depends only on layers < N. Scope is applied by "
            "the orchestrators via scope.json, not here."
        ),
        "stats": {
            "nodes": len(nodes), "types": n_types, "symbols": n_syms,
            "external_syms": len(ext_syms), "external_types": len(ext_types),
            "edges": sum(len(v) for v in adj.values()),
            "layers": len(layers),
            "sccs_flattened": n_cyclic,
            "fallback_edges": len(back),
        },
        "layers": layers,
    }


# ----------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis-root", required=True, type=Path,
                    help="<repo_root>/crustify/analysis")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <analysis_root>/deps-dag.json")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()
    if not args.analysis_root.is_dir():
        print(f"error: analysis root not found: {args.analysis_root}", file=sys.stderr)
        return 2
    dag = compose(args.analysis_root)
    text = json.dumps(dag, indent=2)
    if args.stdout:
        print(text)
    else:
        out = args.out or (args.analysis_root / "deps-dag.json")
        out.write_text(text + "\n")
        print(f"[deps_dag] {dag['stats']} -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
