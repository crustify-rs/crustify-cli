"""Compute the wrap-scope import surface for a port target (scope.json `wrap`).

Tier (iii): a pure function of CodeQL facts + the port seed. The wrap closure
is computed DIRECTLY from the T1 entity tables + the T2 reach graph — it does
NOT read ``syms.json`` (or ``types.json``), so ``scope.json`` is a standalone
pre-analysis artifact, independent of whether ``analyze symbols``/``types`` has
run. (``build_index`` recomputes the exact per-symbol ``depends_on`` view the
symbols composer would write, sharing ``syms_manifest``'s reach primitives, so
the result is byte-equivalent to the old post-``analyze symbols`` aggregation.)
The per-target derivation:

  1. Walk the target's **port** entities (those defined in a ``scope.json`` port
     file) and follow their ``depends_on`` edges into the items they use.
  2. Keep the deps that classify **wrap** (the FFI frontier).
  3. **Narrow** each wrap item's ``declared_in`` — which is the entity-table
     *superset* of every declaration site — down to the header(s) the importing
     port TU actually ``#include``s, using the build-resolved include graph
     (``includes.csv``). A header H survives iff it declares the item **and** is
     in the transitive include-closure of the port TU that depends on it.

The result is the precise import surface (not the all-decls superset, not the
implicit not-port complement), persisted as the ``wrap`` section of the target's
scope.json — **derived and regenerable** from ``port`` (recompute when port
changes). A wrap item that survives through >1 distinct header is genuinely
imported two ways: a re-export signal, recorded rather than collapsed.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from . import scope


# --------------------------------------------------------------- include graph

def build_include_closure(
    includes_rows: list[dict],
) -> Callable[[str], set[str]]:
    """Return ``closure(tu) -> set`` of every file transitively ``#include``d by
    ``tu``, memoized. The graph is build-resolved (``includes.csv`` only records
    branches the traced build actually took), so the closure is exactly what
    *this* build's TU imports. Cycle-safe via a visited set."""
    adj: dict[str, set[str]] = defaultdict(set)
    for r in includes_rows:
        s, d = r.get("source_file"), r.get("included_file")
        if s and d:
            adj[s].add(d)

    cache: dict[str, set[str]] = {}

    def closure(tu: str) -> set[str]:
        hit = cache.get(tu)
        if hit is not None:
            return hit
        seen: set[str] = set()
        stack = list(adj.get(tu, ()))
        while stack:
            h = stack.pop()
            if h in seen:
                continue
            seen.add(h)
            stack.extend(adj.get(h, ()))
        cache[tu] = seen
        return seen

    return closure


# --------------------------------------------------------------- analysis index

class _Index:
    """Flat views over the analysis tree needed for closure + classification.

    Symbols only: the wrap closure walks port *symbols'* ``depends_on`` for the
    callable surface and, for types, walks the CodeQL field-type graph directly
    (``field_type_uses`` + the T1 ``types`` table) — see ``build_type_meta`` /
    ``build_field_edges``. So `types.json` is no longer read here: the type-side
    closure is a pure function of CodeQL facts + scope, independent of whether
    the type-analyzer agent has annotated anything."""

    def __init__(self) -> None:
        # (name, defined_in) -> {kind, declared_in}
        self.sym: dict[tuple[str, str], dict] = {}
        # port-scope symbols, as (entity, owning_tu): the seeds we expand from
        self.port_syms: list[tuple[dict, str]] = []
        # symbol name -> canonical type tags its signature names (its
        # `depends_on.types`). For a wrap function this is its signature surface
        # (params/return); used to pull a facade's signature types into the wrap
        # closure so they get a safe wrapper instead of a raw `ffi::T`. Keyed by
        # name (external linkage ⇒ unique program-wide; a same-named static only
        # over-includes, which add_type then re-filters to wrap aggregates).
        self.sig_types: dict[str, set[str]] = {}


def _decls(v: Any) -> list[str]:
    """Normalize a declared_in field (str | list | None) to a list."""
    if isinstance(v, list):
        return [d for d in v if d]
    return [v] if v else []


