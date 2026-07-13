"""crustify query — read-only oracle over the analysis tree.

Lists types / symbols filtered by scope, synthetic kind, dag layer,
name, and source file. It is the policy/inspection surface: the action commands
(``wrap`` / ``port``) are scope-mechanisms, and you pipe ``query`` output into
their ``--name``. Pure read — no side effects.

Output modes:
  * plain (default): one bare ``id`` per line, deduped, sorted by ``(layer, id)``
    — xargs-ready (``crustify <t> query types --wrap-only | xargs crustify <t> wrap --name``).
    Name collisions (same-named statics in different TUs, or a type/symbol tag
    clash) print the id once; use ``--file`` to target one, or ``--json`` to see
    the multiplicity.
  * ``--json``: one record per ``(id, defined_in)`` — collision-explicit —
    carrying ``{id, kind, subkind, scope, layer, defined_in}``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from crustify.layout import manifest_name

# The composer package lives at ``utils/codeql/compose/`` in the crustify
# checkout, not as an installed package. Put its parent on sys.path so
# ``from compose import scope`` works from this orchestrator (mirror wrap.py).
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))

_SYNTH = ("string", "array")


def query(
    target: Path,
    *,
    subject: str,                       # "types" | "syms"
    names: list[str] | None = None,
    files: list[str] | None = None,
    wrap_only: bool = False,
    port_only: bool = False,
    scope_only: bool = False,
    strings: bool = False,
    arrays: bool = False,
    typegens: bool = False,
    lifetime: bool = False,
    fields: bool = False,
    ops: bool = False,
    methods: bool = False,
    accessors: bool = False,
    update: str | None = None,
    update_help: bool = False,
    schema: bool = False,
    create: str | None = None,
    rs: bool = False,
    manifest: bool = False,
    rng: str | None = None,
    with_details: bool = False,
) -> None:
    """Read-only oracle over types / symbols, resolved from the composer
    manifest (dag-free; graph walks live in ``query dag``).

      * no ``--name``    → **enumerate** the (filtered) entries — bare names,
                           or whole records with ``--with-details``.
      * ``--name T``     → **introspect** one — a summary record by default,
                           the whole record with ``--with-details``;
                           ``--fields`` / ``--ops`` (types) print
                           its windowable lists. (The entry's ``.rs`` module is
                           found via ``scaffold --name``, not here.)
      * ``--name A B …`` → several records at once (no facet).
    """
    kind = "type" if subject == "types" else "symbol"
    # --update-help: print the findings schema the agent submits via --update,
    # then return. No --name (it describes the schema, not an entry) — sibling of
    # --update for schema discovery at runtime.
    if update_help:
        print(json.dumps(_findings_schema(kind), indent=2))
        return
    # --schema: the record's field/slot MEANING (docs/schemas/<types|syms>.md);
    # no --name. Distinct from --update-help, which gives the submission shape.
    if schema:
        print(_schema(kind))
        return
    name_list = list(names or [])
    # --create homes a WHOLE synthetic cluster (its name is IN the entry), so it
    # takes no --name and bypasses resolution.
    if create is not None:
        if subject != "types":
            raise SystemExit(
                "query: --create applies to types only (synthetic clusters).")
        from crustify.layout import Layout
        _create_type(Layout.discover(target), target, create)
        return
    type_facets = fields or ops or methods or accessors
    if type_facets and subject != "types":
        raise SystemExit("query syms: --fields/--ops/--methods/--accessors apply to types only.")
    if typegens and subject != "syms":
        raise SystemExit("query types: --typegens applies to syms only (macros with macro.typegen).")
    if lifetime and subject != "syms":
        raise SystemExit("query types: --lifetime applies to syms only (the raw "
                         "lifecycle primitives). A type's lifecycle is its whole "
                         "`lifetime` block; query it with --with-details.")
    if (type_facets or manifest or update is not None) and len(name_list) != 1:
        raise SystemExit(
            f"query {subject}: facets / --manifest / --update need exactly one --name.")
    if name_list:
        _introspect(target, kind=kind, names=name_list, files=files,
                    fields=fields, ops=ops, methods=methods, accessors=accessors,
                    update=update, manifest=manifest,
                    rng=rng, wrap_only=wrap_only, port_only=port_only,
                    scope_only=scope_only, with_details=with_details)
    else:
        _enumerate(target, kind=kind, files=files,
                   wrap_only=wrap_only, port_only=port_only,
                   strings=strings, arrays=arrays, typegens=typegens,
                   lifetime=lifetime, with_details=with_details)


def _summarize(entry: dict | None, kind: str) -> dict | None:
    """The light view: identity + lifecycle + name-only lists, dropping the
    heavy per-element analysis (per-field ``ptr`` blocks / footprints for types,
    per-arg ownership + ret for symbols). ``--with-details`` returns the whole
    record instead."""
    if not entry:
        return entry
    if kind == "type":
        from compose import scope
        keep = ("type", "typedef", "kind", "declared_in", "defined_in", "casted")
        s = {k: entry[k] for k in keep if k in entry}
        lc = scope.lifetime(entry)
        s["lifetime"] = {"allocs": scope.alloc_fns(lc),
                         **{k: lc.get(k) for k in
                            ("clone", "drop",
                             "locking", "conditional_drop")}}
        s["fields"] = [f.get("name") for f in entry.get("fields") or []]
        # Method surface: lifecycle ops for a concrete type; the explicit `ops`
        # list for a synthetic cluster. (Field accessors are not part of it —
        # the wrapper derives them from the field layout.)
        s["ops"] = scope.type_method_syms(entry)
        return s
    # the symbol's signature lives in `type` (some manifests use `signature`).
    # `macro` / `lifetime` are small agent-facet blocks (not the heavy per-arg
    # ptr analysis), so they ride the light view — matching the type branch's
    # lifecycle summary and this function's "identity + lifecycle" contract.
    keep = ("name", "kind", "defined_in", "declared_in",
            "type", "signature", "macro", "lifetime")
    return {k: entry[k] for k in keep if k in entry}


def _enumerate(
    target: Path, *, kind: str, files, wrap_only, port_only,
    strings, arrays, typegens=False, lifetime=False, with_details=False,
) -> None:
    """List the (filtered) type/symbol entries straight from the manifest — one
    ``name<TAB>defined_in<TAB>declared_in`` line each (the placement provenance),
    or whole records with ``--with-details``."""
    from compose import scope
    from crustify.layout import Layout

    layout = Layout.discover(target)
    sj = layout.scope(target)
    synth_sel = {k for k, on in zip(_SYNTH, (strings, arrays)) if on}
    file_set = set(files or [])
    arr, tagkey = (("types", "name") if kind == "type"
                   else ("symbols", "name"))
    manifest = manifest_name(kind)

    # Scope membership is read straight from scope.json — the authoritative,
    # deduped port/wrap closures — NOT a re-derived "not-port ⇒ wrap"
    # classification over the whole analysis tree. Keyed by origin
    # (defined_in or canonical_decl(declared_in)). This excludes out-of-closure
    # files (test/) and collapses null-def extern twins (only the real def is in
    # scope.json). Empty when scope.json is absent, so --port-only/--wrap-only
    # yield nothing for an unscoped target (e.g. _root) rather than mislabeling.
    # Synthetic types (string/array clusters) are NOT in scope.json — they are
    # *always* wrap-scope, classified by kind here.
    sub = ("types",) if kind == "type" else ("functions", "globals", "macros")
    port_keys = (scope.scope_membership(sj, "port", kinds=sub)
                 if port_only and sj.exists() else set())
    wrap_keys = (scope.scope_membership(sj, "wrap", kinds=sub)
                 if wrap_only and sj.exists() else set())

    rows: list[dict] = []
    for f in layout.analysis.rglob(manifest):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get(arr, []):
            tag = e.get(tagkey)
            if not tag:
                continue
            sk = str(e.get("kind") or "symbol")
            d = e.get("defined_in") or ""
            decls = e.get("declared_in")
            # Synthetic types — string/array clusters — are always wrap-scope,
            # never in scope.json, never port. Generated-container instances
            # (kind: struct, concrete) are NOT synthetic: they flow through
            # scope.json membership.
            is_synth = sk in scope.SYNTHETIC_KINDS
            # Match this row's origin_key against the scope set. A type may be
            # listed by a typedef alias (EXT_RETURN) while the manifest uses its
            # tag (ext_return_en), so try the tag OR any typedef. (Anonymous
            # types have no placeable tag, are absent from the manifest, dropped.)
            cands = ((tag,) if kind != "type"
                     else (tag, *(e.get("typedef") or [])))
            if wrap_only and not (is_synth or any(
                    scope.origin_key(c, d, decls) in wrap_keys for c in cands)):
                continue
            if port_only and (is_synth or not any(
                    scope.origin_key(c, d, decls) in port_keys for c in cands)):
                continue
            if lifetime:
                # Lifecycle-primitive filter: keep only entries carrying a
                # `lifetime` block; --strings/--arrays reinterpret as a synth
                # filter on that block (alloc/clone), not the entry kind.
                lt = e.get("lifetime")
                if not lt:
                    continue
                if synth_sel:
                    got = (lt.get("alloc") or lt.get("clone") or {}).get("synth")
                    if got not in synth_sel:
                        continue
            elif synth_sel and sk not in synth_sel:
                continue
            if typegens and not ((e.get("macro") or {}).get("typegen")):
                continue
            if file_set and d not in file_set:
                continue
            rows.append(e)

    rows.sort(key=lambda e: (e.get(tagkey) or "", e.get("defined_in") or ""))

    # `--lifetime` prints full records by default (like the `query mem` it
    # replaces): you want each primitive's block in hand, not just its name.
    if with_details or lifetime:
        print(json.dumps(rows, indent=2))
        return
    # Plain output: one TSV line per (name, kind, defined_in, declared_in) — the
    # provenance the scaffolder places on, plus the `kind` it buckets by
    # (function_* → functions, macro_* → macros, global_* → globals; a type's
    # struct/enum/callback/string/array all → types). `defined_in` is
    # empty for a TU-less entity; `declared_in` is comma-joined. Deduped by
    # (name, defined_in) since a file-local static can repeat a bare name across
    # TUs. Use `--with-details` for the whole record.
    seen: set = set()
    for e in rows:
        tag = e.get(tagkey)
        d = e.get("defined_in") or ""
        if (tag, d) in seen:
            continue
        seen.add((tag, d))
        decls = e.get("declared_in")
        if isinstance(decls, str):
            decls = [decls]
        k = e.get("kind") or ("symbol" if kind == "symbol" else "struct")
        print(f"{tag}\t{k}\t{d}\t{','.join(decls or [])}")


def _window(seq: list, rng: str | None) -> list:
    rg = _parse_range(rng)
    if rg is None:
        return seq
    a, b = rg
    return seq[a:b]


def _introspect(
    target: Path, *, kind: str, names, files, fields, ops, manifest, rng,
    wrap_only, port_only, with_details, methods=False, accessors=False,
    update=None, scope_only=False,
) -> None:
    """One named entity's record (summary / whole), or — for a single type —
    its windowable ``--fields`` / ``--ops`` / ``--methods`` / ``--accessors``
    lists, the ``--manifest`` types.json that homes it, or a ``--update``
    findings ingest. (The entry's ``.rs`` module is found via ``scaffold
    --name``.)"""
    from crustify import _schedule as S

    if fields or ops or methods or accessors or manifest or update is not None:
        layout, node, by_key = _resolve(target, kind=kind, name=names[0], files=files)
        if manifest:
            p = _manifest_path(layout.analysis, kind, node.id, node.defined_in)
            if p is None:
                raise SystemExit(
                    f"query {kind}: no manifest file for {node.id!r}.")
            print(p)
            return
        if update is not None:
            if kind == "type":
                _update_type(layout, target, node.id, node.defined_in, update)
            else:
                _update_sym(layout, target, node.id, node.defined_in, update)
            return
        if methods:
            # The candidate pool for lifecycle + accessor discovery: the
            # type's COMPLETE opaque_in ∪ non_opaque_in footprint. Returned
            # whole by default (lifecycle routines often live OUTSIDE the
            # target's scope — e.g. SSL_free in ssl_lib.c for an ssl/statem
            # target). --port-only / --wrap-only intersect with that scope's
            # functions (scope.json membership). Schema-agnostic — the agent
            # never opens the manifest.
            entry = _load_type_entry(layout.analysis, node.id, node.defined_in) or {}
            pool = {s for grp in ("opaque_in", "non_opaque_in")
                    for syms in (entry.get(grp) or {}).values() for s in syms}
            sj = layout.scope(target)
            if (wrap_only or port_only) and sj.exists():
                from compose import scope as _sc
                keep = {k[0] for k in _sc.scope_membership(
                    sj, "wrap" if wrap_only else "port",
                    kinds=("functions", "globals", "macros"))}
                pool &= keep
            win = _window(sorted(pool), rng)
            print(json.dumps(win, indent=2) if with_details
                  else ("\n".join(win) if win else "[]"))
            return
        if accessors:
            # {field: [touchers]} — the type's fields (ALL declared by default;
            # --port-only/--wrap-only narrow to fields touched by that scope's
            # code) mapped to the COMPLETE, UNfiltered set of functions that
            # touch each field (raw t2/field_accesses.csv). The toucher set is
            # never scope-filtered: a field touched in-scope via raw `obj->field`
            # while its real accessor is out of scope still surfaces here.
            _accessors(layout, target, node.id, node.defined_in,
                       wrap_only=wrap_only, port_only=port_only,
                       scope_only=scope_only)
            return
        meta = S.load_type_meta(layout.analysis)
        flds, lifecycle = meta.get(node.id, ([], set()))
        if fields:
            entry = _load_type_entry(layout.analysis, node.id, node.defined_in)
            objs = (entry.get("fields") if entry else None) or [{"name": f} for f in flds]
            # ALL declared fields by default; --port-only/--wrap-only/--scope-only
            # narrow to fields touched by that scope's code (raw field_accesses ∩
            # scope.json membership). --scope-only = port ∪ wrap, but a PORT-scope
            # type keeps ALL fields (native reimplementation — see _field_keep_set).
            keep = _field_keep_set(layout, target, node.id, node.defined_in,
                                   wrap_only=wrap_only, port_only=port_only,
                                   scope_only=scope_only)
            if keep is not None:
                objs = [o for o in objs if o.get("name") in keep]
            win = _window(objs, rng)
            if with_details:
                print(json.dumps(win, indent=2))
            else:
                names = [o.get("name", "") for o in win if o.get("name")]
                print("\n".join(names) if names else "[]")
            return
        # --ops: names only, lifecycle-first, scope-filterable via scope.json
        # membership (same oracle as enumeration / wrap / port).
        op_pred = lambda _n: True            # noqa: E731
        if wrap_only or port_only:
            from compose import scope as _sc
            sj = layout.scope(target)
            op_pred = (_sc.in_scope_pred(sj, "wrap" if wrap_only else "port")
                       if sj.exists() else (lambda _n: False))
        win = _window(S.ordered_ops(node, by_key, lifecycle, op_pred), rng)
        if with_details:
            # name + the TU/header that defines each op — the scaffolder reads
            # this to find a type's *implementing TU* (the `.c` among its ops'
            # defined_in). No `.c` here ⇒ fully header-implemented.
            print(json.dumps(
                [{"id": o.id, "defined_in": o.defined_in, "kind": o.subkind}
                 for o in win], indent=2))
        else:
            print("\n".join(o.id for o in win))
        return

    # record(s): summary by default, whole record with --with-details.  # noqa: E501
    return _records(target, kind, names, files, with_details)


def _scope_touched_fields(layout, target, tag: str, defined_in: str | None,
                          which: str) -> set:
    """Field names of `tag` touched by some function in scope `which`
    (port|wrap|scope) — raw ``t2/field_accesses`` ∩ scope.json membership.
    ``scope`` is the union (port ∪ wrap) — fields any in-scope code touches.
    Empty if no scope.json. Drives the --port-only/--wrap-only/--scope-only
    narrowing for --fields and --accessors."""
    import csv as _csv
    from compose import scope as _sc
    sj = layout.scope(target)
    if not sj.exists():
        return set()
    whichs = ("port", "wrap") if which == "scope" else (which,)
    funcs = {k[0] for w in whichs for k in _sc.scope_membership(
        sj, w, kinds=("functions", "globals", "macros"))}
    out: set = set()
    fac = layout.t2 / "field_accesses.csv"
    if fac.exists():
        with fac.open() as fh:
            for r in _csv.DictReader(fh):
                if r.get("struct_name") != tag:
                    continue
                if defined_in and r.get("struct_def_file") \
                        and r["struct_def_file"] != defined_in:
                    continue
                if r.get("enclosing_name") in funcs and r.get("field_name"):
                    out.add(r["field_name"])
    return out


def _is_port_scope_type(layout, target, tag: str, defined_in: str | None) -> bool:
    """True iff `tag` is a PORT-scope type in scope.json (reimplemented
    natively, so its whole layout is in scope). False if no scope.json."""
    from compose import scope as _sc
    sj = layout.scope(target)
    if not sj.exists():
        return False
    return _sc.origin_key(tag, defined_in or None, None) in _sc.scope_membership(
        sj, "port", kinds=("types",))


def _field_keep_set(layout, target, tag: str, defined_in: str | None, *,
                    wrap_only: bool, port_only: bool, scope_only: bool) -> set | None:
    """Field-name keep-set for the -only narrowing, or None = keep ALL fields.

      - --wrap-only / --port-only → fields touched by that scope's code.
      - --scope-only → fields touched by ANY in-scope code (port ∪ wrap) for a
        WRAP-scope type; ALL fields for a PORT-scope type — a port type is
        reimplemented natively, so every field is in scope, touched or not.
    """
    if not (wrap_only or port_only or scope_only):
        return None
    if scope_only and _is_port_scope_type(layout, target, tag, defined_in):
        return None
    which = "wrap" if wrap_only else "port" if port_only else "scope"
    return _scope_touched_fields(layout, target, tag, defined_in, which)


def _accessors(layout, target, tag: str, defined_in: str | None, *,
               wrap_only: bool = False, port_only: bool = False,
               scope_only: bool = False) -> None:
    """``{field: [touchers]}`` for the type's fields.

    ALL declared fields by default; --port-only/--wrap-only/--scope-only narrow
    the FIELD set to the ones touched by that scope's code (--scope-only =
    port ∪ wrap; a port-scope type keeps all fields — see _field_keep_set).
    Each field's toucher set is the COMPLETE, UNfiltered set of functions that
    access it — read straight from the raw ``t2/field_accesses`` edge, NOT the
    port-scope ``depends_on`` inversion. So an accessor that is itself out of
    scope (while the field is touched in-scope via raw ``obj->field``) still
    surfaces as a candidate."""
    import csv as _csv
    from collections import defaultdict

    entry = _load_type_entry(layout.analysis, tag, defined_in) or {}
    declared = [f.get("name") for f in entry.get("fields") or [] if f.get("name")]

    complete: dict[str, set] = defaultdict(set)
    fac = layout.t2 / "field_accesses.csv"
    if fac.exists():
        with fac.open() as fh:
            for r in _csv.DictReader(fh):
                if r.get("struct_name") != tag:
                    continue
                if defined_in and r.get("struct_def_file") \
                        and r["struct_def_file"] != defined_in:
                    continue
                fld, fn = r.get("field_name"), r.get("enclosing_name")
                if fld and fn:
                    complete[fld].add(fn)

    keep = _field_keep_set(layout, target, tag, defined_in,
                           wrap_only=wrap_only, port_only=port_only,
                           scope_only=scope_only)
    scoped = set(declared) if keep is None else {f for f in declared if f in keep}

    out = {f: sorted(complete.get(f, set())) for f in sorted(scoped)}
    print(json.dumps({"name": tag, "fields": out}, indent=2))


# ----------------------------------------------------------- --update ingest

_LIFECYCLE_KEYS = ("allocs", "clone", "drop", "locking",
                   "conditional_drop")
_FINDINGS_TOP = set(_LIFECYCLE_KEYS) | {"fields", "_comment_agent"}
_FIELD_AGENT_KEYS = {"ptr"}

# --create ingest (buffer pass): a whole synthetic string/array cluster entry.
# A cluster records the CVec strategy family: its identity, the byte-level
# `allocs` that produce the raw sized buffer, the `dtor` that drives the
# CLenDropped release ZST, and (array-only) `elems` for the typed CVec<T> aliases.
# It carries NO `ops` list: realloc/memdup/cleanse and the higher-level
# duplicating constructors are ordinary FREE FUNCTIONS wrapped by the symbol
# wrapper, never claimed by the cluster (the recorded `allocs` are metadata for
# the strategy/aliases -- the allocator functions are still wrapped as free
# syms). Length-aware teardown is read off the dtor's signature by the wrapper,
# so there is no stored `len_aware_drop`.
_SYNTH_CREATE_KINDS = {"string", "array"}
_CREATE_TOP = {
    "name", "type", "kind", "declared_in", "defined_in", "_comment_agent",
    "allocs", "clone", "drop", "locking", "conditional_drop",
    # ARRAY-only element surface: `elems` lists the concrete element types the
    # buffer holds at call sites (rows {type, note}) for the wrapper's typed
    # CVec<T> aliases. Element OWNERSHIP (drop-each-element) is a PORT concern
    # (ptr.owned_elem), never here. A string carries no `elems` (single buffer).
    "elems"}

# Symbol findings (functions / callbacks / macros) — the agent-fillable surface.
_SYM_FINDINGS_TOP = {"macro", "lifetime", "ptr_args", "ptr_ret", "forks"}
# Same structured ownership block as a struct field's `ptr` (see types.md#ptr):
# `owned` nests {exclusive, shared}, `borrowed` nests {lifetime}, `array` is
# null|{by_val}|{by_ref:{owned,borrowed}}. A pointer at a call boundary and a
# pointer in a struct extract identical properties, so args and returns share it.
_PTR_ARG_AGENT_KEYS = {"array", "string", "owned", "borrowed", "nullable",
                       "mutable", "note"}
_PTR_RET_AGENT_KEYS = _PTR_ARG_AGENT_KEYS
_FORK_KEYS = {"ptr_args", "ptr_ret", "callsites"}


# `types.json` is shared with the synthetic string/array-cluster analyzers; the
# struct type-analyzer's `--schema` excludes their cluster-only fields.
_SYNTHETIC_SCHEMA_FIELDS = {"array_fields"}


def _schema(kind: str) -> str:
    """Field/slot MEANING for ``--schema`` — display-only markdown the analyzer
    reads at runtime instead of opening the templates. Distinct from
    ``--update-help`` (:func:`_findings_schema`), which gives the submission
    *shape* + rules; meaning and shape are never duplicated.

    Primary source: ``docs/schemas/<types|syms>.md``, split on ``## <field>``
    headings. The struct type-analyzer drops cluster-only sections (the
    synthetic string/array ``array_fields``), self-identified by an in-body
    ``*(cluster-only`` tag (with :data:`_SYNTHETIC_SCHEMA_FIELDS` as an explicit
    backstop). Falls back to the template's legacy ``_comment_*`` blocks
    (rendered as markdown) until a given kind's ``.md`` exists, so the migration
    off the templates is phased. Empty string if neither source is readable."""
    root = Path(__file__).resolve().parents[2]
    doc = root / "docs" / "schemas" / ("types.md" if kind == "type" else "syms.md")
    if doc.exists():
        try:
            text = doc.read_text()
        except OSError:
            return ""
        parts = re.split(r"(?m)^## (\S+)\s*$", text)
        preamble, sections = parts[0], list(zip(parts[1::2], parts[2::2]))
        keep = [preamble.rstrip()]
        for field, body in sections:
            cluster_only = (kind == "type" and (field in _SYNTHETIC_SCHEMA_FIELDS
                            or body.lstrip().startswith("*(cluster-only")))
            if cluster_only:
                continue
            keep.append(f"## {field}\n{body.rstrip()}")
        return "\n\n".join(s for s in keep if s).rstrip() + "\n"
    # Fallback: legacy template `_comment_*` blocks -> markdown, until <kind>.md.
    tmpl = root / "templates" / ("types.json" if kind == "type" else "syms.json")
    try:
        rec = json.loads(tmpl.read_text())
    except (OSError, ValueError):
        return ""
    out: list[str] = []
    for k, v in rec.items():
        if not k.startswith("_comment"):
            continue
        field = k[len("_comment_"):] or "overview"
        if kind == "type" and field in _SYNTHETIC_SCHEMA_FIELDS:
            continue
        body = " ".join(v) if isinstance(v, list) else v
        out.append(f"## {field}\n{body}")
    return "\n\n".join(out).rstrip() + "\n"


def _findings_schema(kind: str) -> dict:
    """The findings JSON an analyzer agent submits through ``--update`` — the
    schema boundary, returned by ``--update-help`` so the agent discovers it at
    runtime instead of hard-coding it. The top-level key sets are the validator's
    own (``_FINDINGS_TOP`` / ``_SYM_FINDINGS_TOP``), so this never drifts from
    what ``--update`` actually accepts."""
    if kind == "type":
        return {
            "_subject": "type",
            "_doc": ("Submit ONLY the lifecycle slots / fields you are filling. "
                     "Merge is partial, idempotent, and lock-serialized; "
                     "unmentioned slots/fields are left untouched. Unknown "
                     "top-level keys are hard-rejected. (Lifecycle slots are "
                     "stored under the entry's `lifetime` block, but you submit "
                     "them FLAT, as shown.)"),
            "allocs": ["<byte-level allocator fn that produces a raw T (heap types: "
                       "malloc/calloc-family from alloc.json; may be several for "
                       "polymorphic backends or a heap/mmap split). Empty for "
                       "stack/embedded types. Higher-level open/lookup/init logic "
                       "stays a free function, NOT here.>"],
            "clone": {
                "shared": "<refcount-bump fn (the up_ref) -> CRefCloned / CArc Clone> | null",
                "exclusive": ["<deep-copy / dup fn -> CCloned / CBox Clone>"],
            },
            "drop": {
                "exclusive": ["<exclusive destructor fn>"],
                "shared": ["<shared/refcounted destructor fn>"],
                "fields": ["<fields-only disposer fn>"],
            },
            "locking": [{
                "acquire": "<lock fn>", "release": "<unlock fn>",
                "locks": ["<field storing the lock object>"],
                "locked_fields": ["<field the lock guards>"],
            }],  # or null; a LIST -- one entry per distinct lock/field discipline
            "conditional_drop": "<drop-condition descriptor> | null",
            "fields": {
                "<field_name>": {
                    "ptr": {
                        "array": "null | {by_val: true} | {by_ref: {owned: bool, borrowed: bool}}",
                        "string": "bool",
                        "owned": "null | {exclusive: bool, shared: bool}",
                        "borrowed": "null | {lifetime: <source>}",
                        "nullable": "bool", "mutable": "bool", "note": "<str>",
                    }
                }
            },
            "_comment_agent": "<your rationale (optional free text)>",
            "_rules": [
                "drop: each role (shared / exclusive / fields) is a LIST and may "
                "name several destructors of that kind; a single C function must "
                "not appear in two different roles (roles stay disjoint). A "
                "parameterized free (one taking an element-free callback, e.g. "
                "OPENSSL_sk_pop_free) is STILL a destructor - the owned-element "
                "variant - not disqualified.",
                "every named op must be a real function, not a macro "
                "(record the underlying function it expands to).",
                "field.ptr: `array` = null | {by_val:true} | {by_ref:{owned,"
                "borrowed}}, where by_ref is a pointer array (container) and its "
                "owned/borrowed is ELEMENT ownership. `owned` = null | {exclusive,"
                "shared} (the pointer/buffer ownership); `borrowed` = null | "
                "{lifetime}. `owned` and `borrowed` MAY both be non-null "
                "(runtime-conditional dual ownership); likewise owned.exclusive + "
                "owned.shared, and by_ref.owned + by_ref.borrowed. Still enforced: "
                "string XOR array; a borrowed pointer requires a lifetime; a const "
                "pointee implies mutable != true.",
            ],
            "_valid_top_keys": sorted(_FINDINGS_TOP),
        }
    return {
        "_subject": "symbol",
        "_doc": ("Submit ONLY the facets you are filling. Merge is partial and "
                 "idempotent. Unknown top-level keys are hard-rejected. `kind` is "
                 "composer-fixed and is never submitted; `macro` is for MACROS "
                 "only — functions / globals / callbacks must omit it."),
        "macro": {
            "alias": "<bool: expands to existing symbol(s)/type(s)>",
            "const": "<bool: expands to a compile-time constant>",
            "typegen": "<bool: declares new types and/or their ops>",
        },
        "_lifetime_note": ("The lifecycle-primitive block: null for an ordinary "
                           "symbol, else EXACTLY ONE of alloc / clone / refcount "
                           "/ lock (the subfield name IS the primitive kind). See "
                           "`query syms --schema`, ## lifetime."),
        "lifetime": {
            "alloc": {"role": "'allocator' | 'free'",
                      "freed_by": "[<free names>] (allocator only)",
                      "synth": "'string' | 'array' (allocator only)"},
            "clone": {"freed_by": "[<free names>]", "synth": "'string' | 'array'"},
            "refcount": {"op": "new|up|down|get|free|assert", "cluster": "<type name>"},
            "lock": {"op": "new|read_lock|write_lock|unlock|free", "cluster": "<type name>"},
        },
        "_ptr_note": ("`ptr_args` records and `ptr_ret` carry the SAME ownership "
                      "block — identical to a struct field's `ptr` (see "
                      "`query types --schema`, ## ptr). `owned` gives CBox vs "
                      "CArc; `borrowed.lifetime` sources are arg-oriented "
                      "(`arg:<name>`, `arg:<name>->path`, `static`, `other`)."),
        "ptr_args": {
            "<position:int>": {
                "array": "null | {by_val: true} | {by_ref: {owned: bool, borrowed: bool}}",
                "string": "bool",
                "owned": "null | {exclusive: bool, shared: bool}",
                "borrowed": "null | {lifetime: <source>}",
                "nullable": "bool", "mutable": "bool", "note": "<str>",
            }
        },
        "ptr_ret": "<same block as one ptr_args record> | null",
        "forks": [{
            "ptr_args": "<as ptr_args above>",
            "ptr_ret": "<as ptr_ret above>",
            "callsites": ["<callsite id>"],
        }],
        "_rules": [
            "macro: macros only; all three flags required, each a bool. They are "
            "independent (a macro may be none of them — that is the shape that "
            "needs a C shim, since bindgen binds a const, an alias' target, and "
            "a typegen's expansion).",
            "lifetime: null, or EXACTLY ONE of alloc/clone/refcount/lock. "
            "alloc.role='allocator' needs synth (string|array); role='free' takes "
            "neither freed_by nor synth. clone needs synth. refcount/lock need "
            "op (from their enum) + cluster. Any symbol kind may carry it (a "
            "refcount 'assert' may be a macro).",
            "ptr_ret only on a pointer-returning symbol.",
            "ptr: array = null | {by_val:true} | {by_ref:{owned,borrowed}} (by_ref "
            "is a container; its owned/borrowed is ELEMENT ownership). string XOR "
            "array; a const pointee implies mutable != true; a borrowed pointer "
            "needs a lifetime; exclusive/shared only under owned. owned + borrowed "
            "MAY both be non-null (runtime-conditional dual ownership); likewise "
            "owned.exclusive + owned.shared and by_ref.owned + by_ref.borrowed.",
            "forks: callbacks only; each callsite claimed by exactly one entry.",
        ],
        "_valid_top_keys": sorted(_SYM_FINDINGS_TOP),
    }
_MACRO_FLAGS = frozenset({"alias", "const", "typegen"})

# `lifetime` block: exactly one of these primitive-kind subfields is populated
# (the subfield name IS the kind); the whole block is null for ordinary symbols.
# See docs/schemas/syms.md `## lifetime`.
_LIFETIME_KINDS = frozenset({"alloc", "clone", "refcount", "lock"})
_SYNTH_VALUES = frozenset({"string", "array"})
_REFCOUNT_OPS = frozenset({"new", "up", "down", "get", "free", "assert"})
_LOCK_OPS = frozenset({"new", "read_lock", "write_lock", "unlock", "free"})


def _lifetime_block_errors(blk) -> list[str]:
    """Validate a submitted `lifetime` block against docs/schemas/syms.md.

    `null` (ordinary symbol) is accepted. Otherwise EXACTLY ONE of
    `{alloc, clone, refcount, lock}` must be populated, and that subfield's
    shape is checked per its kind. Semantic rules (untyped-only, tag-the-callable)
    are the agent's judgement, not enforced here."""
    if blk is None:
        return []
    if not isinstance(blk, dict):
        return ["lifetime: must be an object or null"]
    kinds = set(blk)
    bad = kinds - _LIFETIME_KINDS
    if bad:
        return [f"lifetime: unknown subfield(s) {sorted(bad)} "
                f"(one of {sorted(_LIFETIME_KINDS)})"]
    if len(kinds) != 1:
        return ["lifetime: exactly ONE of "
                f"{sorted(_LIFETIME_KINDS)} must be populated (got {sorted(kinds)})"]
    (kind,) = kinds
    sub = blk[kind]
    e: list[str] = []
    if not isinstance(sub, dict):
        return [f"lifetime.{kind}: must be an object"]

    def _synth(v):
        if v not in _SYNTH_VALUES:
            e.append(f"lifetime.{kind}.synth: {v!r} not in {sorted(_SYNTH_VALUES)}")

    def _freed_by(v):
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            e.append(f"lifetime.{kind}.freed_by: must be a list of symbol names")

    if kind == "alloc":
        bad = set(sub) - {"role", "freed_by", "synth"}
        if bad:
            e.append(f"lifetime.alloc: unknown key(s) {sorted(bad)}")
        role = sub.get("role")
        if role not in ("allocator", "free"):
            e.append("lifetime.alloc.role: must be 'allocator' or 'free'")
        if role == "allocator":
            if "synth" not in sub:
                e.append("lifetime.alloc.synth: required for an allocator")
            else:
                _synth(sub["synth"])
            _freed_by(sub.get("freed_by", []))
        elif role == "free":
            extra = {"freed_by", "synth"} & set(sub)
            if extra:
                e.append(f"lifetime.alloc: {sorted(extra)} apply to an allocator, "
                         f"not a free")
    elif kind == "clone":
        bad = set(sub) - {"freed_by", "synth"}
        if bad:
            e.append(f"lifetime.clone: unknown key(s) {sorted(bad)}")
        if "synth" not in sub:
            e.append("lifetime.clone.synth: required")
        else:
            _synth(sub["synth"])
        _freed_by(sub.get("freed_by", []))
    else:  # refcount | lock
        bad = set(sub) - {"op", "cluster"}
        if bad:
            e.append(f"lifetime.{kind}: unknown key(s) {sorted(bad)}")
        ops = _REFCOUNT_OPS if kind == "refcount" else _LOCK_OPS
        if sub.get("op") not in ops:
            e.append(f"lifetime.{kind}.op: must be one of {sorted(ops)}")
        if not (isinstance(sub.get("cluster"), str) and sub.get("cluster")):
            e.append(f"lifetime.{kind}.cluster: required (the cluster type name)")
    return e


def _apply_ptr_agent(entry: dict, ptr_args_f: dict | None,
                     ptr_ret_f: dict | None) -> None:
    """Apply agent ptr findings (`ptr_args` keyed by position, `ptr_ret`) onto an
    entry's structural ptr blocks, in place. Only the agent-owned keys are
    copied; the composer's position/name/type/const/depth are left intact."""
    by_pos = {str(a.get("position")): a for a in entry.get("ptr_args") or []}
    for pos, blk in (ptr_args_f or {}).items():
        arg = by_pos.get(str(pos))
        if arg is not None:
            for k in _PTR_ARG_AGENT_KEYS:
                if k in blk:
                    arg[k] = blk[k]
    if ptr_ret_f is not None and entry.get("ptr_ret") is not None:
        for k in _PTR_RET_AGENT_KEYS:
            if k in ptr_ret_f:
                entry["ptr_ret"][k] = ptr_ret_f[k]


def _ptr_invariant_errors(field: str, ptr: dict, field_type: str) -> list[str]:
    """Hard-reject the IMPOSSIBLE / inconsistent shapes in one field's `ptr`
    block. Most old ownership *dependencies* are now STRUCTURAL (unrepresentable
    otherwise): `exclusive`/`shared` nest under `owned`, `lifetime` under
    `borrowed`, element-ownership under `array.by_ref` — so only shape validity,
    the string/array mutex, borrowed-requires-lifetime, and const/mutable remain.

    Dual ownership is allowed: `owned` and `borrowed` may BOTH be non-null
    (runtime-conditional), likewise `owned.exclusive`+`owned.shared` and
    `array.by_ref.owned`+`.borrowed`."""
    e: list[str] = []
    array = ptr.get("array")
    if array is not None:
        if not isinstance(array, dict):
            e.append(f"field {field!r}: array must be null or {{by_val|by_ref}}")
        else:
            kinds = [k for k in ("by_val", "by_ref") if array.get(k)]
            if len(kinds) != 1:
                e.append(f"field {field!r}: array needs exactly one of by_val / by_ref")
            elif "by_ref" in kinds and not isinstance(array["by_ref"], dict):
                e.append(f"field {field!r}: array.by_ref must be {{owned, borrowed}}")
    if ptr.get("string") and array is not None:
        e.append(f"field {field!r}: string and array both set (must be XOR)")
    borrowed = ptr.get("borrowed")
    if isinstance(borrowed, dict) and not borrowed.get("lifetime"):
        e.append(f"field {field!r}: borrowed set but lifetime unset")
    owned = ptr.get("owned")
    if owned is not None and not isinstance(owned, dict):
        e.append(f"field {field!r}: owned must be null or {{exclusive, shared}}")
    if "const" in (field_type or "") and ptr.get("mutable") is True:
        e.append(f"field {field!r}: const in type but mutable == true")
    return e


def _locked_update(path: Path, apply) -> None:
    """Serialize a read-modify-write of `path` against concurrent ``--update``
    processes, then install the result atomically.

    The exclusive lock is held on the manifest's PARENT DIRECTORY fd, NOT on the
    data file. The merge is committed by an atomic ``os.replace``, which swaps in
    a NEW inode — so a lock held on the data file's own fd would not serialize a
    process that opens the file fresh, and a writer that opened before the swap
    would read-modify-write the orphaned pre-update inode (the lost-update race
    this fixes). The directory inode never moves (an in-dir rename leaves it
    intact), so every writer contends on the one lock and leaves no on-disk
    artifact. The data file is (re-)read only AFTER the lock is acquired, so each
    writer sees the latest committed content. Lock granularity is per-dir; manifest
    kinds (types.json/syms.json) are written in separate analyze stages, so this
    serializes only same-file concurrent writers in practice. `apply(doc)` mutates
    the loaded doc in place, or raises ``SystemExit`` to reject (applying nothing)."""
    import fcntl
    import tempfile
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        doc = json.loads(path.read_text())
        apply(doc)                           # validate + merge, or raise SystemExit
        blob = json.dumps(doc, indent=1) + "\n"
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(path.parent), delete=False)
        try:
            tmp.write(blob)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)
    finally:
        fcntl.flock(dirfd, fcntl.LOCK_UN)
        os.close(dirfd)


