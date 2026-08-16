"""Compute the IMPORT surface for a target (scope.json `import`).

Tier (iii): a pure function of CodeQL facts + the target seed. The surface is
computed DIRECTLY from the T1 entity tables + the T2 reach graph — it does NOT
read ``syms.json`` (or ``types.json``), so ``scope.json`` is a standalone
pre-analysis artifact, independent of whether ``analyze symbols``/``types`` has
run. (``build_index`` recomputes the exact per-symbol ``depends_on`` view the
symbols composer would write, sharing ``syms_manifest``'s reach primitives, so
the result is byte-equivalent to the old post-``analyze symbols`` aggregation.)
The per-target derivation:

  1. Walk the target's entities (those whose home is a ``scope.json``
     ``target.files`` entry) and follow their ``depends_on`` edges into the
     items they use.
  2. Keep the deps that fall OUTSIDE the target — the FFI frontier.
  3. **Narrow** each item's ``declared_in`` — which is the entity-table
     *superset* of every declaration site — down to the header(s) the importing
     target TU actually ``#include``s, using the build-resolved include graph
     (``includes.csv``). A header H survives iff it declares the item **and** is
     in the transitive include-closure of the target TU that depends on it.

The result is the precise import surface (not the all-decls superset, not the
implicit not-target complement), persisted as the ``import`` section of the
target's scope.json — **derived and regenerable** from ``target`` (recompute
when it changes). An item that survives through >1 distinct header is genuinely
imported two ways: a re-export signal, recorded rather than collapsed.

Membership says only that the target does not OWN the item. What will be DONE
with it — a safe wrapper over the seam, or a native translation — is the
translate stage's objective, chosen per unit by the orchestrator.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from . import scope
from . import macro_families as _mf


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
        # target symbols, as (entity, owning_tu): the seeds we expand from
        self.target_syms: list[tuple[dict, str]] = []
        # symbol name -> canonical type tags its signature names (its
        # `depends_on.types`). For a wrap function this is its signature surface
        # (params/return); used to pull a facade's signature types into the wrap
        # closure so they get a safe wrapper instead of a raw `ffi::T`. Keyed by
        # name (external linkage ⇒ unique program-wide; a same-named static only
        # over-includes, which add_type then re-filters to wrap aggregates).
        self.sig_types: dict[str, set[str]] = {}
        # Reach object — exposed so compose_import can use its query API
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
    target_paths: set[str],
    scope_json_path,
    seed_paths: set[str] | None = None,
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
    queries them for callees of target symbols (reachable ⇒ present), so any
    extra entries are inert. ``target_syms`` is seeded FILE-level
    (``defined_in`` in a target file).
    """
    from . import syms_manifest as sm
    from .reach import Reach

    seed_paths = seed_paths or set()

    funcs = scope.load_csv(csv_dir_t1 / "functions.csv")
    globals_ = scope.load_csv(csv_dir_t1 / "globals.csv")
    macros = scope.load_csv(csv_dir_t1 / "macros.csv")
    types = scope.load_csv(csv_dir_t1 / "types.csv")
    by_name = scope.build_types_index(types)
    reach = Reach(csv_dir_t2, target_paths)

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

    tgt_funcs = scope.load_entities(scope_json_path, scope.TARGET, "functions")
    tgt_globals = scope.load_entities(scope_json_path, scope.TARGET, "globals")
    tgt_macros = scope.load_entities(scope_json_path, scope.TARGET, "macros")

    idx = _Index()

    def ingest(rows, kind_of, decls_of, target_set, gate) -> None:
        for r in rows:
            name = r.get("name")
            if not name:
                continue
            def_file = r.get("def_file") or ""
            in_target = (name, def_file) in target_set
            # Candidate gate — mirror syms_manifest pass 1 (scope_enabled,
            # not seed): an out-of-target symbol is admitted only if the
            # target reaches it and it is an allowed kind. WITHOUT this,
            # sig_types (keyed by bare name) conflates same-named statics in
            # unreachable files — e.g. a target `git_odb` reaches odb.c's
            # `normalize_options`, but blame.c/describe.c also define
            # `normalize_options`, leaking git_blame/describe_options into
            # the import surface.
            seeded = bool(seed_paths) and (
                def_file in seed_paths
                or any(d in seed_paths for d in decls_of(r)))
            if not in_target and not seeded and not gate(r):
                continue
            dep_types = sm._compose_dep_types(
                reach, name, def_file, by_name,
                field_access_index if in_target else None,
            )
            idx.sym[(name, def_file)] = {
                "kind": kind_of(r), "declared_in": decls_of(r),
            }
            sig = {t.get("type") for t in dep_types
                   if isinstance(t, dict) and t.get("type")}
            if sig:
                idx.sig_types.setdefault(name, set()).update(sig)
            if def_file in target_paths:
                dep_syms = (
                    sm._compose_dep_syms(
                        sm._forward_syms_of(name, def_file, reach), sym_index)
                    if in_target else []
                )
                idx.target_syms.append(({
                    "name": name, "defined_in": r.get("def_file") or None,
                    "depends_on": {"syms": dep_syms, "types": dep_types},
                }, def_file))

    ingest(funcs, lambda r: r["linkage"],
           lambda r: scope.parse_decl_files(r.get("decl_files") or ""),
           tgt_funcs,
           lambda r: r["linkage"] not in sm._WRAP_DISALLOWED_FN_KINDS
           and reach.is_function_port_reachable(r["name"], r["def_file"]))
    ingest(globals_, lambda r: r["linkage"],
           lambda r: scope.parse_decl_files(r.get("decl_files") or ""),
           tgt_globals,
           lambda r: r["linkage"] not in sm._WRAP_DISALLOWED_GLOBAL_KINDS
           and reach.is_global_port_reachable(r["name"], r["def_file"]))
    # A TEMPLATE GENERATOR is admitted regardless of call-site reachability.
    # The ordinary gate asks "does port code expand this macro", which a
    # generator never satisfies: it expands once at file scope in a header to
    # mint a type, and is never invoked from a function body. But its family IS
    # reached -- through the instances -- and the generic Rust type that the
    # instances alias has to be emitted by somebody. Without this the macro is
    # in neither scope section and therefore unschedulable, the same dead end
    # the gate-missed callbacks sit in.
    _generators = set(_mf.load(csv_dir_t1.parent))
    ingest(macros, lambda r: "macro",
           lambda r: [r["def_file"]] if r.get("def_file") else [],
           tgt_macros,
           lambda r: (r["name"] in _generators
                      or reach.is_macro_port_reachable(r["name"], r["def_file"])))
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