def build_index(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    port_paths: set[str],
    scope_json_path: Path,
) -> _Index:
    """Build the closure index DIRECTLY from CodeQL T1/T2 — no ``syms.json``.

    Tier (iii): the wrap closure's symbol surface is recomputed from the same
    T2 reach graph the symbols composer uses, so ``scope.json`` is a pure
    function of CodeQL facts + the port seed — independent of whether (or how)
    ``analyze symbols`` has run. For every function/global/macro we recompute
    the EXACT ``depends_on`` view the symbols composer writes, so this index is
    byte-equivalent to the one the old ``syms.json`` walk produced:

      - port-scope entity → full ``depends_on`` (``_compose_dep_syms`` over the
        forward-sym set + ``_compose_dep_types`` with the body field-access
        index) — matching ``_port_additions_function``;
      - everything else → signature-only (``syms: []``, ``_compose_dep_types``
        with ``field_access_index=None``) — matching ``_wrap_additions_function``.

    ``sym``/``sig_types`` are filled for every symbol; the closure only ever
    queries them for callees of port symbols (port-reachable ⇒ present), so any
    extra entries are inert. ``port_syms`` is seeded FILE-level (``defined_in``
    in a port file), exactly as the original walk did.
    """
    from . import syms_manifest as sm
    from .reach import Reach

    funcs = scope.load_csv(csv_dir_t1 / "functions.csv")
    globals_ = scope.load_csv(csv_dir_t1 / "globals.csv")
    macros = scope.load_csv(csv_dir_t1 / "macros.csv")
    types = scope.load_csv(csv_dir_t1 / "types.csv")
    by_name = scope.build_types_index(types)
    reach = Reach(csv_dir_t2, port_paths)

    # File-local statics reuse a name across TUs; disambiguate their body
    # field-accesses by def_file (mirrors syms_manifest.compose — multidef
    # conflation otherwise leaks unrelated types into depends_on.types).
    deffiles: dict[str, set] = defaultdict(set)
    for r in funcs:
        if r.get("name") and r.get("def_file"):
            deffiles[r["name"]].add(r["def_file"])
    multidef = {n for n, fs in deffiles.items() if len(fs) > 1}
    field_access_index = sm._load_field_access_index(csv_dir_t2, multidef)

    sym_index: dict[tuple[str, str], dict] = {}
    for r in (*funcs, *globals_, *macros):
        sym_index[(r["name"], r["def_file"])] = r

    port_funcs = scope.load_port_entities(scope_json_path, "functions")
    port_globals = scope.load_port_entities(scope_json_path, "globals")
    port_macros = scope.load_port_entities(scope_json_path, "macros")

    idx = _Index()

    def ingest(rows, kind_of, decls_of, port_set, gate) -> None:
        for r in rows:
            name = r.get("name")
            if not name:
                continue
            def_file = r.get("def_file") or ""
            is_port = (name, def_file) in port_set
            # Candidate gate — mirror syms_manifest pass 1 (scope_enabled,
            # not seed): a non-port symbol is admitted only if it is
            # port-reachable and an allowed kind. WITHOUT this, sig_types
            # (keyed by bare name) conflates same-named statics in unreachable
            # files — e.g. a port `git_odb` reaches odb.c's `normalize_options`,
            # but blame.c/describe.c also define `normalize_options`, leaking
            # git_blame/describe_options into the wrap surface.
            if not is_port and not gate(r):
                continue
            dep_types = sm._compose_dep_types(
                reach, name, def_file, by_name,
                field_access_index if is_port else None,
            )
            idx.sym[(name, def_file)] = {
                "kind": kind_of(r), "declared_in": decls_of(r),
            }
            sig = {t.get("type") for t in dep_types
                   if isinstance(t, dict) and t.get("type")}
            if sig:
                idx.sig_types.setdefault(name, set()).update(sig)
            if def_file in port_paths:
                dep_syms = (
                    sm._compose_dep_syms(
                        sm._forward_syms_of(name, def_file, reach), sym_index)
                    if is_port else []
                )
                idx.port_syms.append(({
                    "name": name, "defined_in": r.get("def_file") or None,
                    "depends_on": {"syms": dep_syms, "types": dep_types},
                }, def_file))

    ingest(funcs, lambda r: r["linkage"],
           lambda r: scope.parse_decl_files(r.get("decl_files") or ""),
           port_funcs,
           lambda r: r["linkage"] not in sm._WRAP_DISALLOWED_FN_KINDS
           and reach.is_function_port_reachable(r["name"], r["def_file"]))
    ingest(globals_, lambda r: r["linkage"],
           lambda r: scope.parse_decl_files(r.get("decl_files") or ""),
           port_globals,
           lambda r: r["linkage"] not in sm._WRAP_DISALLOWED_GLOBAL_KINDS
           and reach.is_global_port_reachable(r["name"], r["def_file"]))
    ingest(macros, lambda r: "macro",
           lambda r: [r["def_file"]] if r.get("def_file") else [],
           port_macros,
           lambda r: reach.is_macro_port_reachable(r["name"], r["def_file"]))
    return idx


