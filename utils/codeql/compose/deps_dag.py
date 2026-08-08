#!/usr/bin/env python3
"""deps_dag.py — unified dependency DAG over types AND symbols.

Builds one scope-agnostic directed graph whose
nodes are **types** and **all symbols** (functions / macros / globals,
including a type's lifecycle methods), where ``A -> B`` means "A needs B
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
SCCs.

**No lifecycle is stored here at all** — not the op list, not their signature
types. A type's method surface is reverse-derived from the acting symbols'
``lifetime``, an LLM-submitted judgement field. Folding op signatures into a
type's deps made the LAYERING a function of agent output (one dropper
declaration moved 1,021 nodes by a layer), and merely *listing* the ops made the
FILE one. Both are gone: ``deps-dag.json`` is a pure function of the C —
field types, signature types, ``depends_on`` and the cast graph, every one
composer-derived from CodeQL — so the same tree always yields the same bytes.
The wrap scheduler still co-emits a type with its methods; it reverse-derives
them from the analysis tree at schedule time (``_schedule.load_type_meta`` ->
``ordered_ops``), which also means a submission takes effect on the next wave
with no recompose in between.

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
    from . import macro_families as _mf
except ImportError:                   # script execution (python3 deps_dag.py)
    import scope as _scope            # type: ignore
    import macro_families as _mf      # type: ignore


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
    __slots__ = ("tag", "def_file", "kind", "defined_in", "declared_in",
                 "ctype_refs", "dep_types", "dep_syms",
                 "cast_to", "cast_from", "nfields", "generated_by")

    def __init__(self, tag: str, def_file: str = "") -> None:
        self.tag = tag
        self.def_file = def_file
        self.kind: str | None = None
        self.defined_in: str | None = None
        self.declared_in: str | None = None
        self.ctype_refs: set = set()        # (type_name, type_def_file) field edges
        self.dep_types: set[str] = set()    # resolved canonical tags
        self.dep_syms: set[str] = set()     # resolved free-symbol names
        self.cast_to: set[str] = set()      # casted.to tags (this -> T)
        self.cast_from: set[str] = set()    # casted.from tags (T -> this)
        self.nfields: int = 0               # full struct field count (its port LoC)
        self.generated_by: str | None = None  # the macro that minted it, if any

    def cast_degree(self) -> int:
        """Cast-graph centrality: how many distinct types this one is cast
        to/from. A generic engine / polymorphic base is a high-degree hub;
        an instance / derived is low-degree. Drives the erasure ordering edge."""
        return len(self.cast_to | self.cast_from)

    @property
    def key(self) -> "TypeKey":
        return (self.tag, self.def_file)

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

    @property
    def key(self) -> "TypeKey":
        return (self.tag, self.def_file)

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


def _field_ctype_refs(entry: dict, keep_fields: set[str] | None = None) -> set[str]:
    """Type refs from a struct's fields.

    ``keep_fields`` restricts to the named fields — the port-touched subset
    for a wrap-scope struct, whose other fields are layout bound opaquely and
    must not order this target's work. None keeps every field (port scope).
    """
    refs: set[str] = set()
    for fld in entry.get("fields") or []:
        if keep_fields is not None and fld.get("name") not in keep_fields:
            continue
        t = fld.get("type")
        if t:
            refs.add(t)
        # A bare function-pointer field renders as `..(*)(..)`, which names
        # nothing — the composer puts the user types from its SIGNATURE in
        # `sig_types` instead. Without these a vtable struct (`bio_method_st`
        # and friends) is a dependency leaf even though its slots traffic in
        # `BIO`, `BIO_info_cb`, … and would schedule before them.
        refs.update(fld.get("sig_types") or [])
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

def _entries_of(src, kind: str) -> list:
    """Entries of ``kind`` from either a composed ``(types, syms)`` pair or a
    legacy analysis-root path.

    The pair is the live form -- records composed from the CodeQL tables and
    overlaid with `ownership-store.json` by :mod:`crustify.manifests`, with no
    per-stem tree to walk. The path form stays for this module's own CLI.
    """
    if isinstance(src, tuple):
        return list(src[0] if kind == "types" else src[1])
    root = Path(src)
    fname = "types.json" if kind == "types" else "syms.json"
    key = "types" if kind == "types" else "symbols"
    out: list = []
    for f in sorted(root.rglob(fname)):
        try:
            out += json.loads(f.read_text()).get(key) or []
        except Exception:
            continue
    return out


def _collect(analysis_root: Path,
             port_syms: set | None = None,
             port_fields: dict[str, set[str]] | None = None,
             codeql_dir: Path | None = None,
             in_scope_types: set | None = None):
    """Collect nodes/edges from the analysis tree, narrowed to one target.

    The tree is scope-agnostic and ACCUMULATES across targets: an entry that
    was port-scope for an earlier target keeps the full body-level
    ``depends_on`` it gained then. Building this target's graph from that
    unfiltered tree imports another target's body edges, which deepens the
    layering for work this target never does.

    So edges are narrowed per node against THIS target's scope:

      - **port-scope symbol** — every edge. Its body is translated, so its
        callees and field-derived type uses are real dependencies.
      - **wrap-scope symbol** — signature only. A binding is emitted from the
        signature alone: callee edges are dropped outright, and type edges
        keep only signature/opaque uses (``fields: []``).
      - **wrap-scope struct** — only fields the port scope actually touches
        (``port_fields``); the rest of the layout is never reached through
        this target and must not order its work.

    ``port_syms`` of None disables narrowing (whole-tree graph, the old
    behaviour) — used by callers with no scope in hand.
    """
    types: dict[str, TypeNode] = {}
    syms: dict[SymKey, SymNode] = {}

    tmeta, tedges, tcasts, talias, tgen = (collect_types_csv(codeql_dir) if codeql_dir
                                     else ({}, {}, {}, {}, {}))
    for key, m in tmeta.items():
        tag, df = key
        if tag.startswith("(unnamed"):
            continue                       # the anonymous sentinel is not a type
        # The CSVs carry every type in the DB; the graph is per TARGET. Keep the
        # scope's types only — reading the analysis tree used to narrow this
        # implicitly, since the composer only ever emitted in-scope entries.
        if in_scope_types is not None and not (
                (tag, df) in in_scope_types or tag in in_scope_types):
            continue
        n = types.setdefault(key, TypeNode(tag, df))
        # A node's kind is what the type IS, not how it was spelled. T1's `kind`
        # column is the raw C form, so shape A (`typedef struct {…} T;`) reads
        # `typedef` there while `unaliased_kind` carries the aggregate. The
        # manifest this collection replaced reported the RESOLVED kind, and
        # every consumer was written against that: `agents/wrap._PROMPT_BY_KIND`
        # dispatches on `node.subkind` and accepts only struct/union/enum, so
        # emitting the raw kind made 411 of 727 type nodes undispatchable -- a
        # whole wave died with `unsupported manifest kind 'typedef'`. Resolve
        # through `uak` so a node agrees with `query types --name`.
        _k = m["kind"]
        if _k == "typedef" and (m.get("uak") or "") in _AGGREGATE_UAK:
            _k = m["uak"].split("_")[0]        # struct_anonymous -> struct
        n.kind = n.kind or _k
        n.generated_by = n.generated_by or tgen.get(key)
        n.defined_in = n.defined_in or (df or None)
        n.declared_in = n.declared_in or _scope.canonical_decl(sorted(m["decls"])) if m["decls"] else n.declared_in
        # A wrap struct only orders work through the fields the port scope
        # actually reads; the rest is layout it binds opaquely. `port_fields`
        # is keyed by TAG (it comes from `depends_on.types[].fields`, which
        # names no file), so a colliding tag pools both entities' touched sets
        # -- which can only over-keep a field, never drop one.
        keep = None if port_fields is None else port_fields.get(tag, set())
        for fname, tname, tdf in tedges.get(key, ()):
            if keep is not None and fname not in keep:
                continue
            n.ctype_refs.add((tname, tdf))   # BOTH ends identified at source
        # `casts.csv` is tag-keyed (a cast names no file), so a colliding tag
        # shares its cast set across entities — over-including an ordering edge,
        # never dropping one.
        to, frm = tcasts.get(tag, (set(), set()))
        n.cast_to |= to
        n.cast_from |= frm

    if True:
        for e in _entries_of(analysis_root, "symbols"):
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
                # `depends_on` is only ever emitted for an entry that was
                # port-scope for SOME target; whether it is port-scope for
                # THIS one decides how much of it applies.
                is_port = port_syms is None or key in port_syms
                for d in dep.get("types") or []:
                    if not d.get("type"):
                        continue
                    # A wrap node keeps signature/opaque uses (fields: []) and
                    # drops field-derived ones: it binds the type, never reads it.
                    if is_port or not d.get("fields"):
                        n.dep_on_types.add(d["type"])
                if is_port:
                    for d in dep.get("syms") or []:
                        if d.get("name"):
                            n.dep_on_syms.add(
                                (d["name"], _sym_filekey(d.get("defined_in"),
                                                         d.get("declared_in"))))
            n.sig_type_refs |= _sig_type_refs(e)
    return types, syms, talias


# A type entity is (tag, def_file). A tag ALONE does not identify a type: 18 tags
# in OpenSSL have several definitions, and the two `ring_buf`s / two
# `ossl_record_layer_st`s are unrelated structs with disjoint layouts.
TypeKey = tuple[str, str]

#: Translation-unit suffixes. A type DEFINED in one has no linkage past that
#: TU, so it can never be the entity a foreign reference resolves to.
_C_TU_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".cu")

#: Underlying kinds that are a LAYOUT — the only typedefs that mint a node.
_AGGREGATE_UAK = frozenset({
    "struct", "union", "enum",
    "struct_anonymous", "union_anonymous", "enum_anonymous",
})


def collect_types_csv(codeql_dir: Path):
    """The type side, straight from CodeQL — ``(meta, field_edges)``.

    Replaces reading ``types.json``. The manifest stores a field's type as a
    BARE C string (``{"name": "wrl", "type": "ossl_record_layer_st"}``), which
    cannot be resolved afterwards when the tag names more than one struct: the
    edge lands on whichever entity the composer happened to keep. T2's
    ``field_type_uses`` carries ``type_def_file`` alongside ``type_name``, so
    both ends of every field edge are identified at source.

    ``meta`` is ``(tag, def_file) -> {kind, uak, decls}``. Rows with no
    ``def_file`` are declaration-only and cannot disagree about identity, so
    they fold into every definition of that name; a name with no definition
    anywhere keeps its lone ``(name, "")`` entity. Same rule as
    ``wrap_closure.build_type_meta`` -- and deliberately so: two identity
    resolvers that drift are how the first collision survived a fix.
    """
    t1, t2 = codeql_dir / "t1", codeql_dir / "t2"
    meta: dict[TypeKey, dict] = {}
    alias: dict[str, str] = {}          # typedef name -> underlying tag
    floating: dict[str, list] = collections.defaultdict(list)
    _fams = _mf.load(codeql_dir)
    _gen_of = _mf.generated_by(_fams)          # (tag, def_file) -> macro
    for r in _scope.load_csv(t1 / "types.csv") + _mf.synthetic_type_rows(_fams):
        n, df = r.get("name"), r.get("def_file") or ""
        if not n:
            continue
        row = (_scope.parse_decl_files(r.get("decl_files") or ""),
               r.get("kind") or "", r.get("unaliased_kind") or "")
        # A `typedef` row names its underlying tag in `aliases` and defines
        # nothing itself. It is an ALIAS, not an entity: minting one made 177
        # typedef names (`CERT`, `DTLS1_STATE`, `BIO_SSL`, …) into type nodes
        # that the analysis tree canonicalises onto their struct.
        under = (r.get("aliases") or "").split("|")[0].strip()
        if (r.get("kind") or "") == "typedef":
            if under:
                alias.setdefault(n, under)
                continue
            # A typedef of a PRIMITIVE (`typedef int SSL_TICKET_STATUS;`) names
            # no underlying tag and is a Rust primitive, never a node. The
            # manifest never emitted these; the CSV lists them like any type.
            if (r.get("unaliased_kind") or "") not in _AGGREGATE_UAK:
                continue
        if not df:
            floating[n].append(row)
            continue
        m = meta.setdefault((n, df), {"kind": "", "uak": "", "decls": set()})
        _fold_type_row(m, *row)
    defs: dict[str, list[str]] = collections.defaultdict(list)
    for n, df in meta:
        defs[n].append(df)
    for n, rows in floating.items():
        for df in defs.get(n) or [""]:
            m = meta.setdefault((n, df), {"kind": "", "uak": "", "decls": set()})
            for row in rows:
                _fold_type_row(m, *row)

    casts: dict[str, tuple[set, set]] = collections.defaultdict(
        lambda: (set(), set()))
    for r in _scope.load_csv(t2 / "casts.csv"):
        f, t = r.get("from_tag"), r.get("to_tag")
        if f and t and f != t:
            casts[f][0].add(t)          # f is cast TO t
            casts[t][1].add(f)          # t is cast FROM f

    edges: dict[TypeKey, list[tuple[str, str, str]]] = collections.defaultdict(list)
    for r in _scope.load_csv(t2 / "field_type_uses.csv"):
        sn, tn = r.get("struct_name"), r.get("type_name")
        if not (sn and tn):
            continue
        edges[(sn, r.get("struct_def_file") or "")].append(
            (r.get("field_name") or "", tn, r.get("type_def_file") or ""))
    return meta, dict(edges), dict(casts), alias, _gen_of


def _fold_type_row(m: dict, decls, kind: str, uak: str) -> None:
    """Fold one T1 row into an entity: decls union, aggregate kind beats
    ``typedef``, aggregate unaliased-kind beats a scalar one."""
    m["decls"].update(decls)
    if not m["kind"] or (kind in ("struct", "union", "enum")
                         and m["kind"] not in ("struct", "union", "enum")):
        m["kind"] = kind or m["kind"]
    if not m["uak"]:
        m["uak"] = uak


def _alias_map(analysis_root: Path, types: dict,
               talias: dict[str, str] | None = None) -> dict[str, 'TypeKey']:
    """typedef alias -> canonical tag (+ identity), for C-string resolution."""
    # bare tag -> entity. A tag naming several entities resolves to the one a
    # foreign TU could actually reach: a struct defined inside a `.c` has no
    # linkage past that TU, so a header definition wins. Same rule as
    # `wrap_closure.resolve` -- the alternative is picking by CSV order, which
    # is how `ossl_record_layer_st` came to mean the 10-field QUIC struct.
    by_tag: dict[str, list] = collections.defaultdict(list)
    for k in types:
        by_tag[k[0]].append(k)
    amap: dict[str, TypeKey] = {}
    for t, ks in by_tag.items():
        hdr = [k for k in ks if not k[1].endswith(_C_TU_SUFFIXES)]
        amap[t] = (hdr or ks)[0]
    # `CERT` -> `cert_st` -> its entity. Chase transitively: a typedef of a
    # typedef is legal C and the T1 table records each hop separately.
    for a, under in (talias or {}).items():
        seen = {a}
        while under in (talias or {}) and under not in seen:
            seen.add(under)
            under = talias[under]
        tgt = amap.get(under)
        if tgt is not None:
            amap.setdefault(a, tgt)
    if True:
        for e in _entries_of(analysis_root, "types"):
            if not _is_real(e):
                continue
            for alias in e.get("typedef") or []:
                tgt = amap.get(e.get("name") or e["type"])
                if tgt is not None:
                    amap.setdefault(alias, tgt)
    return amap


# -------------------------------------------------------------- edge building

def _resolve_ctype(ref, amap: dict, types: dict):
    """A C type string, a bare tag, or a ``(tag, def_file)`` pair -> the
    in-universe ENTITY key, else None."""
    if isinstance(ref, tuple):
        return ref if ref in types else amap.get(ref[0])
    name = _base_type_name(ref) if re.search(r"[\s*\[\]]", ref) else ref
    if not name:
        return None
    return amap.get(name)


def _build_edges(types, syms, amap):
    """Fill dep_types / dep_syms on every node; return external sym/type names
    discovered (so we can mint nodes for them).

    Ops are **not folded** into their types: every symbol (functions / macros
    / globals — including a type's lifecycle methods) is its own node. A type
    depends only on its non-scalar **field** types; an op's dependency on its
    type falls out naturally from the op's **signature** (receiver / return
    type). This keeps the type graph acyclic and leaves only genuine
    recursion as SCCs. The type node stores no ops at all — the wrap scheduler
    reverse-derives them from the analysis tree so this file stays a pure
    function of the C.
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

    def res_type_tag(tag: str, dt: set):
        """A depends_on.types TAG (no def_file — the symbol side still stores
        bare tags) -> the entity key it denotes, via `amap`."""
        if not tag:
            return
        k = amap.get(tag) or (tag, "")
        dt.add(k)
        if k not in types:
            ext_types.add(k)

    def res_ctype(ref, dt: set, ds: set[SymKey] | None = None):
        # `ref` is a (type_name, type_def_file) field edge from the CSVs, or a
        # bare C type string from a symbol signature.
        nm = ref[0] if isinstance(ref, tuple) else ref
        # Checked before `_resolve_ctype` — see `cb_keys`.
        if ds is not None:
            cb = cb_keys.get(nm) or cb_keys.get(_base_type_name(nm) or "")
            if cb is not None:
                ds.add(cb)
                return
        t = _resolve_ctype(ref, amap, types)
        if t is None:
            return
        dt.add(t)
        if t not in types:
            ext_types.add(t)

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

    # Types depend on their non-scalar FIELD types, and on nothing else.
    #
    # A type's op/dtor signature refs are deliberately NOT folded in. They would
    # be the types a wrapper's method signatures mention, but a type's method
    # surface is reverse-derived from the acting symbols' ``lifetime`` — an
    # LLM-SUBMITTED judgement field. Folding those signatures made the graph's
    # topology a function of agent output: declaring `ASN1_item_free` the
    # dropper of `ASN1_VALUE` added `ASN1_VALUE_st -> ASN1_ITEM_st` (the
    # dropper's OTHER argument), moving that type off layer 0 and shifting 1,021
    # nodes with it. Two model arms that disagree about one dropper produced
    # different layerings, so "which layer is X at" was arm-dependent rather
    # than a property of the C. The DAG must be deterministic: it is the
    # substrate the wrap stage schedules against, and it has to be stable while
    # the wave that fills those judgement fields is still running.
    #
    # Nothing is lost for ordering. The op is its own symbol node and carries
    # its signature types as its own deps, so those types are still wrapped
    # before it. The type node stores no ops either — the wrap scheduler
    # reverse-derives that surface from the analysis tree at schedule time.
    #
    # `wedge[(T1,T2)]` keeps the reference multiplicity so the genuine cycles
    # (parent↔child backrefs) can be flattened by weighted feedback-arc-set, not
    # collapsed into a co-scheduled blob.
    wedge: dict[tuple[str, str], int] = collections.defaultdict(int)
    for key, n in types.items():
        for ref in n.ctype_refs:                     # field refs (hard layout)
            t = _resolve_ctype(ref, amap, types)
            # `n.dep_syms` passed so a field of function-pointer-typedef type
            # lands on the callback's SYMBOL node. This edge is load-bearing:
            # a function that invokes a callback it reached through a struct
            # field never names the typedef in its own signature, so its
            # ordering runs struct -> callback, and it depends on the struct.
            res_ctype(ref, n.dep_types, n.dep_syms)
            if t in types and t != key:
                wedge[(key, t)] += 1
        # Cast-graph ordering edge (classifies the otherwise-ambiguous `casted`
        # relation into a correct-direction dep). For each T this type is cast
        # TO, add `tag -> T` ONLY when T is strictly more cast-central — i.e. T
        # is cast to/from more types than `tag`. High cast-degree marks the hub:
        # a generic engine erased to by many instances, or a polymorphic base
        # downcast to by many derived. So the edge always points from the
        # low-degree instance/derived UP to the high-degree engine/base it
        # depends on — never the reverse — and the strict `>` keeps it acyclic
        # (edges run low-degree → high-degree only, so no cast cycle can form).
        # Macro-generator ordering edge. Unlike `casted` below, this relation
        # is DIRECTED at the source: an instance always depends on the macro
        # that minted it (the generic must exist before its aliases), so there
        # is no centrality heuristic and no `>` guard to invert when an
        # instance carries genuine casts of its own. The >= 2 threshold lives
        # in `macro_families.load`, so a one-off macro mints no node and
        # produces no edge here.
        if n.generated_by:
            g = next((k for k in types if k[0] == n.generated_by), None)
            if g is not None and g != key:
                n.dep_types.add(g)
                wedge[(key, g)] += 1

        my_deg = n.cast_degree()
        for t in (amap.get(x) for x in n.cast_to):
            tn = types.get(t) if t else None
            if tn is not None and t != key and tn.cast_degree() > my_deg:
                n.dep_types.add(t)
                wedge[(key, t)] += 1
        n.dep_types.discard(key)         # no self-edge
        wedge.pop((key, key), None)

    # Array-cluster element inversion. A typed `CVec<T>` alias on cluster A
    # references element wrapper T, but the cluster is a foundational leaf (the
    # generic container is T-agnostic; the synthetic tag has no dependents).
    # Folding A->T forward would sink the allocator below the transitive closure
    # of every type ever heap-allocated. Instead invert: emit T->A (the element
    # depends on the cluster, so A wraps first) and hand back (A, T) as a forced
    # back-edge — A's alias renders T raw (`fallback`), and T back-fills it once
    # T's wrapper lands, strictly after A's module is complete (no same-wave
    # writer race on A's file).
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

    return ext_syms, ext_types, dict(wedge)