def _update_type(layout, target, tag: str, defined_in: str | None,
                 src: str) -> None:
    """Ingest an agent *findings* doc and merge it into the type's manifest
    entry — the schema boundary, so the agent never opens types.json.

    `src` is a path, or ``"-"`` for stdin. The findings doc is the flat,
    name-keyed agent shape (lifecycle slots + `fields: {name: {ptr}}` + optional
    `_comment_agent`). We HARD-REJECT (and apply nothing) on structural
    contradictions, unknown field names, hallucinated functions, a lifecycle op
    that is a macro kind, and ptr-invariant violations; otherwise partial-merge
    (only the slots/fields mentioned) under a lock + atomic rename."""
    raw = sys.stdin.read() if src == "-" else Path(src).read_text()
    try:
        f = json.loads(raw)
    except ValueError as ex:
        raise SystemExit(f"--update: findings is not valid JSON: {ex}")
    if not isinstance(f, dict):
        raise SystemExit("--update: findings must be a JSON object.")

    bad_top = set(f) - _FINDINGS_TOP
    if bad_top:
        raise SystemExit(f"--update: unknown findings key(s): {sorted(bad_top)}")

    p = _manifest_path(layout.analysis, "type", tag, defined_in)
    if p is None or not Path(p).exists():
        raise SystemExit(f"--update: no manifest file homes type {tag!r}.")
    path = Path(p)

    # Real C symbol universe — the hallucination guard — split by kind. A
    # lifecycle op must name a FUNCTION, never a macro: a macro that expands to a
    # function should be recorded as that underlying function; a macro that does
    # not expand to one is omitted (§2.3). So a lifecycle op that is a macro kind
    # (and not also a real function) is hard-rejected below.
    import csv as _csv
    funcs: set[str] = set()
    macros: set[str] = set()
    for csv_name, bucket in (("functions.csv", funcs), ("macros.csv", macros)):
        p_csv = layout.t1 / csv_name
        if p_csv.exists():
            with p_csv.open() as fh:
                for r in _csv.DictReader(fh):
                    if r.get("name"):
                        bucket.add(r["name"])
    universe = funcs | macros

    def _id_match(e: dict) -> bool:
        # Identity is `defined_in or canonical_decl(declared_in)`: an
        # anonymous-typedef struct (e.g. a STACK_OF instance) has a null
        # `defined_in`, so a caller's file identifies it via `declared_in`.
        if (e.get("name") or e.get("type")) != tag:
            return False
        if defined_in is None or e.get("defined_in") == defined_in:
            return True
        return e.get("defined_in") is None and defined_in in (
            e.get("declared_in") or [])

    def _apply(doc: dict) -> None:
        entries = doc.get("types", [])
        entry = next((e for e in entries if _id_match(e)), None)
        if entry is None:
            raise SystemExit(f"--update: type {tag!r} not found in {path}.")

        field_by_name = {fld.get("name"): fld
                         for fld in entry.get("fields") or []}

        errors: list[str] = []

        def check_fn(fn: str, role: str) -> None:
            if universe and fn not in universe:
                errors.append(f"{role} {fn!r} is not a known function "
                              f"(hallucination?)")
            elif fn in macros and fn not in funcs:
                errors.append(f"{role} {fn!r} is a macro, not a function — "
                              f"record the underlying function it expands to, "
                              f"or omit it (§2.3)")

        # Lifecycle function-name checks.
        for fn in f.get("allocs") or []:
            check_fn(fn, "alloc")
        clone = f.get("clone") or {}
        if isinstance(clone, dict):
            if clone.get("shared"):
                check_fn(clone["shared"], "clone.shared")
            for fn in clone.get("exclusive") or []:
                check_fn(fn, "clone.exclusive")
        d = f.get("drop") or {}
        if isinstance(d, dict):
            # Current schema is {shared, exclusive, fields}. Old keys (e.g. the
            # pre-split `storage`) are HARD-REJECTED — findings must be current
            # schema, no legacy fields.
            bad_drop = set(d) - {"shared", "exclusive", "fields"}
            if bad_drop:
                errors.append(f"drop has old/unknown key(s) {sorted(bad_drop)} "
                              "(use shared / exclusive / fields)")
            # Each role is a LIST (a role may hold several distinct destructors
            # of the same kind); a scalar is tolerated. No single C function may
            # fill two different roles (roles stay disjoint).
            seen_drop: dict[str, str] = {}
            for role in ("shared", "exclusive", "fields"):
                v = d.get(role)
                if not v:
                    continue
                for name in (v if isinstance(v, list) else [v]):
                    if not name:
                        continue
                    if name in seen_drop and seen_drop[name] != role:
                        errors.append(f"drop.{seen_drop[name]} and drop.{role} "
                                      f"name the same function {name!r} (roles "
                                      "must be disjoint)")
                    else:
                        seen_drop[name] = role
                        check_fn(name, f"drop.{role}")

        # Per-field checks.
        for fname, fa in (f.get("fields") or {}).items():
            if fname not in field_by_name:
                errors.append(f"unknown field {fname!r} (not in {tag}'s layout)")
                continue
            bad = set(fa) - _FIELD_AGENT_KEYS
            if bad:
                errors.append(f"field {fname!r}: unknown key(s) {sorted(bad)}")
            if "ptr" in fa and isinstance(fa["ptr"], dict):
                errors += _ptr_invariant_errors(
                    fname, fa["ptr"], field_by_name[fname].get("type") or "")

        if errors:
            raise SystemExit(
                "--update REJECTED — fix and re-run:\n  - "
                + "\n  - ".join(errors))

        # Merge (partial, idempotent): only the slots/fields mentioned. Lifecycle
        # slots land under `lifetime` (scope.lifetime returns the mutable dict —
        # the nested block for migrated records, the flat record otherwise).
        from compose.scope import lifetime as _lifetime
        lc = _lifetime(entry)
        for k in _LIFECYCLE_KEYS:
            if k in f:
                lc[k] = f[k]
        if "_comment_agent" in f:
            entry["_comment_agent"] = f["_comment_agent"]
        for fname, fa in (f.get("fields") or {}).items():
            fld = field_by_name[fname]
            for k in _FIELD_AGENT_KEYS:
                if k in fa:
                    fld[k] = fa[k]

    _locked_update(path, _apply)
    print(f"updated {tag} in {path}")