def build_type_meta(type_rows: list[dict]) -> dict[str, dict]:
    """Type metadata from the T1 ``types`` table — ``tag -> {def_file, decls,
    kind}`` — the deterministic CodeQL source for classify/home/narrow, covering
    EVERY type (incl. system/external leaves like ``pthread_mutex_t`` that have
    no analysis entry). CodeQL emits two rows per type — a ``typedef`` row (often
    empty ``def_file``, public decl header) and the underlying ``struct`` row
    (real ``def_file``, full decl set) — so rows are MERGED per name: first
    non-empty ``def_file`` wins, decls union, aggregate kind beats ``typedef``."""
    meta: dict[str, dict] = {}
    for r in type_rows:
        name = r.get("name")
        if not name:
            continue
        df = r.get("def_file") or ""
        decls = scope.parse_decl_files(r.get("decl_files") or "")
        kind = r.get("kind") or ""
        uak = r.get("unaliased_kind") or ""
        m = meta.get(name)
        if m is None:
            meta[name] = {"def_file": df, "decls": set(decls),
                          "kind": kind, "uak": uak}
        else:
            if df and not m["def_file"]:
                m["def_file"] = df
            m["decls"].update(decls)
            if kind in ("struct", "union", "enum") and \
                    m["kind"] not in ("struct", "union", "enum"):
                m["kind"] = kind
            if uak in _AGGREGATE_UAK and m["uak"] not in _AGGREGATE_UAK:
                m["uak"] = uak
    for m in meta.values():
        m["decls"] = sorted(m["decls"])
    return meta


# Underlying-type kinds that bindgen emits as a layout (struct/union/enum,
# incl. the anonymous-struct underlying a typedef like `pthread_mutex_t`).
# A scalar/primitive typedef (`uint32_t`, `size_t`, `time_t`) is a Rust
# primitive — never an FFI type to bind/home. A `callback` typedef is a SYMBOL
# (handled on the sym surface), not a wrap-type. So the type closure admits
# only aggregates.
_AGGREGATE_UAK = frozenset({
    "struct", "union", "enum",
    "struct_anonymous", "union_anonymous", "enum_anonymous",
})


def _is_aggregate(meta: dict) -> bool:
    return meta.get("kind") in ("struct", "union", "enum") \
        or meta.get("uak") in _AGGREGATE_UAK