# ------------------------------------------------------- node id + adjacency

def _tid(key) -> str:
    tag, df = key
    return "t:" + tag + "\x00" + (df or "")


def _sid(key: SymKey) -> str:
    # Graph id encodes (name, file) so same-named file-local statics are
    # distinct nodes; the emitted ``id`` stays the bare name (+ ``defined_in``).
    name, file = key
    return "s:" + name + "\x00" + (file or "")


def _build_graph(types, syms, ext_syms, ext_types):
    """Return (nodes, adj): nodes id -> record dict; adj id -> set(dep ids).
    Every type and every symbol is a node (ops are not folded, and a type
    carries no ``ops`` list — the scheduler derives it from the analysis
    tree)."""
    nodes: dict[str, dict] = {}
    adj: dict[str, set[str]] = {}

    def add(nid, rec):
        nodes[nid] = rec
        adj.setdefault(nid, set())

    for key, n in types.items():
        add(_tid(key), {
            "id": n.tag, "node_kind": "type", "subkind": n.kind,
            "defined_in": n.origin(),
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
            add(_tid(tag), {"id": tag[0], "node_kind": "type", "subkind": "external",
                            "defined_in": None,
                            "_dt": set(), "_ds": set()})
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
    if len(comp) == 1:
        rec = dict(nodes[comp[0]])
        out = {"id": rec["id"], "node_kind": rec["node_kind"],
               "subkind": rec["subkind"], "defined_in": rec["defined_in"]}
        if rec["node_kind"] == "type":
            if rec.get("loc"):
                out["loc"] = rec["loc"]
        elif rec.get("loc"):
            out["loc"] = rec["loc"]
        return out  # deps filled by caller
    def member(m):
        d = {"id": m["id"], "node_kind": m["node_kind"], "subkind": str(m["subkind"]),
             "defined_in": m.get("defined_in")}
        if m["node_kind"] == "type":
            if m.get("loc"):
                d["loc"] = m["loc"]
        elif m.get("loc"):
            d["loc"] = m["loc"]
        return d
    return {"scc": [member(nodes[c]) for c in comp]}


def _populate_nfields(codeql_dir: Path, types: dict[str, TypeNode]) -> None:
    """Set each type's ``nfields`` to its **full** struct field count from the
    T1 ``fields.csv`` (``<crustify>/codeql/t1/fields.csv``, a sibling of the
    analysis tree). This is the whole struct, NOT the port-accessed subset that
    ``types.json``'s ``fields[]`` narrows to — a struct's translated surface
    (``define_type!`` + accessors) scales with its field layout, so a type's
    own LoC is its field count. fields.csv attributes anonymous-struct fields to
    the naming typedef, so ``struct_name`` matches the type tag. Missing CSV →
    every ``nfields`` stays 0 (still deterministic, no CodeQL)."""
    fcsv = Path(codeql_dir) / "t1" / "fields.csv"
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
    for key, n in types.items():
        cnt = by_key.get((n.tag, n.defined_in or ""))
        if cnt is None:
            cnt = by_key.get((n.tag, n.declared_in or ""))
        n.nfields = cnt if cnt is not None else by_name.get(n.tag, 0)


def port_touched_fields(analysis_root: Path, port_syms: set) -> dict[str, set[str]]:
    """``{type tag: field names the port scope reads}``.

    Derived from the port-scope symbols' own ``depends_on.types[].fields``,
    which is exactly "fields this function accesses" — the same quantity the
    types composer computes transiently as ``focus_by_key`` for the wrapper's
    focus. A type absent from the result is reached only opaquely.
    """
    touched: dict[str, set[str]] = {}
    if True:
        for e in _entries_of(analysis_root, "symbols"):
            key = (e.get("name"),
                   _sym_filekey(e.get("defined_in"), e.get("declared_in")))
            if key not in port_syms:
                continue
            for d in (e.get("depends_on") or {}).get("types") or []:
                tag = d.get("type")
                if tag:
                    touched.setdefault(tag, set()).update(d.get("fields") or [])
    return touched


def compose(analysis_root, scope_json=None, codeql_dir: Path | None = None
            ) -> dict[str, Any]:
    """Build the layered DAG. With ``scope_json``, narrowed to that target
    (see :func:`_collect`); without it, unnarrowed.

    ``analysis_root`` is the composed ``(types, syms)`` pair, or -- for this
    module's own CLI -- a legacy analysis-root path. ``codeql_dir`` must be
    given with the pair, since there is no tree path to derive it from."""
    port_syms = port_fields = None
    if scope_json is not None and (isinstance(scope_json, dict)
                                   or Path(scope_json).is_file()):
        from . import scope as _scope
        port_syms = set()
        for kind in ("functions", "globals", "macros"):
            try:
                port_syms |= _scope.load_port_entities(scope_json, kind)
            except Exception:
                pass
        port_fields = port_touched_fields(analysis_root, port_syms)
    in_scope_types = None
    if scope_json is not None and (isinstance(scope_json, dict)
                                   or Path(scope_json).is_file()):
        _sj = _scope._doc(scope_json)
        # Pair-keyed where the scope knows the defining file, name-keyed where
        # it does not. Nodes are keyed `(tag, def_file)`, so a bare-name filter
        # admits an out-of-scope type whenever ANY in-scope type shares its
        # name -- `entry` (libgit2) rode in from deps/xdiff on the unrelated
        # `entry` in indexer.c, and `ring_buf` (openssl) from bss_dgram_pair.c.
        # Such a node has no record behind it, so it contributes nothing but a
        # spurious `--file` prompt on a name with one real meaning.
        #
        # A declared-only type (opaque handle, ~1/3 of the scope) has no
        # `defined_in` to pair with, so it stays name-keyed and the membership
        # test accepts either form.
        in_scope_types = set()
        for side in ("port", "wrap"):
            for e in _sj.get(side, {}).get("types") or []:
                df = e.get("defined_in")
                in_scope_types.add((e["name"], df) if df else e["name"])
    if codeql_dir is None:
        codeql_dir = Path(analysis_root).parent / "codeql"
    types, syms, talias = _collect(analysis_root, port_syms, port_fields,
                                   codeql_dir=codeql_dir,
                                   in_scope_types=in_scope_types)
    _populate_nfields(codeql_dir, types)
    amap = _alias_map(analysis_root, types, talias)
    ext_syms, ext_types, wedge = _build_edges(types, syms, amap)
    nodes, adj = _build_graph(types, syms, ext_syms, ext_types)
    sccs = _tarjan(adj)
    # Flatten genuine cycles (parent↔child backrefs) with a weighted FAS: the
    # back-edges become per-node `fallback` deps (the wrap stage renders those as
    # raw `ffi::T` — their target is a not-yet-wrapped cycle sibling), and the
    # remaining graph is acyclic, so every node layers individually.
    back = _break_sccs(sccs, adj, nodes, wedge)
    n_cyclic = sum(1 for c in sccs if len(c) > 1)
    adj_dag = {nid: {d for d in deps if (nid, d) not in back}
               for nid, deps in adj.items()}
    sccs = _tarjan(adj_dag)
    buckets = _layer(adj_dag, sccs)

    def _grp(ids):
        # BOTH sides carry `defined_in`, so a same-named collision is
        # unambiguous on either. Type nodes are keyed `(tag, def_file)` now that
        # the type side is collected from CodeQL: a TU-local `struct version_info`
        # is a different type in every `.c` that declares one, and a bare tag
        # cannot say which. Consumers key on the pair throughout.
        def pairs(prefix):
            out = [{"name": nodes[d]["id"], "defined_in": nodes[d]["defined_in"]}
                   for d in ids if d.startswith(prefix)]
            out.sort(key=lambda x: (x["name"], x["defined_in"] or ""))
            return out
        return {"types": pairs("t:"), "syms": pairs("s:")}

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
            "depends on its non-scalar field types, and on nothing else: op "
            "signature types and the op list itself are BOTH excluded, because "
            "a type's lifecycle is reverse-derived from the agent-submitted "
            "`lifetime` field and this graph is a pure function of the C. The "
            "wrap scheduler derives a type's ops from the analysis tree at "
            "schedule time. Genuine cycles (parent<->child backrefs) are "
            "flattened by weighted feedback-arc-set: the resulting graph is "
            "acyclic (no `scc:[...]` super-nodes), and a node's `fallback` "
            "(when present) lists the cycle back-edges its wrapper must render "
            "as raw `ffi::T` because their target is a not-yet-wrapped sibling; "
            "`back_fill` (the reverse) lists nodes that already render *this* "
            "type raw and should switch to its wrapper once it lands. "
            "Every node is keyed by (name, defined_in) -- symbols by "
            "(name, defined_in|canonical-decl), types by (tag, def_file) -- so "
            "both `deps.types` and `deps.syms` (and the `fallback`/`back_fill` "
            "twins) are lists of {name, defined_in} objects: a same-named static "
            "and a TU-local `struct version_info` are equally ambiguous under a "
            "bare name. Topo-layered: layer "
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