def _sym_ptr_invariant_errors(label: str, blk: dict, const: bool,
                              is_ret: bool) -> list[str]:
    """Hard-reject the IMPOSSIBLE shapes in one symbol `ptr_args[*]` / `ptr_ret`
    block. These are the SAME structural invariants a struct field's `ptr`
    obeys (see check_types_consistency): `array` is null|{by_val}|{by_ref};
    string⊕array; borrowed⟹lifetime; owned is null|{exclusive,shared};
    const⟹mutable≠true.

    `owned`+`borrowed` is NOT rejected: an arg or return may be BOTH to mean
    runtime-conditional dual ownership (owned on one path, borrowed on another);
    likewise owned.exclusive+shared and array.by_ref.owned+borrowed. (`is_ret`
    is retained for call-site symmetry; the invariants are uniform across args
    and returns.)"""
    e: list[str] = []
    array = blk.get("array")
    if array is not None:
        if not isinstance(array, dict):
            e.append(f"{label}: array must be null or {{by_val|by_ref}}")
        elif len([k for k in ("by_val", "by_ref") if array.get(k)]) != 1:
            e.append(f"{label}: array needs exactly one of by_val / by_ref")
    if blk.get("string") and array is not None:
        e.append(f"{label}: string and array both set (must be XOR)")
    borrowed = blk.get("borrowed")
    if isinstance(borrowed, dict) and not borrowed.get("lifetime"):
        e.append(f"{label}: borrowed set but lifetime unset")
    owned = blk.get("owned")
    if owned is not None and not isinstance(owned, dict):
        e.append(f"{label}: owned must be null or {{exclusive, shared}}")
    if const and blk.get("mutable") is True:
        e.append(f"{label}: const pointee but mutable == true")
    return e


