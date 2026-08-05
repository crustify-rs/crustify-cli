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
    any agent has annotated anything."""

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
        # Reach object — exposed so compose_wrap can use its query API
        # (e.g. functions_using_type / is_function_port_reachable) for the
        # callback walk that runs after build_index returns.
        self.reach = None


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
    idx.reach = reach
    return idx


def _merge_meta(m: dict, decls: list[str], kind: str, uak: str) -> None:
    """Fold one T1 row into an existing entity: decls union, aggregate kind
    beats ``typedef``, aggregate unaliased-kind beats a scalar one."""
    m["decls"].update(decls)
    if not m["kind"]:
        m["kind"] = kind
    elif kind in ("struct", "union", "enum") and \
            m["kind"] not in ("struct", "union", "enum"):
        m["kind"] = kind
    if not m["uak"]:
        m["uak"] = uak
    elif uak in _AGGREGATE_UAK and m["uak"] not in _AGGREGATE_UAK:
        m["uak"] = uak


def build_type_meta(type_rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Type metadata from the T1 ``types`` table — ``(tag, def_file) ->
    {def_file, decls, kind, uak}`` — the deterministic CodeQL source for
    classify/home/narrow, covering EVERY type (incl. system/external leaves like
    ``pthread_mutex_t`` that have no analysis entry).

    Keyed on the PAIR, because a tag alone does not identify a type. Two
    unrelated structs may share one: ``ring_buf`` is the QUIC stream buffer in
    ``include/internal/ring_buf.h`` and, separately, a file-local datagram-BIO
    buffer inside ``crypto/bio/bss_dgram_pair.c``, with disjoint fields. Merging
    those by name produced a chimera — ``def_file`` from whichever row the CSV
    listed first, ``decls`` unioned across both — and since every downstream
    join is on ``(name, defined_in)``, a wrap entry stamped with the wrong
    ``def_file`` matched no analysis record and fell out of the surface
    entirely.

    Rows that DO describe one entity still merge: CodeQL emits a ``typedef`` row
    (usually empty ``def_file``, public decl header) beside the underlying
    ``struct`` row (real ``def_file``, full decl set). Those carry no
    ``def_file`` to disagree on, so they are folded into every definition of
    that name — a forward declaration cannot say which one it refers to. A name
    with no definition anywhere keeps its lone ``(name, "")`` entity."""
    meta: dict[tuple[str, str], dict] = {}
    floating: dict[str, list[tuple[list[str], str, str]]] = defaultdict(list)
    for r in type_rows:
        name = r.get("name")
        if not name:
            continue
        df = r.get("def_file") or ""
        decls = scope.parse_decl_files(r.get("decl_files") or "")
        kind = r.get("kind") or ""
        uak = r.get("unaliased_kind") or ""
        if not df:
            floating[name].append((decls, kind, uak))
            continue
        m = meta.get((name, df))
        if m is None:
            meta[(name, df)] = {"def_file": df, "decls": set(decls),
                                "kind": kind, "uak": uak}
        else:
            _merge_meta(m, decls, kind, uak)

    defs_of: dict[str, list[str]] = defaultdict(list)
    for name, df in meta:
        defs_of[name].append(df)
    for name, rows in floating.items():
        targets = defs_of.get(name)
        if targets:
            for df in targets:
                for decls, kind, uak in rows:
                    _merge_meta(meta[(name, df)], decls, kind, uak)
        else:
            m = {"def_file": "", "decls": set(), "kind": "", "uak": ""}
            for decls, kind, uak in rows:
                _merge_meta(m, decls, kind, uak)
            meta[(name, "")] = m

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

# Translation-unit suffixes. A type DEFINED in one of these has no linkage past
# that TU, so it can never be an importable wrap item for another file.
_C_TU_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".cu")


def _is_aggregate(meta: dict) -> bool:
    return meta.get("kind") in ("struct", "union", "enum") \
        or meta.get("uak") in _AGGREGATE_UAK