def build_field_edges(field_type_rows: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """Field→type edges from T2 ``field_type_uses`` — ``struct_tag ->
    [(field_name, field_type_name)]``. Keyed by tag alone (the field structure
    is per-struct; public structs have unique tags, and pooling the rare
    file-local-name clash only over-includes, never drops). This is the same
    deterministic source the type composer lists a struct's fields from."""
    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in field_type_rows:
        sn, fn, tn = r.get("struct_name"), r.get("field_name"), r.get("type_name")
        if sn and fn and tn:
            edges[sn].append((fn, tn))
    return edges


# --------------------------------------------------------------- aggregation

def _sym_bucket(kind: str) -> str:
    if kind.startswith("macro"):
        return "macros"
    if kind.startswith("global"):
        return "globals"
    return "functions"  # function_* (and anything else callable)


def compose_wrap(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    scope_json_path: Path,
    includes_rows: list[dict],
    port_paths: set[str],
    type_rows: list[dict],
    field_type_rows: list[dict],
) -> dict:
    """Build the ``wrap`` section: ``{files, functions, globals, macros, types}``.

    ``files`` is the narrowed import-header surface; the entity buckets list the
    reached wrap items with their import header(s) (``declared_in``) and a
    ``reexport`` flag when an item is imported through more than one header.

    Two complementary walks:
      - **symbols** — expand each port symbol's ``depends_on`` edges (callable
        surface + the types its signature/body name), classifying each as
        wrap (FFI frontier) or port (keep walking, via its own seed).
      - **types** — walk the CodeQL field-type graph (``type_rows`` = T1
        ``types``, ``field_type_rows`` = T2 ``field_type_uses``): a PORT struct
        is full-scanned (the Rust port reimplements its whole ``#[repr(C)]``
        layout, so every field type is a real dep); a WRAP struct only walks the
        fields port code actually accesses (``port_touched``) — bindgen owns the
        rest of its layout. This is what pulls a by-value-embedded external type
        like ``pthread_mutex_t`` (``git_odb.lock``) into the wrap surface.
    """
    closure = build_include_closure(includes_rows)
    idx = build_index(csv_dir_t1, csv_dir_t2, port_paths, scope_json_path)
    type_meta = build_type_meta(type_rows)
    field_edges = build_field_edges(field_type_rows)

    # wrap item identity -> {"name"/"type", "defined_in", "declared_in": set}
    sym_items: dict[tuple[str, str], dict] = {}
    type_items: dict[str, dict] = {}
    files: set[str] = set()

    def narrow(decls: list[str], tu: str) -> list[str]:
        """Headers declaring the item that the importing TU actually #includes.
        Falls back to the declared headers if the include graph has no row for
        the TU (defensive — a real dep always has ≥1 in-closure header)."""
        clo = closure(tu)
        hit = [d for d in decls if d in clo]
        return hit or [d for d in decls if d.endswith((".h", ".hpp", ".hh"))] or decls

    def add_sym(name: str, df: str, decls: list[str], tu: str) -> None:
        if not name or scope.classify(df, decls, port_paths) != "wrap":
            return
        via = narrow(decls, tu)
        if not via:
            return  # no header to import from — a compiler builtin / intrinsic,
                    # not a wrappable FFI item (the port stage lowers these).
        rec = sym_items.setdefault((name, df), {
            "name": name, "defined_in": df, "declared_in": set()})
        rec["declared_in"].update(via)
        files.update(via)

    def add_type(tag: str, tu: str) -> None:
        meta = type_meta.get(tag)
        if meta is None:
            return  # unknown tag (synthetic cluster / anonymous) — never a C
                    # type bindgen binds; synthetics are wrap-by-kind downstream.
        if not _is_aggregate(meta):
            return  # scalar/primitive typedef (Rust primitive) or a callback
                    # (a sym, handled on the symbol surface) — not a wrap-type.
        df, decls = meta["def_file"], meta["decls"]
        if scope.classify(df, decls, port_paths) != "wrap":
            return
        via = narrow(decls, tu)
        if not via:
            return
        rec = type_items.setdefault(tag, {
            "type": tag, "defined_in": df, "declared_in": set()})
        rec["declared_in"].update(via)
        files.update(via)

    # Fields of a WRAP struct that port code actually reaches into, harvested
    # from the port symbols' `depends_on.types[].fields` (composer-derived from
    # field_accesses). Drives the wrap-struct half of the field-walk.
    port_touched: dict[str, set[str]] = defaultdict(set)

    # Expand from port functions/globals/macros via their depends_on edges.
    for e, tu in idx.port_syms:
        dep = e.get("depends_on") or {}
        for d in dep.get("syms") or []:
            nm = d.get("name")
            add_sym(nm, d.get("defined_in") or "",
                    _decls(d.get("declared_in")), tu)
            # One hop further: a wrap facade's signature types need safe
            # wrappers too (else the facade takes/returns a raw `ffi::T`). Pull
            # the called symbol's signature types — e.g. a port that frees
            # `STACK_OF(OCSP_RESPID)` reaches `OCSP_RESPID_free` (wrap), whose
            # signature names `ocsp_responder_id_st`; without this the element
            # type stays an unwrapped opaque pointer. `add_type` re-filters to
            # wrap-scope aggregates, so a primitive/port/opaque-only sig type is
            # dropped; an opaque-but-owned struct lands as a field-less wrapper.
            for st in idx.sig_types.get(nm, ()):
                add_type(st, tu)
        for d in dep.get("types") or []:
            tag = d.get("type")
            if not tag:
                continue
            add_type(tag, tu)
            for fld in d.get("fields") or []:
                port_touched[tag].add(fld)

    # Field-walk over the CodeQL type graph. PORT struct → every field type
    # (full layout reimplemented in Rust); WRAP struct → only port-touched
    # fields (bindgen owns the rest). Recurse into reached types the same way;
    # cycle-safe via `walked`. The importing TU is the seed port type's
    # def_file (a port file); narrow() falls back to declared headers when the
    # include graph has no TU row (e.g. a header-defined struct).
    walked: set[str] = set()

    def walk_type(tag: str, tu: str) -> None:
        if tag in walked:
            return
        walked.add(tag)
        meta = type_meta.get(tag)
        if meta is None:
            return
        cls = scope.classify(meta["def_file"], meta["decls"], port_paths)
        edges = field_edges.get(tag, [])
        if cls == "wrap":
            add_type(tag, tu)
            touched = port_touched.get(tag, set())
            edges = [(f, t) for (f, t) in edges if f in touched]
        for _field, ftype in edges:
            walk_type(ftype, tu)

    for tag, meta in type_meta.items():
        if meta["def_file"] in port_paths:
            walk_type(tag, meta["def_file"])

    # Canonicalize a null-def `extern` item onto its real definition. A port
    # caller that saw only a prototype records the dep as (name, ""); another
    # that saw the definition records (name, <tu.c>). They are the SAME external
    # symbol (C external linkage ⇒ the name is unique program-wide ⇒ no
    # mismatch), so re-key the null-def entry to the real defined_in (merging
    # declared_in) — never two scope entries for one entity. Restricted to
    # external-linkage kinds; statics/macros never carry an extern row. Genuine
    # out-of-tree externs (no real def anywhere) keep their lone (name, "").
    _EXTERNAL = {"function_exported", "global_extern"}
    real_def: dict[str, str] = {}                  # name -> its real defined_in
    for (nm, df2), meta in idx.sym.items():
        if df2 and meta.get("kind") in _EXTERNAL:
            real_def[nm] = df2
    for key in [k for k in sym_items if not k[1]]:
        name = key[0]
        rdf = real_def.get(name)
        if rdf is None:
            continue                               # genuine extern — keep as-is
        rec = sym_items.pop(key)
        tgt = sym_items.setdefault((name, rdf), {
            "name": name, "defined_in": rdf, "declared_in": set()})
        tgt["declared_in"].update(rec["declared_in"])

    # Bucket symbols by kind (looked up from the index).
    buckets: dict[str, list] = {"functions": [], "globals": [], "macros": []}
    for (name, df), rec in sym_items.items():
        kind = (idx.sym.get((name, df)) or {}).get("kind", "")
        via = sorted(rec["declared_in"])
        buckets[_sym_bucket(kind)].append({
            "name": name, "defined_in": df, "declared_in": via,
            **({"reexport": True} if len(via) > 1 else {})})

    types_out = []
    for tag, rec in type_items.items():
        via = sorted(rec["declared_in"])
        types_out.append({
            "type": tag, "defined_in": rec["defined_in"], "declared_in": via,
            **({"reexport": True} if len(via) > 1 else {})})

    return {
        "_comment": (
            "DERIVED wrap-scope import surface for this target — regenerable "
            "from `port`. Computed by compose/wrap_closure.py: the FFI items "
            "port code reaches, with `declared_in` narrowed to the header(s) "
            "the importing port TU actually #includes (build-resolved). "
            "`reexport: true` marks an item imported through >1 header. "
            "Recompute whenever `port` changes."
        ),
        "files": sorted(files),
        "functions": sorted(buckets["functions"], key=lambda r: (r["name"], r["defined_in"])),
        "globals": sorted(buckets["globals"], key=lambda r: (r["name"], r["defined_in"])),
        "macros": sorted(buckets["macros"], key=lambda r: (r["name"], r["defined_in"])),
        "types": sorted(types_out, key=lambda r: r["type"]),
    }