def _update_sym(layout, target, name: str, defined_in: str | None,
                src: str) -> None:
    """Ingest an agent *findings* doc for ONE symbol and merge it into syms.json
    — the schema boundary, so the agent never opens the manifest.

    `src` is a path, or ``"-"`` for stdin. Findings shape:
    ``{macro?, ptr_args?: {<position>: <ptr block>}, ptr_ret?: <ptr block>,
    forks?: [{ptr_args, ptr_ret, callsites}]}``, where a ptr block is the
    structured ownership record ``{array, string, owned, borrowed, nullable,
    mutable, note}`` shared with a struct field's `ptr`. `macro` is the
    expansion facets (macros only — `kind` itself is composer-fixed and never
    submitted). `forks` (callbacks only) splits a typedef whose invokers realize
    different ownership contracts into extra ``kind:"callback"`` entries
    (variant>=1), partitioning ``used_by.call`` — one Rust wrapper per entry. We
    HARD-REJECT on unknown keys, a bad macro block, an unknown arg position, a
    `ptr_ret` on a non-pointer-return, a fork on a non-callback / with an unknown
    or double-claimed callsite, or ptr-invariant violations; else partial-merge
    (primary) + idempotent fork replace, under a lock + atomic rename."""

    raw = sys.stdin.read() if src == "-" else Path(src).read_text()
    try:
        f = json.loads(raw)
    except ValueError as ex:
        raise SystemExit(f"--update: findings is not valid JSON: {ex}")
    if not isinstance(f, dict):
        raise SystemExit("--update: findings must be a JSON object.")

    bad_top = set(f) - _SYM_FINDINGS_TOP
    if bad_top:
        raise SystemExit(f"--update: unknown findings key(s): {sorted(bad_top)}")

    p = _manifest_path(layout.analysis, "symbol", name, defined_in)
    if p is None or not Path(p).exists():
        raise SystemExit(f"--update: no manifest file homes symbol {name!r}.")
    path = Path(p)

    holder = {"n": 0}

    def _apply(doc: dict) -> None:
        entries = doc.get("symbols", [])

        def _is_primary(e: dict) -> bool:
            # Identity is `defined_in or canonical_decl(declared_in)`: a
            # callback (and other header-only decls) has a null `defined_in`,
            # so a caller's file identifies it via `declared_in`.
            if e.get("name") != name or (e.get("variant") or 0) != 0:
                return False
            if defined_in is None or e.get("defined_in") == defined_in:
                return True
            return e.get("defined_in") is None and defined_in in (
                e.get("declared_in") or [])

        # The primary entry (variant 0) — re-submits with existing forks on
        # disk still target it.
        entry = next((e for e in entries if _is_primary(e)), None)
        if entry is None:
            raise SystemExit(
                f"--update: symbol {name!r} not found in {path}.")

        errors: list[str] = []
        ekind = entry.get("kind") or ""

        # `macro`: the expansion facets, agent-set on macros only. `kind` itself
        # is composer-fixed and terminal for every symbol (see docs/schemas/
        # syms.md `## kind`), so it is not a submittable key at all.
        if "macro" in f:
            blk = f["macro"]
            if ekind != "macro":
                errors.append(
                    f"macro: {name!r} is {ekind!r}, not a macro — the macro "
                    f"block applies to macros only")
            elif not isinstance(blk, dict):
                errors.append("macro: must be an object")
            else:
                bad = set(blk) - _MACRO_FLAGS
                if bad:
                    errors.append(f"macro: unknown key(s) {sorted(bad)}")
                missing = _MACRO_FLAGS - set(blk)
                if missing:
                    errors.append(
                        f"macro: missing flag(s) {sorted(missing)} — all of "
                        f"{sorted(_MACRO_FLAGS)} are required")
                for k in sorted(_MACRO_FLAGS & set(blk)):
                    if not isinstance(blk[k], bool):
                        errors.append(
                            f"macro.{k}: must be a boolean, got {blk[k]!r}")

        # `lifetime`: the lifecycle-primitive block (alloc / clone / refcount /
        # lock). Any symbol kind may carry it — a refcount `assert` guard is
        # legitimately a macro, so we do NOT gate it on kind.
        if "lifetime" in f:
            errors += _lifetime_block_errors(f["lifetime"])

        arg_by_pos = {str(a.get("position")): a
                      for a in entry.get("ptr_args") or []}
        pr = f.get("ptr_ret")

        # Validates one ownership contract — the primary's (`where=""`) or a
        # fork's. Both shapes are identical, so forks reuse it verbatim.
        def _check_ptr(args_f, ret_f, where):
            for pos, blk in (args_f or {}).items():
                if not isinstance(blk, dict):
                    errors.append(f"{where}ptr_args[{pos}]: must be an object")
                    continue
                if str(pos) not in arg_by_pos:
                    errors.append(
                        f"{where}ptr_args: no pointer arg at position {pos} "
                        f"in {name!r}")
                    continue
                bad = set(blk) - _PTR_ARG_AGENT_KEYS
                if bad:
                    errors.append(
                        f"{where}ptr_args[{pos}]: unknown key(s) {sorted(bad)}")
                errors.extend(_sym_ptr_invariant_errors(
                    f"{where}ptr_args[{pos}]", blk,
                    bool(arg_by_pos[str(pos)].get("const")), is_ret=False))
            if ret_f is not None:
                if not isinstance(ret_f, dict):
                    errors.append(f"{where}ptr_ret: must be an object")
                elif entry.get("ptr_ret") is None:
                    errors.append(f"{where}ptr_ret: {name!r} has no pointer return")
                else:
                    bad = set(ret_f) - _PTR_RET_AGENT_KEYS
                    if bad:
                        errors.append(f"{where}ptr_ret: unknown key(s) {sorted(bad)}")
                    errors.extend(_sym_ptr_invariant_errors(
                        f"{where}ptr_ret", ret_f,
                        bool(entry["ptr_ret"].get("const")), is_ret=True))

        _check_ptr(f.get("ptr_args"), pr, "")

        # Forks (callbacks only): split the typedef by ownership cluster.
        forks = f.get("forks")
        if forks is not None:
            if entry.get("kind") != "callback":
                errors.append(
                    f"forks: only a callback may fork ({name!r} is {ekind!r})")
            elif not isinstance(forks, list):
                errors.append("forks: must be a list")
            else:
                # Full invoker set = primary ∪ existing forks, so a re-submit
                # (where prior forks already drew callsites out of the
                # primary) still validates the same partition.
                all_calls = set((entry.get("used_by") or {}).get("call") or [])
                for e in entries:
                    if (e.get("name") == name
                            and (defined_in is None
                                 or e.get("defined_in") == defined_in)
                            and (e.get("variant") or 0) >= 1):
                        all_calls |= set(
                            (e.get("used_by") or {}).get("call") or [])
                claimed: set = set()
                for i, fk in enumerate(forks):
                    if not isinstance(fk, dict):
                        errors.append(f"forks[{i}]: must be an object")
                        continue
                    badf = set(fk) - _FORK_KEYS
                    if badf:
                        errors.append(f"forks[{i}]: unknown key(s) {sorted(badf)}")
                    sites = fk.get("callsites") or []
                    if not sites:
                        errors.append(
                            f"forks[{i}]: callsites empty (a fork must own "
                            f">=1 invoker)")
                    for s in sites:
                        if all_calls and s not in all_calls:
                            errors.append(
                                f"forks[{i}]: callsite {s!r} not in {name!r} "
                                f"used_by.call")
                        if s in claimed:
                            errors.append(
                                f"forks[{i}]: callsite {s!r} already claimed "
                                f"by another fork")
                        claimed.add(s)
                    _check_ptr(fk.get("ptr_args"), fk.get("ptr_ret"),
                               f"forks[{i}].")

        if errors:
            raise SystemExit(
                "--update REJECTED — fix and re-run:\n  - "
                + "\n  - ".join(errors))

        # Merge primary (partial, idempotent): only the slots/args mentioned.
        if "macro" in f:
            entry["macro"] = {k: f["macro"][k] for k in sorted(_MACRO_FLAGS)}
        if "lifetime" in f:
            entry["lifetime"] = f["lifetime"]
        _apply_ptr_agent(entry, f.get("ptr_args"), pr)

        # Materialize forks (idempotent replace): drop any prior forks, then
        # spawn one variant>=1 entry per cluster (inheriting the primary's
        # composer structure), and PARTITION used_by.call so each invoker
        # belongs to exactly one variant.
        if forks is not None:
            import copy as _copy
            # Recover the FULL invoker set (primary ∪ existing forks) before
            # dropping the old forks — so re-partitioning (incl. un-forking
            # via `forks: []`) restores every callsite to the right variant.
            full_call = list((entry.get("used_by") or {}).get("call") or [])
            seen_call = set(full_call)
            for e in entries:
                if (e.get("name") == name
                        and (defined_in is None
                             or e.get("defined_in") == defined_in)
                        and (e.get("variant") or 0) >= 1):
                    for s in (e.get("used_by") or {}).get("call") or []:
                        if s not in seen_call:
                            full_call.append(s)
                            seen_call.add(s)
            entries[:] = [e for e in entries
                          if not (e.get("name") == name
                                  and (defined_in is None
                                       or e.get("defined_in") == defined_in)
                                  and (e.get("variant") or 0) >= 1)]
            fork_sites = {s for fk in forks for s in (fk.get("callsites") or [])}
            entry.setdefault("used_by", {})["call"] = [
                s for s in full_call if s not in fork_sites]
            for i, fk in enumerate(forks, start=1):
                fe = _copy.deepcopy(entry)
                fe["variant"] = i
                fe["used_by"] = {"call": sorted(fk.get("callsites") or []),
                                 "ref": []}
                _apply_ptr_agent(fe, fk.get("ptr_args"), fk.get("ptr_ret"))
                entries.append(fe)
            doc["symbols"] = entries
            holder["n"] = len(forks)


    _locked_update(path, _apply)

    n = holder["n"]
    tail = f" (+{n} fork{'s' if n != 1 else ''})" if n else ""
    print(f"updated {name} in {path}{tail}")