def compose_import(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    scope_json_path,
    includes_rows: list[dict],
    target_paths: set[str],
    type_rows: list[dict],
    field_type_rows: list[dict],
    seed_paths: set[str] | None = None,
) -> dict:
    """Build the ``import`` section:
    ``{files, functions, globals, macros, types}``.

    ``files`` is the narrowed import-header surface; the entity buckets list
    the reached items with their import header(s) (``declared_in``) and a
    ``reexport`` flag when an item is imported through more than one header.

    Two complementary walks out of the TARGET section:

      - **symbols** — expand each target symbol's ``depends_on`` edges (the
        callable surface plus the types its signature and body name),
        classifying each as an import (the FFI frontier) or as target (keep
        walking, via its own seed).
      - **types** — walk the CodeQL field-type graph (``type_rows`` = T1
        ``types``, ``field_type_rows`` = T2 ``field_type_uses``). A TARGET
        struct is full-scanned: it is the target's own layout, so every field
        type is a real dep. An IMPORT struct walks only the fields target code
        actually accesses (``target_touched``) — bindgen owns the rest. This
        is what pulls a by-value-embedded external type like
        ``pthread_mutex_t`` (``git_odb.lock``) into the surface.

    ``seed_paths`` inverts the seeding for a WRAP campaign
    (`scope-config.json`'s `files.import`). The target section is empty, so
    there is nothing to expand from; instead every entity those files DECLARE
    or DEFINE is an import outright, and the same two walks run from there.
    Declaration-site membership is the right test precisely because these are
    public headers: the bodies of what they declare sit in `.c` files this
    target does not name, and asking "does target code reach it" has no answer
    when there is no target code.

    Nothing here decides what will be DONE with an item; port or wrap is the
    translate stage's objective. An import is simply something the target
    does not own.
    """
    seed_paths = seed_paths or set()

    def _seeded(df: str, decls: list[str]) -> bool:
        """Is this entity named by `files.import`? Definition OR any
        declaration site — see the class docstring."""
        return bool(seed_paths) and (
            df in seed_paths or any(d in seed_paths for d in decls))
    closure = build_include_closure(includes_rows)
    idx = build_index(csv_dir_t1, csv_dir_t2, target_paths, scope_json_path,
                      seed_paths)
    type_meta = build_type_meta(type_rows)
    field_edges = build_field_edges(field_type_rows)

    # wrap item identity -> {"name"/"type", "defined_in", "declared_in": set}
    sym_items: dict[tuple[str, str], dict] = {}
    type_items: dict[tuple[str, str], dict] = {}
    files: set[str] = set()

    defs_of: dict[str, list[str]] = defaultdict(list)
    for _tag, _df in type_meta:
        defs_of[_tag].append(_df)

    def narrow(decls: list[str], tu: str | None) -> list[str]:
        """Headers declaring the item that the importing TU actually #includes.

        ``tu`` is the target TU whose dependency pulled this item in. ``None``
        marks an item reached from a TARGET entity that is not itself a TU —
        a header-declared type, or any entity on a target with no `.c` of its
        own — so there is no include graph to consult and the target's own
        file set stands in for it.

        The two fallbacks differ, and must. With a TU, missing the closure
        means the include graph had no row for it, so every declaring header
        is still a candidate. Without one it means the item is declared in no
        target file at all, reached only transitively — and handing back every
        declaring header there readmits the private headers the narrowing
        exists to exclude, while >1 survivor additionally stamps
        `reexport: true`, which sorts a private `*_local.h` ahead of the public
        header and sends scaffold to home the item under it. Such an item has
        one honest home, so return one: `canonical_decl`'s ranking (in-repo
        header > in-repo source > external, generated deprioritised)."""
        clo = (seed_paths or target_paths) if tu is None else closure(tu)
        hit = [d for d in decls if d in clo]
        if hit:
            return hit
        if tu is None:
            canon = scope.canonical_decl(decls)
            return [canon] if canon else decls
        return [d for d in decls if d.endswith((".h", ".hpp", ".hh"))] or decls

    def add_sym(name: str, df: str, decls: list[str], tu: str | None) -> None:
        if not name or scope.classify(df, decls, target_paths) != scope.IMPORT:
            return
        via = narrow(decls, tu)
        if not via:
            return  # no header to import from — a compiler builtin / intrinsic,
                    # not a wrappable FFI item.
        rec = sym_items.setdefault((name, df), {
            "name": name, "defined_in": df, "declared_in": set()})
        rec["declared_in"].update(via)
        files.update(via)

    def resolve(tag: str, tu: str | None) -> list[tuple[str, str]]:
        """The ``(tag, def_file)`` entities a bare tag may denote, restricted to
        those ``tu`` can actually see.

        Callers reach types by tag alone (``depends_on.types[].type``,
        ``sig_types``, a field's ``type_name``), but a tag can name more than one
        struct. Prefer the entities whose declaring headers this TU includes;
        failing that, the ones defined in a header — a struct defined inside a
        ``.c`` has no linkage past that TU, so it can never be an importable
        wrap item for anyone else. Both filters empty means every candidate is
        TU-local: apply that same linkage argument one step further and keep
        only a definition in ``tu`` ITSELF, because no other TU's file-static
        struct is nameable from here. Returning every candidate instead (the
        prior "err toward over-inclusion" fallback) credits one TU's port-side
        reachability to an unrelated same-tagged struct in another TU, which
        admits a phantom into the wrap closure: libgit2's ``entry`` is
        ``struct entry`` in BOTH src/libgit2/indexer.c (port) and
        deps/xdiff/xpatience.c (wrap, and touched by no port file), and the
        fallback put the xdiff one in wrap.types with no manifest record
        behind it."""
        dfs = defs_of.get(tag)
        if not dfs:
            return []
        keys = [(tag, df) for df in dfs]
        if len(keys) == 1:
            return keys
        clo = (seed_paths or target_paths) if tu is None else closure(tu)
        hit = [k for k in keys if any(d in clo for d in type_meta[k]["decls"])]
        if hit:
            return hit
        hdr = [k for k in keys if not k[1].endswith(_C_TU_SUFFIXES)]
        if hdr:
            return hdr
        return [k for k in keys if k[1] == tu]

    def add_type_key(key: tuple[str, str], tu: str | None) -> None:
        meta = type_meta.get(key)
        if meta is None:
            return  # unknown tag (not in the types tree) — never a C type
                    # bindgen binds.
        if key[0].startswith("("):
            return  # An ANONYMOUS tag (`(unnamed enum)`, `(unnamed
                    # class/struct/union)`) is a synthetic placeholder CodeQL
                    # reuses for every anonymous definition in the DB, so dozens
                    # of distinct types collide on the one string and it is not
                    # a name anything can reference or place. `_target_entities`
                    # drops them from the port section for the same reason, and
                    # the analysis tree carries none — so an entry here would
                    # match no record and be unschedulable. Their FIELDS are not
                    # lost: `entities/fields.ql` flattens an anonymous member
                    # into its named parent under a qualified name.
        if not _is_aggregate(meta):
            return  # scalar/primitive typedef (Rust primitive) or a callback
                    # (a sym, handled on the symbol surface) — not a wrap-type.
        df, decls = meta["def_file"], meta["decls"]
        if scope.classify(df, decls, target_paths) != scope.IMPORT:
            return
        via = narrow(decls, tu)
        if not via:
            return
        rec = type_items.setdefault(key, {
            "type": key[0], "defined_in": df, "declared_in": set()})
        rec["declared_in"].update(via)
        files.update(via)

    def add_type(tag: str, tu: str | None) -> None:
        for key in resolve(tag, tu):
            add_type_key(key, tu)

    # Fields of a WRAP struct that port code actually reaches into, harvested
    # from the port symbols' `depends_on.types[].fields` (composer-derived from
    # field_accesses). Drives the wrap-struct half of the field-walk.
    target_touched: dict[str, set[str]] = defaultdict(set)

    # Expand from port functions/globals/macros via their depends_on edges.
    for e, tu in idx.target_syms:
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
                target_touched[tag].add(fld)

    # Field-walk over the CodeQL type graph. PORT struct → every field type
    # (full layout reimplemented in Rust); WRAP struct → only port-touched
    # fields (bindgen owns the rest). A TARGET struct is full-scanned: its
    # definition is the target's own, so every field type is a real dep --
    # which for a struct defined in a header the config named means the fields
    # ARE the API (`git_strarray`, `git_diff_options`). One only
    # forward-declared in a target header classifies as an import and stays
    # opaque, since bindgen owns a layout nobody can see and there are no
    # accessors to write. Recurse into reached types the same way; cycle-safe
    # via `walked`. The importing TU is the seed port type's def_file (a port
    # file) or None when the seed is not a TU; narrow() falls back to a
    # canonical header there.
    walked: set[tuple[str, str]] = set()

    def walk_type(key: tuple[str, str], tu: str | None) -> None:
        if key in walked:
            return
        walked.add(key)
        meta = type_meta.get(key)
        if meta is None:
            return
        cls = scope.classify(meta["def_file"], meta["decls"], target_paths)
        edges = field_edges.get(key, [])
        if cls == scope.IMPORT:
            add_type_key(key, tu)
            # A struct whose DEFINITION is in a file the config named is a
            # public value type: its fields are the API, so every field type is
            # a real dep and the walk is full. One only forward-declared there
            # stays opaque — bindgen owns a layout nobody can see and there are
            # no accessors to write. Otherwise narrow to what target code
            # touches; `target_touched` stays keyed by tag (it comes from the
            # target symbols' `depends_on.types[].fields`, which name a tag and
            # not a def_file), so a colliding tag pools both structs' touched
            # sets — which can only over-walk, never drop.
            if meta["def_file"] not in seed_paths:
                touched = target_touched.get(key[0], set())
                edges = [e for e in edges if e[0] in touched]
        for _field, ftype, ftype_df in edges:
            if ftype_df and (ftype, ftype_df) in type_meta:
                walk_type((ftype, ftype_df), tu)   # exact edge target
            else:
                for k in resolve(ftype, tu):
                    walk_type(k, tu)

    for key, meta in type_meta.items():
        if meta["def_file"] in target_paths:
            walk_type(key, meta["def_file"])

    # SEEDED walk (`files.import`). Every entity those files name enters the
    # section outright, and its signature / field types follow. `tu=None`: a
    # seed has no importing TU, so narrow() resolves against the seed set —
    # which for a public header set is exactly what a consumer #includes.
    if seed_paths:
        for (name, df), meta in list(idx.sym.items()):
            if not _seeded(df, meta["declared_in"]):
                continue
            add_sym(name, df, meta["declared_in"], None)
            for st in idx.sig_types.get(name, ()):
                add_type(st, None)
        for key, meta in list(type_meta.items()):
            if _seeded(key[1], meta["decls"]):
                walk_type(key, None)

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
        if scope.classify(df, decls, target_paths) != scope.IMPORT:
            continue
        # A callback declared in a TARGET file needs no reachability: the
        # config named the header, so the API it is a parameter of is in scope
        # whether or not anything calls through it.
        in_target = bool(df in target_paths or any(d in target_paths for d in decls)
                         or _seeded(df, decls))
        reach_ = idx.reach
        if not in_target:
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
                any(fn_df in target_paths for _, fn_df in all_users)
                or any(reach_.is_function_port_reachable(fn, fn_df) for fn, fn_df in all_users)
            )
            if not reachable:
                continue
        # find a port TU that actually includes a declaring header to get the
        # narrowed declared_in (mirrors narrow() used in add_sym); a
        # target-declared callback narrows against the target file set instead,
        # having no importing TU.
        # add_sym re-checks scope.classify — bypass it and insert directly into
        # sym_items (the classify guard above already ensures wrap-scope only).
        for src in ([None] if in_target else list(target_paths)):
            via = narrow(decls, src) if src is None else \
                [d for d in decls if d in closure(src)]
            if via:
                rec = sym_items.setdefault((tag, df), {
                    "name": tag, "defined_in": df, "declared_in": set()})
                rec["declared_in"].update(via)
                files.update(via)
                break

    # Template-generator macros. `ingest` admits them to the closure index, but
    # the emit above only reaches a symbol through some port entity's
    # `depends_on` -- and a generator is never a callee: it expands once at file
    # scope to mint a type. Its family IS reached, through the instances, and the
    # generic those instances alias has to be emitted by somebody, so emit the
    # macro itself. Same shape as the callback path above: narrow `declared_in`
    # to the header a target TU actually includes, or admit the generator when
    # its own defining header is itself a target file, the include test having
    # nothing to run against.
    for _macro, _fam in _mf.load(csv_dir_t1.parent).items():
        _df = _fam["def_file"]
        if not _df or (_macro, _df) in sym_items:
            continue
        # Relevance, not a member COUNT: admit a generator only when this target
        # actually reaches one of its instances. A count threshold is a function
        # of the extracted build (`entry_short` mints two types in source, one
        # here, because the SHA256 pair is behind an #ifdef), whereas "does the
        # closure contain a member" is a fact about this target. Without it,
        # dropping the count let PCRE2_STRUCTURE_LIST and DEFINE_LHASH_OF_INTERNAL
        # in with zero reachable members.
        # Relevance spans BOTH scopes: `type_items` is the wrap closure's own
        # set, and every khash instance is port-scope, so testing against it
        # alone rejected all 25. A member counts as reached when it is a known
        # type defined inside this target's port files, or already in the wrap
        # closure. `classify` is NOT the test -- it would call any system header
        # an import whether or not this target reaches it, readmitting PCRE2 and
        # LHASH with zero reachable members.
        _reached = any(
            (tag, df) in type_items
            or df in target_paths
            for tag, df in _fam["members"])
        if not _reached:
            continue
        if _df not in target_paths and _df not in seed_paths and not any(
                _df in closure(p) for p in target_paths):
            continue
        rec = sym_items.setdefault((_macro, _df), {
            "name": _macro, "defined_in": _df, "declared_in": set()})
        rec["declared_in"].add(_df)
        files.add(_df)

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
        if rdf in target_paths:
            # The null-def entry got in through `classify`'s decl fallback,
            # which reads the DECLARING header — and a public header sits
            # outside the port set even when the body does not. Resolving the
            # definition settles it the way `classify` would have with the
            # def_file in hand: the entity is port-scope, so it is dropped
            # rather than re-keyed, which would otherwise file a port symbol
            # under wrap with a port `defined_in`. Unreachable while a null
            # def_file means "no body in the database" (a prototype-only call
            # site still records the resolved `.c`), so this enforces an
            # invariant the data currently supplies on its own.
            continue
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
        "seeds": sorted(seed_paths),
        "_comment": (
            "The IMPORT section: everything the TARGET reaches that "
            "`scope-config.json`'s `files` does not name — the FFI frontier. "
            "DERIVED and regenerable from `target`; recompute when it changes. "
            "Computed by compose/import_closure.py by expanding each target "
            "symbol's `depends_on` edges and walking the CodeQL field-type "
            "graph. `declared_in` is narrowed to the header(s) the importing "
            "target TU actually #includes (build-resolved), or to one "
            "canonical header when the seed is not a TU. `reexport: true` "
            "marks an item imported through >1 header. Membership here says "
            "the target does not OWN the item, not what will be done with it: "
            "port or wrap is the translate stage's objective. `seeds` is "
            "non-empty for a WRAP campaign (`files.import`): the section is "
            "then seeded off those files directly rather than expanded from a "
            "target, and a struct DEFINED in one of them keeps its full field "
            "layout while a forward-declared one stays opaque."
        ),
        "files": sorted(files),
        "functions": sorted(buckets["functions"], key=lambda r: (r["name"], r["defined_in"])),
        "globals": sorted(buckets["globals"], key=lambda r: (r["name"], r["defined_in"])),
        "macros": sorted(buckets["macros"], key=lambda r: (r["name"], r["defined_in"])),
        "types": sorted(types_out, key=lambda r: (r["name"], r["defined_in"])),
    }