def build_field_edges(
    field_type_rows: list[dict],
) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """Field→type edges from T2 ``field_type_uses`` — ``(struct_tag,
    struct_def_file) -> [(field_name, field_type_name, field_type_def_file)]``.

    Keyed on the pair for the same reason as :func:`build_type_meta`: a tag
    alone can name two different structs, and pooling their fields walks edges
    that neither struct has. The CSV already carries ``struct_def_file`` and
    ``type_def_file``, so both ends of the edge are identified exactly. This is
    the same deterministic source the type composer lists a struct's fields
    from."""
    edges: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for r in field_type_rows:
        sn, fn, tn = r.get("struct_name"), r.get("field_name"), r.get("type_name")
        if sn and fn and tn:
            edges[(sn, r.get("struct_def_file") or "")].append(
                (fn, tn, r.get("type_def_file") or ""))
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
    type_items: dict[tuple[str, str], dict] = {}
    files: set[str] = set()

    defs_of: dict[str, list[str]] = defaultdict(list)
    for _tag, _df in type_meta:
        defs_of[_tag].append(_df)

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

    def resolve(tag: str, tu: str) -> list[tuple[str, str]]:
        """The ``(tag, def_file)`` entities a bare tag may denote, restricted to
        those ``tu`` can actually see.

        Callers reach types by tag alone (``depends_on.types[].type``,
        ``sig_types``, a field's ``type_name``), but a tag can name more than one
        struct. Prefer the entities whose declaring headers this TU includes;
        failing that, the ones defined in a header — a struct defined inside a
        ``.c`` has no linkage past that TU, so it can never be an importable
        wrap item for anyone else. Both filters empty means the tag has only
        TU-local definitions and none is visible here; keep them all rather than
        drop the edge, so the walk still errs toward over-inclusion."""
        dfs = defs_of.get(tag)
        if not dfs:
            return []
        keys = [(tag, df) for df in dfs]
        if len(keys) == 1:
            return keys
        clo = closure(tu)
        hit = [k for k in keys if any(d in clo for d in type_meta[k]["decls"])]
        if hit:
            return hit
        hdr = [k for k in keys if not k[1].endswith(_C_TU_SUFFIXES)]
        return hdr or keys

    def add_type_key(key: tuple[str, str], tu: str) -> None:
        meta = type_meta.get(key)
        if meta is None:
            return  # unknown tag (anonymous / not in the types tree) — never
                    # a C type bindgen binds.
        if not _is_aggregate(meta):
            return  # scalar/primitive typedef (Rust primitive) or a callback
                    # (a sym, handled on the symbol surface) — not a wrap-type.
        df, decls = meta["def_file"], meta["decls"]
        if scope.classify(df, decls, port_paths) != "wrap":
            return
        via = narrow(decls, tu)
        if not via:
            return
        rec = type_items.setdefault(key, {
            "type": key[0], "defined_in": df, "declared_in": set()})
        rec["declared_in"].update(via)
        files.update(via)

    def add_type(tag: str, tu: str) -> None:
        for key in resolve(tag, tu):
            add_type_key(key, tu)

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
    walked: set[tuple[str, str]] = set()

    def walk_type(key: tuple[str, str], tu: str) -> None:
        if key in walked:
            return
        walked.add(key)
        meta = type_meta.get(key)
        if meta is None:
            return
        cls = scope.classify(meta["def_file"], meta["decls"], port_paths)
        edges = field_edges.get(key, [])
        if cls == "wrap":
            add_type_key(key, tu)
            # `port_touched` stays keyed by tag: it comes from the port symbols'
            # `depends_on.types[].fields`, which name a tag and not a def_file.
            # On a colliding tag that pools both structs' touched sets, which
            # can only over-walk (a field name one struct lacks matches no edge).
            touched = port_touched.get(key[0], set())
            edges = [e for e in edges if e[0] in touched]
        for _field, ftype, ftype_df in edges:
            if ftype_df and (ftype, ftype_df) in type_meta:
                walk_type((ftype, ftype_df), tu)   # exact edge target
            else:
                for k in resolve(ftype, tu):
                    walk_type(k, tu)

    for key, meta in type_meta.items():
        if meta["def_file"] in port_paths:
            walk_type(key, meta["def_file"])

    # Callback typedefs (unaliased_kind == "callback" in types.csv): function-
    # pointer typedefs that are wrap-scope. They are excluded from add_type (not
    # an aggregate) and never appear in functions.csv (so ingest() misses them),
    # but they ARE part of the wrap surface — bindgen emits them and the wrap
    # stage needs safe type aliases for them. Emit each as a plain sym entry so
    # it lands in wrap.functions alongside regular functions with no extra tag.
    #
    # Reachability gate: at least one function that mentions this callback in
    # its signature (reach.functions_using_type) must itself be port-reachable
    # — the same criterion the syms_manifest uses to decide whether to emit a
    # callback entry. This avoids pulling in every callback in transitively
    # included headers (which the include-closure gate would do).
    for (tag, _cb_df), tmeta in type_meta.items():
        if tmeta.get("uak") != "callback":
            continue
        decls = tmeta["decls"]
        df = tmeta["def_file"]
        # Skip port-scope callbacks — they're already collected in
        # port.functions by scope_manifest. Only emit wrap-scope callbacks here
        # to avoid bucket overlap.
        if scope.classify(df, decls, port_paths) != "wrap":
            continue
        reach_ = idx.reach
        # Try both the real def_file and "" (callback typedefs often have
        # no def_file in the T1 table since they're header-only typedefs).
        users = reach_.functions_using_type(tag, df) | reach_.functions_using_type(tag, "")
        # Mirror the _wrap_port_reachable gate from types_manifest: a type is
        # wrap-reachable if any function that mentions it (sig or body) is
        # defined in a port file OR is port-reachable from a port file.
        body_users = reach_.functions_using_type_in_body(tag, df) | \
                     reach_.functions_using_type_in_body(tag, "")
        all_users = users | body_users
        reachable = (
            any(fn_df in port_paths for _, fn_df in all_users)
            or any(reach_.is_function_port_reachable(fn, fn_df) for fn, fn_df in all_users)
        )
        if not reachable:
            continue
        # find a port TU that actually includes a declaring header to get the
        # narrowed declared_in (mirrors narrow() used in add_sym).
        # add_sym re-checks scope.classify — bypass it and insert directly into
        # sym_items (the classify guard above already ensures wrap-scope only).
        for port_path in port_paths:
            via = [d for d in decls if d in closure(port_path)]
            if via:
                rec = sym_items.setdefault((tag, df), {
                    "name": tag, "defined_in": df, "declared_in": set()})
                rec["declared_in"].update(via)
                files.update(via)
                break

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
    for (tag, _tdf), rec in type_items.items():
        via = sorted(rec["declared_in"])
        types_out.append({
            "name": tag, "defined_in": rec["defined_in"], "declared_in": via,
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
        "types": sorted(types_out, key=lambda r: (r["name"], r["defined_in"])),
    }