def _create_type(layout, target, src: str) -> None:
    """Ingest a WHOLE synthetic cluster entry from the buffer pass and write it
    into the manifest — the schema boundary, so the agent never opens types.json.

    Unlike `--update` (partial-merge of an existing composer-emitted entry), the
    `string` / `array` clusters have NO composer skeleton — they are born here,
    so `--create` takes the complete entry and HOMES it: the manifest dir of its
    `defined_in` (the primary ctor's file). The entry is appended to that dir's
    `types.json` (created if absent), or replaced if a cluster of the same
    (type, defined_in) is already there (idempotent re-run). HARD-REJECTS on a
    non-synthetic kind, a missing/ill-typed identity, a hallucinated function, or
    a dtor.storage==fields collision."""
    from compose.path_partition import manifest_dir_for

    raw = sys.stdin.read() if src == "-" else Path(src).read_text()
    try:
        f = json.loads(raw)
    except ValueError as ex:
        raise SystemExit(f"--create: entry is not valid JSON: {ex}")
    if not isinstance(f, dict):
        raise SystemExit("--create: entry must be a JSON object.")

    bad_top = set(f) - _CREATE_TOP
    if bad_top:
        raise SystemExit(f"--create: unknown key(s): {sorted(bad_top)}")

    errors: list[str] = []
    tag = f.get("name") or f.get("type")  # `type` tolerated for legacy findings
    kind = f.get("kind")
    if not tag:
        errors.append("`name` (cluster identifier) is required")
    if kind not in _SYNTH_CREATE_KINDS:
        errors.append(f"`kind` must be one of {sorted(_SYNTH_CREATE_KINDS)} "
                      f"(got {kind!r}) — --create is for synthetic clusters only")
    if not isinstance(f.get("declared_in"), list):
        errors.append("`declared_in` must be a JSON list (even for one header)")
    defined_in = f.get("defined_in")
    mdir = manifest_dir_for(defined_in) or manifest_dir_for(
        next(iter(f.get("declared_in") or []), None))
    if mdir is None:
        errors.append("cannot place: no `defined_in` / `declared_in` to home it")

    # Hallucination guard — functions ∪ macros (a cluster op may be a macro).
    import csv as _csv
    universe: set[str] = set()
    for csv_name in ("functions.csv", "macros.csv"):
        p_csv = layout.t1 / csv_name
        if p_csv.exists():
            with p_csv.open() as fh:
                for r in _csv.DictReader(fh):
                    if r.get("name"):
                        universe.add(r["name"])
    named = list(f.get("allocs") or [])
    _clone = f.get("clone") or {}
    if isinstance(_clone, dict):
        if _clone.get("shared"):
            named.append(_clone["shared"])
        named += list(_clone.get("exclusive") or [])
    d = f.get("drop") or {}
    if isinstance(d, dict):
        # Current schema {shared, exclusive, fields}; old keys (e.g. the
        # pre-split `storage`) are rejected. No function may fill two roles.
        bad_drop = set(d) - {"shared", "exclusive", "fields"}
        if bad_drop:
            errors.append(f"drop has old/unknown key(s) {sorted(bad_drop)} "
                          "(use shared / exclusive / fields)")
        present = [v for r in ("shared", "exclusive", "fields")
                   if (v := d.get(r))]
        if len(present) != len(set(present)):
            errors.append("drop roles name the same function (must differ)")
        named += present

    # ARRAY-only element surface. `elems` rows are {type, note} — the concrete
    # element types the buffer holds at call sites, for the wrapper's typed
    # CVec<T> aliases (element OWNERSHIP/drop is a PORT concern: ptr.owned_elem).
    # A string is a single buffer, so it carries no `elems`. Length-aware
    # teardown is NOT stored: the wrapper reads it off the dtor's signature.
    elems = f.get("elems")
    if elems is not None:
        if kind != "array":
            errors.append("`elems` is array-only")
        elif not isinstance(elems, list):
            errors.append("`elems` must be a list")
        else:
            for i, row in enumerate(elems):
                if not isinstance(row, dict) or not row.get("type"):
                    errors.append(f"elems[{i}]: each row needs a `type`")
                elif set(row) - {"type", "note"}:
                    errors.append(
                        f"elems[{i}]: unknown key(s) "
                        f"{sorted(set(row) - {'type', 'note'})} (rows are {{type, note}})")

    for fn in named:
        if universe and fn not in universe:
            errors.append(f"{fn!r} is not a known function/macro (hallucination?)")

    if errors:
        raise SystemExit(
            "--create REJECTED — fix and re-run:\n  - " + "\n  - ".join(errors))

    entry = {
        "name": tag, "typedef": [], "kind": kind,
        "declared_in": f["declared_in"], "defined_in": defined_in,
        "lifetime": {
            # `allocs` = the byte-level allocators of this buffer family (metadata
            # pairing with `dtor` for the CVec strategy); the allocator functions
            # are still wrapped as free syms by the symbol wrapper.
            "allocs": f.get("allocs") or [],
            "clone": f.get("clone") or {"shared": None, "exclusive": []},
            "drop": f.get("drop") or {"shared": [], "exclusive": [], "fields": []},
            "locking": f.get("locking"),
            "conditional_drop": f.get("conditional_drop"),
        },
        "casted": {"to": [], "from": []}, "fields": [],
    }
    if kind == "array":   # element surface is array-only
        entry["elems"] = f.get("elems") or []
    if "_comment_agent" in f:
        entry["_comment_agent"] = f["_comment_agent"]

    import fcntl
    import tempfile
    path = layout.analysis / mdir / manifest_name("type")
    path.parent.mkdir(parents=True, exist_ok=True)
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        doc = json.loads(path.read_text()) if path.exists() else {
            "_comment": "agent-synthesized buffer-pass clusters", "types": []}
        entries = doc.setdefault("types", [])
        # idempotent: replace a prior cluster of the same identity, else append.
        entries[:] = [e for e in entries
                      if not ((e.get("name") or e.get("type")) == tag
                              and e.get("defined_in") == defined_in)]
        entries.append(entry)
        blob = json.dumps(doc, indent=1) + "\n"
        tmp = tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False)
        try:
            tmp.write(blob)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)
    finally:
        fcntl.flock(dirfd, fcntl.LOCK_UN)
        os.close(dirfd)

    print(f"created {kind} cluster {tag} in {path}")


def _records(target, kind, names, files, with_details) -> None:
    # record(s): summary by default, whole record with --with-details.
    load = _load_type_entry if kind == "type" else _load_sym_entry
    recs: list = []
    for nm in names:
        layout, node, _bk = _resolve(target, kind=kind, name=nm, files=files)
        entry = load(layout.analysis, node.id, node.defined_in)
        if entry is None:
            raise SystemExit(f"query {kind}: no manifest entry for {nm!r}.")
        recs.append(entry if with_details else _summarize(entry, kind))
    print(json.dumps(recs[0] if len(recs) == 1 else recs, indent=2))


def _parse_range(s: str | None) -> tuple[int, int | None] | None:
    """``"20:40"`` → ``(20, 40)``; open ends allowed (``"20:"`` / ``":40"``)."""
    if s is None:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        raise SystemExit(f"--range must be A:B (got {s!r}).")
    a = int(parts[0]) if parts[0] else 0
    b = int(parts[1]) if parts[1] else None
    return (a, b)


def _load_type_entry(analysis: Path, tag: str, defined_in: str | None) -> dict | None:
    """The raw ``types.json`` manifest entry for ``tag`` (preferring the one whose
    ``defined_in`` matches, to disambiguate a same-tag collision)."""
    fallback = None
    for f in analysis.rglob(manifest_name("type")):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("types", []):
            if (e.get("name") or e.get("type")) != tag:
                continue
            if defined_in and e.get("defined_in") == defined_in:
                return e
            fallback = fallback or e
    return fallback


def _manifest_path(analysis: Path, kind: str, tag: str,
                   defined_in: str | None) -> Path | None:
    """The manifest file that homes ``tag`` (the file an annotating agent writes
    back to) — ``types.json`` for a type, ``syms.json`` for a symbol — preferring
    the one whose entry matches ``defined_in``."""
    arr, tagkey = (("types", "name") if kind == "type"
                   else ("symbols", "name"))
    manifest = manifest_name(kind)
    fallback = None
    for f in analysis.rglob(manifest):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get(arr, []):
            if e.get(tagkey) != tag:
                continue
            if defined_in and e.get("defined_in") == defined_in:
                return f
            fallback = fallback or f
    return fallback


# ----------------------------------------------------------------- resolution
# `query type`/`query sym` read ONE entity (its record / fields / ops) — an
# intra-entity lookup, so the **manifest is authoritative**: it's the
# composer-emitted source of truth, fresh at analyze time. The deps-dag.json is
# a POST-analyze graph overlay — absent on a first pass, and STALE on a re-run
# (it predates the manifest edits the agent is making right now), so `_resolve`
# never reads it. (`--ops` ordering is intra-type — lifecycle-first over the
# type's own ops, built here from syms.json — so it works without the dag; only
# `query dag` reads the post-analyze artifact, and it loads it itself.)

def _resolve(target, *, kind: str, name: str, files: list[str] | None):
    """``(layout, node, by_key)`` for one type/symbol, resolved from the
    composer-emitted manifest tree (never the dag — see the note above). For a
    *type*, ``by_key`` is populated with the type's op nodes (resolved from
    ``syms.json``) so :func:`_schedule.ordered_ops` can serve ``--ops``.
    Raises ``SystemExit`` on miss/ambiguity."""
    from crustify import _schedule as S
    from crustify.layout import Layout

    verb = "type" if kind == "type" else "sym"
    noun = "type" if kind == "type" else "symbol"

    def _pick(nodes: list):
        if not nodes:
            raise SystemExit(f"query {verb}: no {noun} {name!r}"
                             f"{' in --file' if files else ''}.")
        if len(nodes) > 1:
            locs = ", ".join(n.defined_in or "?" for n in nodes)
            raise SystemExit(
                f"query {verb}: {name!r} is ambiguous ({locs}) — "
                f"pass --file to pick one.")
        return nodes[0]

    layout = Layout.discover(target)
    file_set = set(files or [])

    # walk the manifest tree (existence + defined_in are composer-filled).
    arr, tagkey = (("types", "name") if kind == "type"
                   else ("symbols", "name"))
    manifest = manifest_name(kind)
    uniq: dict = {}                                   # defined_in -> entry (dedup)
    for f in layout.analysis.rglob(manifest):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get(arr, []):
            if e.get(tagkey) != name:
                continue
            if file_set and e.get("defined_in") not in file_set:
                continue
            # Dedup by defining file; for a forked callback (several same-file
            # entries) prefer the primary (variant 0).
            df = e.get("defined_in")
            if df not in uniq or (e.get("variant") or 0) < (uniq[df].get("variant") or 0):
                uniq[df] = e

    def _mk(e: dict):
        return S.Node(id=name, node_kind=kind, subkind=str(e.get("kind") or "symbol"),
                      defined_in=e.get("defined_in"),
                      layer=0, ops=[], dep_types=[], dep_syms=[])

    node = _pick([_mk(e) for e in uniq.values()])
    by_key = {node.key: node}
    if kind == "type":
        from compose import scope
        # Derived method surface (lifecycle ∪ field accessors), or the synthetic
        # cluster's explicit ops — not a stored concrete-type `ops` list.
        op_names = scope.type_method_syms(uniq.get(node.defined_in) or {})
        if op_names:
            sidx = _syms_index(layout.analysis)
            keys = []
            for nm in op_names:
                se = sidx.get(nm) or {}
                onode = S.Node(id=nm, node_kind="symbol",
                               subkind=str(se.get("kind") or "symbol"),
                               defined_in=se.get("defined_in"), layer=0,
                               ops=[], dep_types=[], dep_syms=[])
                by_key[onode.key] = onode
                keys.append(onode.key)
            node.ops = keys
    return layout, node, by_key


def _syms_index(analysis: Path) -> dict:
    """``name -> first syms.json entry`` for pre-dag op resolution."""
    idx: dict = {}
    for f in analysis.rglob(manifest_name("symbols")):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("symbols", []):
            idx.setdefault(e.get("name"), e)
    return idx


def _load_sym_entry(analysis: Path, name: str, defined_in: str | None) -> dict | None:
    """The raw ``syms.json`` manifest entry for ``name`` (preferring the one whose
    ``defined_in`` matches, to disambiguate same-named file-local statics)."""
    fallback = None
    for f in analysis.rglob(manifest_name("symbols")):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("symbols", []):
            if e.get("name") != name:
                continue
            if defined_in and e.get("defined_in") == defined_in:
                return e
            fallback = fallback or e
    return fallback


def _dag_loc(by_key, by_name, names, files, layer, as_json, keep=None) -> None:
    """``query dag --loc`` — translated-LoC accounting over the dag.

    A **type**'s LoC is ``node.loc`` (its struct field count, Rule 1) **plus**
    its op count (each lifecycle/method op rides the type at 1 line — its real
    body is folded in, not ported standalone). A **function**'s LoC is its body
    span (``node.loc``).

      * ``--name T`` (type)     → fields + ops.
      * ``--name S`` (function) → body LoC.
      * ``--layer N``           → Σ over the layer: types as fields+ops, plus
        standalone (non-folded) function bodies. The bodies of functions that
        are some type's op are excluded — they're counted once, as +1 in their
        owning type (which may sit on another layer), never as a body here.
    """
    # Identity of every folded type-op, gathered globally (an op can sit a layer
    # below its type). Ops with a resolved file match by (name, file); ambiguous
    # ones (file None) match by name, mirroring the scheduler's fallback.
    op_keys: set = set()
    op_names: set = set()
    for n in by_key.values():
        if n.node_kind == "type":
            for nm, df in n.ops:
                op_keys.add((nm, df)) if df else op_names.add(nm)

    def is_folded_op(n) -> bool:
        return (n.id, n.defined_in) in op_keys or n.id in op_names

    def nops(n) -> int:
        # distinct ops ∪ ctors (ctors are a subset of ops for most types, but
        # the synthetic allocator-array clusters carry an allocator ctor not in
        # ops — so union both to avoid undercounting).
        return len({nm for nm, _ in n.ops} | set(n.ctors))

    def val(n) -> int:
        # type: field count (node.loc) + 1 per op; function: its body LoC.
        return n.loc + nops(n) if n.node_kind == "type" else n.loc

    if layer is not None:
        if names:
            raise SystemExit("query dag --loc: --layer and --name are mutually exclusive.")
        rows = [n for n in by_key.values() if n.layer == layer
                and (n.node_kind == "type" or not is_folded_op(n))
                and (keep is None or keep(n))]
    elif names:
        file_set = set(files or [])
        rows, unknown = [], []
        for nm in names:
            hits = [by_key[k] for k in by_name.get(nm, [])
                    if not file_set or (by_key[k].defined_in or "") in file_set]
            rows.extend(hits) if hits else unknown.append(nm)
        if unknown:
            extra = " matching --file" if file_set else ""
            raise SystemExit(f"query dag --loc: no node{extra} for: {', '.join(unknown)}")
    else:
        raise SystemExit("query dag --loc: pass --name T/S or --layer N.")

    rows.sort(key=lambda n: (n.layer, n.id))
    total = sum(val(n) for n in rows)
    if as_json:
        recs = []
        for n in rows:
            r = {"id": n.id, "kind": n.node_kind, "layer": n.layer, "loc": val(n)}
            if n.node_kind == "type":
                r["nfields"], r["nops"] = n.loc, nops(n)
            recs.append(r)
        print(json.dumps({"rows": recs, "total": total}, indent=2))
    else:
        for n in rows:
            print(f"{val(n)}\t{n.id}")
        print(f"{total}\tTOTAL")


def _scope_predicate(layout, target, wrap_only: bool, port_only: bool):
    """A node-keeping predicate for `--wrap-only` / `--port-only`, or None when
    neither is set. The dag is scope-agnostic; scope is read from scope.json on
    demand. `origin_key(id, defined_in)` is exactly the node's serialized origin
    (`Node.origin()`), so dag nodes and scope entries collide on the same key.
    Synthetic string/array clusters are never in scope.json — they are *always*
    wrap-scope, never port (mirrors `query types --wrap-only`)."""
    if not (wrap_only or port_only):
        return None
    from compose import scope as _sc
    sj = layout.scope(target)
    keys = _sc.scope_membership(sj, "port" if port_only else "wrap") if sj.exists() else set()

    def keep(n) -> bool:
        if (getattr(n, "subkind", "") or "") in _sc.SYNTHETIC_KINDS:
            return bool(wrap_only)
        return _sc.origin_key(n.id, n.defined_in, None) in keys
    return keep


def query_dag(
    target: Path,
    *,
    names: list[str] | None = None,
    files: list[str] | None = None,
    depth: int | None = None,
    scc: str | None = None,
    layer: int | None = None,
    as_json: bool = False,
    loc: bool = False,
    wrap_only: bool = False,
    port_only: bool = False,
) -> None:
    """Structural views over the dag. Three mutually-exclusive modes:

      * **closure** (``--name X``): X's transitive **dependencies** — what the
        dag emits *before* it. BFS over ``deps.types`` + ``deps.syms`` (forward
        edges; ``fallback`` back-edges are excluded — emitted after, raw).
        ``--depth N`` limits to N hops (1 = direct, 2 = deps of deps, …).
      * **layer slice** (``--layer N``): every node (type + symbol) at layer N.
      * **scc twins** (``--name X --scc hi-deps|lo-deps``): X's flattened-cycle
        twins. ``hi-deps`` = X's ``fallback`` (higher-layer twins X may use
        **naked**); ``lo-deps`` = X's ``back_fill`` (lower-layer twins that
        **used X naked**).

    ``--file`` disambiguates a ``--name`` collision."""
    from collections import deque

    from crustify import _schedule as S
    from crustify.layout import Layout

    layout = Layout.discover(target)
    dag_path = layout.analysis / "deps-dag.json"
    if not dag_path.exists():
        raise SystemExit(
            f"query dag: no deps-dag.json at {layout.analysis}. "
            f"Run `crustify {target} analyze dag` first.")
    dag = json.loads(dag_path.read_text())
    by_key, by_name = S.load_nodes(dag)
    keep = _scope_predicate(layout, target, wrap_only, port_only)

    # ── mode: LoC view ─────────────────────────────────────────────────
    if loc:
        _dag_loc(by_key, by_name, names, files, layer, as_json, keep)
        return

    def _emit(rows: list) -> None:
        """rows: list[(node, depth|None)] — print bare ids, or --json records."""
        rows.sort(key=lambda r: (r[0].layer, r[0].id) if r[1] is None
                  else (r[1], r[0].layer, r[0].id))
        if as_json:
            recs = []
            for n, d in rows:
                rec = {"id": n.id, "kind": n.node_kind, "subkind": n.subkind,
                       "layer": n.layer, "defined_in": n.defined_in}
                if d is not None:
                    rec["depth"] = d
                recs.append(rec)
            print(json.dumps(recs, indent=2))
            return
        seen: set[str] = set()
        for n, _d in rows:
            if n.id in seen:
                continue
            seen.add(n.id)
            print(n.id)

    # ── mode: layer slice ──────────────────────────────────────────────
    if layer is not None:
        if names:
            raise SystemExit("query dag: --layer and --name are mutually exclusive.")
        _emit([(n, None) for n in by_key.values()
               if n.layer == layer and (keep is None or keep(n))])
        return

    if not names:
        raise SystemExit("query dag: pass --name T/S (closure or --scc) "
                         "or --layer N (slice).")

    # Resolve each --name to its node key(s), --file disambiguating collisions.
    file_set = set(files or [])
    start: list = []
    unknown: list[str] = []
    for nm in names:
        hit = [k for k in by_name.get(nm, [])
               if not file_set or (by_key[k].defined_in or "") in file_set]
        (start.extend(hit) if hit else unknown.append(nm))
    if unknown:
        extra = " matching --file" if file_set else ""
        raise SystemExit(f"query dag: no node{extra} for: {', '.join(unknown)}")

    # ── mode: scc twins (fallback / back_fill) ─────────────────────────
    if scc:
        attr = "fallback" if scc == "hi-deps" else "back_fill"
        tags: list[str] = []
        for k in start:
            tags.extend(getattr(by_key[k], attr))
        rows = []
        for t in dict.fromkeys(tags):               # dedup, preserve order
            for dk in by_name.get(t, []):
                if by_key[dk].node_kind == "type" and (keep is None or keep(by_key[dk])):
                    rows.append((by_key[dk], None))
        _emit(rows)
        return

    # ── mode: transitive dependency closure (BFS) ──────────────────────
    def _dep_keys(n) -> list:
        out = []
        for t in n.dep_types:                       # type tags → their type node
            out.extend(k for k in by_name.get(t, [])
                       if by_key[k].node_kind == "type")
        out.extend(sk for sk in n.dep_syms if sk in by_key)  # in-tree sym deps
        return out

    start_keys = set(start)
    hop: dict = {k: 0 for k in start}               # BFS: first visit = min hop
    q = deque((k, 0) for k in start)
    while q:
        k, h = q.popleft()
        if depth is not None and h >= depth:
            continue
        for dk in _dep_keys(by_key[k]):
            if dk not in hop:
                hop[dk] = h + 1
                q.append((dk, h + 1))

    _emit([(by_key[k], hop[k]) for k in hop
           if k not in start_keys and (keep is None or keep(by_key[k]))])


# ---------------------------------------------------------------------------
# query files — the scope file sets
# ---------------------------------------------------------------------------

def query_files(
    target: Path,
    *,
    port_only: bool = False,
    wrap_only: bool = False,
) -> None:
    """Read-only oracle over the target's scope **files** (one path per line,
    sorted).

      - ``--port-only`` — the port-scope file set (``scope.json.port``).
      - ``--wrap-only`` — the wrap closure: the import-header surface the port
        TUs reach through their ``depends_on`` edges. Read from the cached
        ``scope.json.wrap`` section (the single source of truth, written by
        ``analyze scope --wrap-only``); errors if that section is absent —
        consumers never recompute the closure themselves.
      - neither flag — both, printed as two labeled lists (``# port`` then
        ``# wrap``). The single-flag forms print a bare list (xargs-friendly).
    """
    from compose import scope as scope_mod
    from crustify.layout import Layout

    layout = Layout.discover(target)
    scope_path = layout.scope(target)
    if not scope_path.exists():
        raise SystemExit(
            f"error: scope.json not found at {scope_path}. Run "
            f"`crustify {target} analyze scope --port-only` first.")
    doc = json.loads(scope_path.read_text())

    port_files = wrap_files = None
    if port_only or not wrap_only:
        port_files = sorted(scope_mod.load_port_paths(scope_path))
    if wrap_only or not port_only:
        wrap = doc.get("wrap")
        if wrap is None:
            raise SystemExit(
                f"error: scope.json has no `wrap` section at {scope_path}. Run "
                f"`crustify {target} analyze scope --wrap-only` first "
                f"(composer-only; needs just the `port` section + `build execute`).")
        wrap_files = sorted(set(wrap.get("files") or []))

    if port_only:
        for f in port_files:
            print(f)
    elif wrap_only:
        for f in wrap_files:
            print(f)
    else:
        print(f"# port ({len(port_files)})")
        for f in port_files:
            print(f)
        print(f"\n# wrap ({len(wrap_files)})")
        for f in wrap_files:
            print(f)


def query_mem(
    target: Path,
    *,
    names: list[str] | None = None,
) -> None:
    """Read-only oracle over the allocator clusters in ``alloc.json`` — every
    allocator family returned **verbatim**: the family name, its ``free``, and
    the full record of each allocator (``name`` + the ``zeroing`` / ``sized`` /
    ``aligned`` / ``string`` / ``bounded`` flags + ``defined_in`` /
    ``declared_in`` / ``type``). The wrap agent uses this to emit the ``CBox``
    exclusive-freed strategy ZST (one per cluster, keyed on the free) plus the
    per-allocator constructor wrappers.

      - no filter — every family.
      - ``--name S …`` — only families whose ``free`` or one of its allocators is
        one of ``S`` (the wrapper's common path: "hand me my target symbol's
        cluster").

    No string/byte gate — the per-allocator ``string`` flag rides along so the
    consumer decides what is a nul-terminated string vs a raw byte buffer. Prints
    the JSON array of families to stdout.
    """
    from crustify.layout import Layout

    layout = Layout.discover(target)
    alloc_path = layout.alloc_json
    if not alloc_path.exists():
        raise SystemExit(
            f"error: alloc.json not found at {alloc_path}. Run "
            f"`crustify {target} alloc` first.")
    families = json.loads(alloc_path.read_text()).get("families", [])

    want = set(names or [])
    if want:
        def _members(fam: dict) -> set:
            free = fam.get("free")
            base = {free["name"]} if free else set()
            return base | {a["name"] for a in fam.get("allocators", [])}
        families = [f for f in families if _members(f) & want]

    print(json.dumps(families, indent=2))
