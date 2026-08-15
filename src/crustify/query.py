"""crustify-cli query — read-only oracle over the analysis tree.

Lists types / symbols filtered by scope, synthetic kind, dag layer,
name, and source file. It is the policy/inspection surface: the action commands
(``wrap`` / ``port``) are scope-mechanisms, and you pipe ``query`` output into
their ``--name``. Pure read — no side effects.

Output modes:
  * plain (default): one bare ``id`` per line, deduped, sorted by ``(layer, id)``
    — xargs-ready (``crustify <t> query types --wrap-only | xargs crustify <t> translate --name``).
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


# The composer package lives at ``utils/codeql/compose/`` in the crustify
# checkout, not as an installed package. Put its parent on sys.path so
# ``from compose import scope`` works from this orchestrator (mirror wrap.py).
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))

def query(
    target: Path,
    *,
    subject: str,                       # "types" | "symbols"
    names: list[str] | None = None,
    files: list[str] | None = None,
    wrap_only: bool = False,
    port_only: bool = False,
    fields: bool = False,
    ops: bool = False,
    methods: bool = False,
    field_touchers: bool = False,
    update: str | None = None,
    update_help: bool = False,
    schema: bool = False,
    create: str | None = None,
    rs: bool = False,
    manifest: bool = False,
    lifetime_for: str | None = None,
    taking: str | None = None,
    calling: str | None = None,
    hops: int = 1,
    array: bool = False,
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
    # --lifetime-for: REVERSE lifecycle lookup, parameterized by a TYPE (no
    # --name). Symbols-only; scans each symbol's entry-level `lifetime` block,
    # resolved to the arg it names in `for`.
    if lifetime_for is not None:
        if subject != "symbols":
            raise SystemExit("query: --lifetime-for applies to symbols only.")
        _lifetime_for(target, lifetime_for, array_only=array)
        return
    if taking is not None:
        if subject != "symbols":
            raise SystemExit("query: --taking applies to symbols only.")
        _taking(target, taking, calling, hops, array)
        return
    name_list = list(names or [])
    type_facets = fields or ops or methods or field_touchers
    if type_facets and kind != "type":
        raise SystemExit("query symbols: --fields/--ops/--methods/--field-touchers apply to types only.")
    if (type_facets or manifest or update is not None) and len(name_list) != 1:
        raise SystemExit(
            f"query {subject}: facets / --manifest / --update need exactly one --name.")
    if name_list:
        _introspect(target, kind=kind, names=name_list, files=files,
                    fields=fields, ops=ops, methods=methods, field_touchers=field_touchers,
                    update=update, manifest=manifest,
                    wrap_only=wrap_only, port_only=port_only)
    else:
        _enumerate(target, kind=kind, files=files,
                   wrap_only=wrap_only, port_only=port_only)


# A type SPEC is a struct tag / typedef, or one of two keywords naming an
# UNTYPED lifecycle tier (no types.json entry of its own):
#   `void`   -- raw, byte-level objects (the untyped tier; CRYPTO_free/memdup).
#   `string` -- NUL-terminated strings (CRYPTO_strdup); matched by the char
#               family OR the wrapper's own `ptr.string` verdict.
_SPEC_KEYWORDS = ("void", "string")
_CHAR_TOKENS = {"char", "uint8_t", "int8_t", "u8"}


def _arg_matches_spec(a: dict, spec: str, aliases: set, *,
                      structural_string: bool = True) -> bool:
    """Does one `ptr_args` record's type match the spec (tag / `void` / `string`)?

    `string` has two matchers, and which one is right depends on the caller:
      - the wrapper's own `ptr.string` VERDICT -- exact, but only on an
        analyzed record. This is all `--lifetime-for` (a post-analysis read)
        should trust.
      - the char FAMILY (`structural_string`) -- a structural guess for
        `--taking`, which discovers candidates that are not analyzed yet. It is
        deliberately loose: a `unsigned char *` byte buffer (e.g. a PMS) is NOT
        a NUL-terminated string, so this over-matches by design and the agent
        (or `ptr.string`) settles it.
    """
    import re as _re
    toks = set(_re.findall(r"[A-Za-z_]\w*", a.get("type") or ""))
    if spec == "void":
        return "void" in toks
    if spec == "string":
        if (a.get("ptr") or {}).get("string"):
            return True
        return structural_string and bool(toks & _CHAR_TOKENS)
    return bool(aliases & toks)


def _arg_is_array(a: dict) -> bool:
    """`--array`: the arg's ptr carries an `array` shape (buffer, not a lone
    pointee). Only meaningful on an analyzed record."""
    return (a.get("ptr") or {}).get("array") is not None


# ---------------------------------------------------------- composed records

def _entries(layout, target, kind: str, *, scoped: bool = True) -> list:
    """Every record of ``kind`` (``"type"``/``"types"`` | ``"sym"``/``"symbols"``),
    composed from the CodeQL tables and overlaid with `ownership-store.json`.

    Replaces the rglob over the per-stem tree every reader here used to do. The
    records are identical in shape; only their provenance changed.

    ``scoped=False`` widens to the whole CodeQL universe — see
    :func:`_universe_entry`."""
    from crustify import manifests as _m
    k = "types" if kind in ("type", "types") else "symbols"
    return _m.entries(layout, target, k, stage="query", scoped=scoped)


def _universe_entries(layout, target, kind: str) -> list:
    """Every record of ``kind`` the CodeQL extraction saw, scope seed dropped.

    The composed records are seeded from `scope.json`, so an entity the target
    does not own is absent from them. That is right for *enumeration* — a
    listing is the target's inventory — but wrong for a named lookup: ownership
    does not stop at the scope line. `stack_st_SSL_COMP` is an `ssl` type whose
    only destructor, `ossl_free_compression_methods_int`, lives in
    `crypto/comp_methods.c`, and an agent that cannot read that symbol cannot
    record what it does.

    So a named read and `--update` fall back here. Structure still comes from
    the composer, so validation is identical either way; only the seed differs.
    Nothing is filtered out: the system headers the extraction saw are records
    like any other, and one memoized compose serves the whole universe.
    """
    return _entries(layout, target, kind, scoped=False)


def _universe_entry(layout, target, kind: str, match) -> dict | None:
    """The first record :func:`_universe_entries` yields satisfying ``match``."""
    return next((e for e in _universe_entries(layout, target, kind)
                 if match(e)), None)


def _entry_pair(layout, target) -> tuple[list, list]:
    return (_entries(layout, target, "types"),
            _entries(layout, target, "symbols"))


def _callee_index(layout, target) -> dict:
    """name -> set(callee names), from the composer's `depends_on.syms`. Since
    scope gates emission and not content, this is codebase-wide for every
    emitted record."""
    idx: dict = {}
    for s in _entries(layout, target, "symbols"):
        cs = {d.get("name") for d in (s.get("depends_on") or {}).get("syms") or []}
        if cs:
            idx.setdefault(s.get("name"), set()).update(cs)
    return idx


def _reaches(start: str, targets: set, idx: dict, hops: int) -> set:
    """Which of `targets` are reachable from `start` within `hops` call hops.
    Hops are NOT capped by the tool -- a caller asking for depth is trusted."""
    if hops < 1:
        return set()
    hit: set = set()
    seen = {start}
    frontier = {start}
    for _ in range(hops):
        nxt: set = set()
        for fn in frontier:
            for c in idx.get(fn, ()):  # noqa: SIM118
                hit |= {c} & targets
                if c not in seen:
                    seen.add(c)
                    nxt.add(c)
        if not nxt:
            break
        frontier = nxt
    return hit


def _type_aliases(layout, target, type_name: str) -> set:
    """All spellings of a type (struct tag + typedef(s)) so an arg's ``type``
    string can be matched whichever alias the composer recorded."""
    for t in _entries(layout, target, "types"):
        td = t.get("typedef")
        tds = td if isinstance(td, list) else ([td] if td else [])
        aliases = {t.get("name"), *tds} - {None}
        if type_name in aliases:
            return aliases
    return {type_name}


_LIFETIME_FIELD = {"is_dropper": "dropped_by",
                   "is_disposer": "fields_disposed_by",
                   "is_cloner": "cloned_by"}


def _taking(target: Path, spec: str, calling: str | None,
            hops: int, array_only: bool) -> None:
    """CANDIDATE discovery for lifetime-primitive identification (the inverse of
    `--lifetime-for`, which reads flags that already exist).

    Every symbol with an ARG matching `spec` -- a type tag/typedef, or the
    `void` / `string` keyword. `--calling` narrows to those that reach one of
    the named routines within `--hops` call hops (a dropper/cloner must
    ultimately reach a raw primitive; the top-level one often does so via a
    helper, so hops > 1 is the norm). `--array` keeps only args whose ptr
    carries an `array` shape. Prints JSON."""
    from crustify.layout import Layout

    layout = Layout.discover(target)
    aliases = (_type_aliases(layout, target, spec) if spec not in _SPEC_KEYWORDS
               else {spec})
    want = {c.strip() for c in (calling or "").split(",") if c.strip()}
    idx = _callee_index(layout, target) if want else {}
    rows = []
    if True:
        for s in _entries(layout, target, "symbols"):
            hits = [a for a in s.get("ptr_args") or []
                    if _arg_matches_spec(a, spec, aliases)
                    and (not array_only or _arg_is_array(a))]
            if not hits:
                continue
            reached = sorted(_reaches(s.get("name"), want, idx, hops)) if want else []
            if want and not reached:
                continue
            rows.append({
                "symbol": s.get("name"),
                "defined_in": s.get("defined_in"),
                "args": [{"position": a.get("position"), "name": a.get("name"),
                          "type": a.get("type"), "depth": a.get("depth")}
                         for a in hits],
                **({"reaches": reached} if want else {}),
            })
    rows.sort(key=lambda r: (r["symbol"], r["defined_in"] or ""))
    print(json.dumps({
        "taking": spec,
        "matched_aliases": sorted(aliases),
        "calling": sorted(want) or None,
        "hops": hops if want else None,
        "array_only": array_only,
        "count": len(rows),
        "candidates": rows,
    }, indent=2))


def _lifetime_pool(layout, target) -> list:
    """The symbol records `--lifetime-for` scans.

    A type's destructor need not live in the type's scope —
    `ossl_free_compression_methods_int` (`crypto/comp_methods.c`) is the only
    dropper of the `ssl` type `stack_st_SSL_COMP`. Submitting that role is
    allowed (see :func:`_universe_entries`), so reading it back must be too, or
    the record is written where nothing can see it.

    The scoped records answer for every in-scope role, and only a *submitted*
    role can be out of scope — the composer never invents `lifetime`. So the
    store is the cheap oracle: widen to the universe only when it actually
    holds a lifetime block for a symbol the scope does not carry.
    """
    scoped = _entries(layout, target, "symbols")
    from crustify import store as _store

    doc = _store.load(layout)
    in_scope = {e.get("name") for e in scoped}
    outside = any(r.get("lifetime") and r.get("name") not in in_scope
                  for r in (doc.get("symbols") or []))
    return _universe_entries(layout, target, "symbols") if outside else scoped


def _lifetime_for(target: Path, type_name: str, array_only: bool = False) -> None:
    """Reverse lifecycle lookup for a type: every symbol whose entry-level
    `lifetime` acts on an ARG of that type, grouped into the type's dropped_by /
    fields_disposed_by / cloned_by candidates (from is_dropper / is_disposer /
    is_cloner). These are the Drop / dispose / Clone routines the type wrapper
    records.

    The role is SYMBOL-level and names its subject arg in `lifetime.for`, so the
    scan is one block per symbol resolved to that one arg — a symbol whose
    `lifetime.for` names an arg of a DIFFERENT type is not a candidate for this
    type, even though it may take one. Returns are never subjects (a return is
    produced, not acted on). Prints JSON ``{type, matched_aliases,
    dropped_by:[{symbol,arg,arg_name,arg_type,defined_in,mode?}],
    fields_disposed_by:[...], cloned_by:[...]}``."""
    from crustify.layout import Layout

    layout = Layout.discover(target)
    aliases = (_type_aliases(layout, target, type_name)
               if type_name not in _SPEC_KEYWORDS else {type_name})
    result = {"type": type_name, "matched_aliases": sorted(aliases),
              "array_only": array_only,
              "dropped_by": [], "fields_disposed_by": [], "cloned_by": []}
    seen = set()
    if True:
        for s in _lifetime_pool(layout, target):
            lf = s.get("lifetime")
            if not isinstance(lf, dict):
                continue
            subject = lf.get("for")
            a = next((x for x in s.get("ptr_args") or []
                      if x.get("name") == subject), None)
            if a is None:
                continue
            if not _arg_matches_spec(a, type_name, aliases,
                                     structural_string=False):
                continue
            if array_only and not _arg_is_array(a):
                continue
            for flag, field in _LIFETIME_FIELD.items():
                v = lf.get(flag)
                # `is_cloner` carries its {deep, upref} modes; is_dropper /
                # is_disposer are bare bools.
                if flag == "is_cloner":
                    if not (isinstance(v, dict)
                            and (v.get("deep") or v.get("upref"))):
                        continue
                    modes = [m for m in ("deep", "upref") if v.get(m)]
                else:
                    if v is not True:
                        continue
                    modes = None
                key = (field, s.get("name"), s.get("defined_in"),
                       a.get("position"))
                if key in seen:
                    continue
                seen.add(key)
                row = {
                    "symbol": s.get("name"),
                    "arg": a.get("position"),
                    "arg_name": a.get("name"),
                    "arg_type": a.get("type"),
                    "defined_in": s.get("defined_in"),
                }
                if modes is not None:
                    row["mode"] = modes
                result[field].append(row)
    for field in ("dropped_by", "fields_disposed_by", "cloned_by"):
        result[field].sort(key=lambda r: (r["symbol"], r["arg"]))
    print(json.dumps(result, indent=2))


def _enumerate(
    target: Path, *, kind: str, files, wrap_only, port_only,
) -> None:
    """List the (filtered) type/symbol entries straight from the manifest — one
    ``name<TAB>defined_in<TAB>declared_in`` line each (the placement provenance),
    or whole records with ``--with-details``."""
    from compose import scope
    from crustify.layout import Layout

    from crustify import scope as _scope_mod
    layout = Layout.discover(target)
    file_set = set(files or [])
    arr, tagkey = (("types", "name") if kind == "type"
                   else ("symbols", "name"))

    # Scope membership is read straight from scope.json — the authoritative,
    # deduped port/wrap closures — NOT a re-derived "not-port ⇒ wrap"
    # classification over the whole analysis tree. Keyed by origin
    # (defined_in or canonical_decl(declared_in)). This excludes out-of-closure
    # files (test/) and collapses null-def extern twins (only the real def is in
    # scope.json). Empty when scope.json is absent, so --port-only/--wrap-only
    # yield nothing for a scope-less target (e.g. ".") rather than mislabeling.
    # Synthetic types (string/array clusters) are NOT in scope.json — they are
    # *always* wrap-scope, classified by kind here.
    sub = ("types",) if kind == "type" else ("functions", "globals", "macros")
    # Composed only on the branch that needs it — an unfiltered enumeration
    # must not pay the wrap closure.
    sj = (_scope_mod.try_build(layout, target)
          if (port_only or wrap_only) else None)
    port_keys = (scope.scope_membership(sj, "port", kinds=sub)
                 if port_only and sj is not None else set())
    wrap_keys = (scope.scope_membership(sj, "wrap", kinds=sub)
                 if wrap_only and sj is not None else set())

    rows: list[dict] = []
    if True:
        for e in _entries(layout, target, kind):
            tag = e.get(tagkey)
            if not tag:
                continue
            sk = str(e.get("kind") or "symbol")
            d = e.get("defined_in") or ""
            decls = e.get("declared_in")
            # Match this row's origin_key against the scope set. A type may be
            # listed by a typedef alias (EXT_RETURN) while the manifest uses its
            # tag (ext_return_en), so try the tag OR any typedef. (Anonymous
            # types have no placeable tag, are absent from the manifest, dropped.)
            cands = ((tag,) if kind != "type"
                     else (tag, *(e.get("typedef") or [])))
            if wrap_only and not any(
                    scope.origin_key(c, d, decls) in wrap_keys for c in cands):
                continue
            if port_only and not any(
                    scope.origin_key(c, d, decls) in port_keys for c in cands):
                continue
            if file_set and d not in file_set:
                continue
            rows.append(e)

    rows.sort(key=lambda e: (e.get(tagkey) or "", e.get("defined_in") or ""))

    # One TSV line per (name, kind, defined_in, declared_in) — the
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


def _introspect(
    target: Path, *, kind: str, names, files, fields, ops, manifest,
    wrap_only, port_only, methods=False, field_touchers=False,
    update=None,
) -> None:
    """One named entity's record (summary / whole), or — for a single type —
    its windowable ``--fields`` / ``--ops`` / ``--methods`` / ``--field-touchers``
    lists, the ``--manifest`` types.json that homes it, or a ``--update``
    findings ingest. (The entry's ``.rs`` module is found via ``scaffold
    --name``.)"""
    from crustify import dag as D

    if fields or ops or methods or field_touchers or manifest or update is not None:
        layout, node, by_key = _resolve(target, kind=kind, name=names[0],
                                        files=files,
                                        with_ops=bool(ops or methods))
        if manifest:
            # One store for the whole repo now -- the per-stem manifest an
            # entity used to home in is gone. Kept because agents call it to
            # learn where their submissions land.
            from crustify import store as _store
            print(_store.path(layout))
            return
        if update is not None:
            if kind == "type":
                _update_type(layout, target, node.id, node.defined_in, update)
            else:
                _update_sym(layout, target, node.id, node.defined_in, update)
            return
        if methods:
            # The type's COMPLETE footprint: opaque_in ∪ non_opaque_in — every
            # function tree-wide that touches the type (as a handle or a field).
            # Returned whole by default (the footprint spans the whole codebase,
            # not just the target's scope). --port-only / --wrap-only intersect
            # with that scope's functions (scope.json membership). Schema-agnostic
            # — the agent never opens the manifest.
            entry = _load_type_entry(layout, target, node.id, node.defined_in) or {}
            pool = {s for grp in ("opaque_in", "non_opaque_in")
                    for syms in (entry.get(grp) or {}).values() for s in syms}
            from crustify import scope as _scope_mod
            sj = (_scope_mod.try_build(layout, target)
                  if (wrap_only or port_only) else None)
            if sj is not None:
                from compose import scope as _sc
                keep = {k[0] for k in _sc.scope_membership(
                    sj, "wrap" if wrap_only else "port",
                    kinds=("functions", "globals", "macros"))}
                pool &= keep
            win = sorted(pool)
            print("\n".join(win) if win else "[]")
            return
        if field_touchers:
            # {field: [touchers]} — the type's fields (ALL declared by default;
            # --port-only/--wrap-only narrow to fields touched by that scope's
            # code) mapped to the COMPLETE, UNfiltered set of functions that
            # touch each field (raw t2/field_accesses.csv). The toucher set is
            # never scope-filtered: a field touched in-scope via raw `obj->field`
            # while its real accessor is out of scope still surfaces here.
            _field_touchers(layout, target, node.id, node.defined_in,
                       wrap_only=wrap_only, port_only=port_only)
            return
        meta = D.load_type_meta(_entry_pair(layout, target))
        flds, lifecycle = meta.get(node.id, ([], set()))
        if fields:
            entry = _load_type_entry(layout, target, node.id, node.defined_in)
            objs = (entry.get("fields") if entry else None) or [{"name": f} for f in flds]
            # ALL declared fields by default; --port-only/--wrap-only narrow to
            # fields touched by that scope's code (raw field_accesses ∩ scope.json
            # membership).
            keep = _field_keep_set(layout, target, node.id, node.defined_in,
                                   wrap_only=wrap_only, port_only=port_only)
            if keep is not None:
                objs = [o for o in objs if o.get("name") in keep]
            win = objs
            print(json.dumps(win, indent=2))
            return
        # --ops: names only, lifecycle-first, scope-filterable via scope.json
        # membership (same oracle as enumeration / wrap / port).
        op_pred = lambda _n: True            # noqa: E731
        if wrap_only or port_only:
            from compose import scope as _sc
            from crustify import scope as _scope_mod
            sj = _scope_mod.try_build(layout, target)
            op_pred = (_sc.in_scope_pred(sj, "wrap" if wrap_only else "port")
                       if sj is not None else (lambda _n: False))
        win = D.ordered_ops(node, by_key, lifecycle, op_pred)
        print("\n".join(o.id for o in win))
        return

    # record(s): always the whole record.
    return _records(target, kind, names, files,
                    wrap_only=wrap_only, port_only=port_only)


_TOUCHED_CACHE: dict = {}


def scope_touched_index(layout, target, which: str) -> dict:
    """``{tag: {def_file|"": {field}}}`` — every field some `which`-scope
    (port|wrap) function touches, in ONE pass. Cached per (target, which).

    The UNION of both access edges, because they answer different questions and
    a type can owe an accessor under either:

    ``t2/fa_with_root``     the outermost NAMED container plus a dotted member
                            path. The only edge that resolves an anonymous
                            aggregate embedded in a named struct, so without it
                            ``ssl_session_st`` narrows to 32 of its 41 fields,
                            losing the whole ``ext.*`` group.

    ``t2/field_accesses``   the IMMEDIATE declaring type. The only edge that
                            names a by-value-embedded struct: ``s->ts_msg_read.t``
                            is `ssl_connection_st`/`ts_msg_read.t` to the walk
                            above, but `OSSL_TIME`/`t` here — and per
                            ``prompts/principles.md`` both owe something, the container
                            a projecting getter and the embedded type its own
                            field accessor.

    Taking either alone loses the other's case; ``fa_with_root`` is not a
    superset of ``field_accesses``, which is the trap this docstring exists to
    stop someone (me, twice) falling into.
    """
    import csv as _csv
    from compose import scope as _sc

    key = (str(layout.repo_root), str(target), which)
    if key in _TOUCHED_CACHE:
        return _TOUCHED_CACHE[key]
    from crustify import scope as _scope_mod
    sj = _scope_mod.try_build(layout, target)
    if sj is None:
        return {}
    funcs = {k[0] for k in _sc.scope_membership(
        sj, which, kinds=("functions", "globals", "macros"))}
    out: dict = {}

    def add(tag, def_file, field):
        if tag and field:
            out.setdefault(tag, {}).setdefault(def_file or "", set()).add(field)

    for name, tag_col, file_col, fld_col in (
            ("fa_with_root.csv", "root_struct_name", "root_struct_def_file",
             "field_path"),
            ("field_accesses.csv", "struct_name", "struct_def_file",
             "field_name")):
        fac = layout.t2 / name
        if not fac.exists():
            continue
        with fac.open() as fh:
            for r in _csv.DictReader(fh):
                if r.get("enclosing_name") in funcs:
                    add(r.get(tag_col), r.get(file_col), r.get(fld_col))
    _TOUCHED_CACHE[key] = out
    return out


def _scope_touched_fields(layout, target, tag: str, defined_in: str | None,
                          which: str) -> set:
    """Field names of `tag` touched by some function in scope `which`
    (port|wrap). Empty if no scope.json. Drives the --port-only/--wrap-only
    narrowing for --fields and --field-touchers, and — through
    `scope_touched_index` — which fields the scaffolder anchors."""
    by_file = scope_touched_index(layout, target, which).get(tag) or {}
    if defined_in and defined_in in by_file:
        return set(by_file[defined_in])
    return {f for s in by_file.values() for f in s}


def _field_keep_set(layout, target, tag: str, defined_in: str | None, *,
                    wrap_only: bool, port_only: bool) -> set | None:
    """Field-name keep-set for the -only narrowing, or None = keep ALL fields.
    --port-only / --wrap-only → fields touched by that scope's code."""
    if not (wrap_only or port_only):
        return None
    which = "wrap" if wrap_only else "port"
    return _scope_touched_fields(layout, target, tag, defined_in, which)


def _field_touchers(layout, target, tag: str, defined_in: str | None, *,
               wrap_only: bool = False, port_only: bool = False) -> None:
    """``{field: [touchers]}`` for the type's fields.

    ALL declared fields by default; --port-only/--wrap-only narrow the FIELD set
    to the ones touched by that scope's code. Each field's toucher set is the
    COMPLETE, UNfiltered set of functions that access it — read straight from the
    raw ``t2/field_accesses`` edge, NOT the port-scope ``depends_on`` inversion.
    So a toucher that is itself out of scope (while the field is touched in-scope
    via raw ``obj->field``) still surfaces as a candidate."""
    import csv as _csv
    from collections import defaultdict

    entry = _load_type_entry(layout, target, tag, defined_in) or {}
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
                           wrap_only=wrap_only, port_only=port_only)
    scoped = set(declared) if keep is None else {f for f in declared if f in keep}

    out = {f: sorted(complete.get(f, set())) for f in sorted(scoped)}
    print(json.dumps({"name": tag, "fields": out}, indent=2))


# ----------------------------------------------------------- --update ingest

# A type stores NO lifecycle of its own. Which routines drop / dispose / clone
# it is recorded once, on the acting symbol (`syms.json`'s entry-level
# `lifetime`), and read back by reverse lookup -- `query symbols --lifetime-for
# <TAG>`, or `scope.build_lifecycle_index` composer-side. So the type findings
# surface is the field layout only.
_FINDINGS_TOP = {"fields", "_comment_agent"}
# `refcount` marks the ONE field that stores the type's reference count (the
# datum an up_ref bumps and a down-ref decrements). It is what makes the type
# decides which ROUTINE backs the type's `CDropped`/`CCloned` impl (down-ref and
# up_ref vs `*_free` and `*_dup`) -- the wrapper is `CBox` either way -- and it
# names the field a generated shim reads when the type carries a refcount but
# exposes no up_ref function.
# Ordered, not a set: this drives the key order an agent's submission lands in,
# and set iteration made that arbitrary. Mirrors `store.FIELD_KEYS`.
_FIELD_AGENT_KEYS = ("ptr", "refcount", "locked_by")

# Symbol findings (functions / callbacks / macros) — the agent-fillable surface.
_SYM_FINDINGS_TOP = {"ptr_args", "ptr_ret", "lifetime", "forks", "ptr",
                     "locked_by"}
# Same structured ownership block as a struct field's `ptr` (see types.md#ptr):
# `owned` is a bool, `borrowed` nests {lifetime}, `array` is
# null|{by_val}|{by_ref:{owned,borrowed}}. A pointer at a call boundary and a
# pointer in a struct extract identical properties, so args, returns, globals and
# struct fields all carry the IDENTICAL key set -- the lifecycle role is not a
# property of a pointer record but of the symbol (see `_LIFETIME_KEYS`).
_PTR_AGENT_KEYS = {"scalar", "array", "string", "owned", "borrowed",
                   "nullable", "mutable", "note"}
# The SYMBOL-level lifetime block (functions / callbacks): which lifecycle-
# primitive role this symbol plays, and on which arg. `for` names that arg BY
# NAME -- the same vocabulary a borrowed pointer uses for its source
# (`arg:<name>`), since both are arg-dependent facts. `is_dropper` (bool) = frees
# the arg's own storage; `is_disposer` (bool) = frees the arg's fields but KEEPS
# its storage -- MUTUALLY EXCLUSIVE, the storage is either released or retained.
# `is_cloner` = null | {deep, upref} -- `deep` copies into a fresh allocation,
# `upref` bumps a refcount; the two co-exist on a body that branches between
# them, and on an untyped `void *` whose concrete element decides at runtime.
_LIFETIME_KEYS = {"for", "is_dropper", "is_disposer", "is_cloner"}
_FORK_KEYS = {"ptr_args", "ptr_ret", "lifetime", "callsites"}
# The concurrency binding on a GLOBAL (or, in types.json, a struct field): the
# lock object guarding the slot plus its acquire/release op lists. It sits at the
# entry level (sibling of `ptr`), not inside `ptr`, because the guarded datum is
# often a non-pointer (a refcount int, a flag).
_LOCKED_BY_KEYS = {"lock", "lock_op", "unlock_op"}


def _schema(kind: str) -> str:
    """Field/slot MEANING for ``--schema`` — display-only markdown the wrapper
    reads at runtime instead of opening the templates. Distinct from
    ``--update-help`` (:func:`_findings_schema`), which gives the submission
    *shape* + rules; meaning and shape are never duplicated.

    Primary source: ``docs/schemas/<types|syms>.md``, split on ``## <field>``
    headings. Falls back to the template's legacy ``_comment_*`` blocks
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
        body = " ".join(v) if isinstance(v, list) else v
        out.append(f"## {field}\n{body}")
    return "\n\n".join(out).rstrip() + "\n"


def _findings_schema(kind: str) -> dict:
    """The findings JSON a wrapper agent submits through ``--update`` — the
    schema boundary, returned by ``--update-help`` so the agent discovers it at
    runtime instead of hard-coding it. The top-level key sets are the validator's
    own (``_FINDINGS_TOP`` / ``_SYM_FINDINGS_TOP``), so this never drifts from
    what ``--update`` actually accepts."""
    # Raw submission SHAPE only -- field MEANING + enforced invariants live in the
    # schema doc (`query <kind> --schema` = docs/schemas/{types,syms}.md), which is
    # fed to the agent. `_valid_top_keys` is the drift guard (validator constants).
    if kind == "type":
        return {
            "_subject": "type",
            "_see": "query types --schema for field meaning + enforced invariants",
            "_lifecycle": ("NOT submitted here — which routines drop / dispose / "
                           "clone this type is recorded on the acting SYMBOL "
                           "(syms.json `lifetime`), and read back with "
                           "`query symbols --lifetime-for <TAG>`."),
            "fields": {
                "<field_name>": {
                    "refcount": "bool   (this field stores the type's refcount)",
                    "ptr": {
                        "scalar": ("null | {by_val: true} | {by_ref: {owned: "
                                   "<owned>|null, borrowed: {lifetime}|null}}"),
                        "array": ("null | {by_val: true} | "
                                  "{by_ref: {owned: <owned>|null, "
                                  "borrowed: {lifetime}|null}}"),
                        "string": "bool",
                        "owned": "bool",
                        "borrowed": "null | {lifetime: <source>}",
                        "nullable": "bool", "mutable": "bool | null", "note": "<str>",
                    },
                    "locked_by": ("null | {lock: <name>, lock_op: [<fn>], "
                                  "unlock_op: [<fn>]}"),
                }
            },
            "_comment_agent": "<optional>",
            "_valid_top_keys": sorted(_FINDINGS_TOP),
        }
    return {
        "_subject": "symbol",
        "_see": "query syms --schema for facet meaning + enforced invariants",
        "ptr_args": {
            "<position:int>": {
                "ptr": {
                    "scalar": ("null | {by_val: true} | {by_ref: {owned: <owned>|"
                               "null, borrowed: {lifetime}|null}}"),
                    "array": ("null | {by_val: true} | {by_ref: {owned: <owned>|"
                              "null, borrowed: {lifetime}|null}}"),
                    "string": "bool",
                    "owned": "bool",
                    "borrowed": "null | {lifetime: <source>}",
                    "nullable": "bool", "mutable": "bool | null", "note": "<str>",
                }
            }
        },
        "ptr_ret": "{ptr: <same block as ptr_args[pos].ptr>} | null",
        "lifetime": ("null | {for: <arg name>, is_dropper: bool, is_disposer: "
                     "bool, is_cloner: {deep: bool, upref: bool}|null}   "
                     "(functions/callbacks: THIS symbol's lifecycle role, on the "
                     "arg named by `for`; null = no role)"),
        "ptr": "<same block as ptr_args[pos].ptr> | null   (globals only)",
        "locked_by": ("null | {lock: <name>, lock_op: [<fn>], unlock_op: [<fn>]}"
                      "   (globals only)"),
        "forks": [{
            "ptr_args": "<as ptr_args>", "ptr_ret": "<as ptr_ret>",
            "lifetime": "<as lifetime>", "callsites": ["<callsite id>"],
        }],
        "_valid_top_keys": sorted(_SYM_FINDINGS_TOP),
    }


def _apply_ptr_agent(entry: dict, ptr_args_f: dict | None,
                     ptr_ret_f: dict | None, contract: dict | None = None,
                     has_lifetime: bool = False) -> None:
    """Apply one agent ownership contract onto an entry's structural records, in
    place: `ptr_args` (keyed by position), `ptr_ret`, and the entry-level
    `lifetime`. The agent-owned ownership block nests under each record's `ptr`
    key (isolated from the composer's position/name/type/const/depth), so it is
    replaced WHOLESALE -- like a struct field's ptr.

    `lifetime` is applied only when the findings actually carry the key
    (`has_lifetime`): omitting it leaves any prior role standing, while an
    explicit `null` clears it. Taking the whole `contract` dict is what lets a
    callback FORK carry its own role — a fork deep-copies the primary, so
    without this it would silently inherit the primary's."""
    by_pos = {str(a.get("position")): a for a in entry.get("ptr_args") or []}
    for pos, rec in (ptr_args_f or {}).items():
        arg = by_pos.get(str(pos))
        if arg is not None and isinstance(rec, dict) and "ptr" in rec:
            arg["ptr"] = rec["ptr"]
    if (ptr_ret_f is not None and isinstance(ptr_ret_f, dict)
            and "ptr" in ptr_ret_f and entry.get("ptr_ret") is not None):
        entry["ptr_ret"]["ptr"] = ptr_ret_f["ptr"]
    if has_lifetime and contract is not None:
        entry["lifetime"] = contract.get("lifetime")


def _shape_slot_errors(label: str, slot, name: str) -> list[str]:
    """Validate a by_val/by_ref cardinality slot -- `scalar` (ONE pointee) or
    `array` (a buffer). Both share one grammar: `null` | `{by_val: true}` (inline
    value(s)) | `{by_ref: {owned, borrowed}}` (pointer(s) to element(s), whose
    ownership nests). So a single-pointee `T**` is `scalar.by_ref` and a buffer of
    element pointers is `array.by_ref`."""
    e: list[str] = []
    if slot is None:
        return e
    if not isinstance(slot, dict):
        return [f"{label}: {name} must be null or {{by_val|by_ref}}"]
    kinds = [k for k in ("by_val", "by_ref") if slot.get(k)]
    if len(kinds) != 1:
        e.append(f"{label}: {name} needs exactly one of by_val / by_ref")
    elif "by_ref" in kinds and not isinstance(slot["by_ref"], dict):
        e.append(f"{label}: {name}.by_ref must be {{owned, borrowed}}")
    return e


def _shape_by_ref_element_errors(label: str, slot, name: str) -> list[str]:
    """A by_ref ELEMENT (the inner pointee of a `scalar`/`array` by_ref) obeys the
    same owned-and/or-borrowed-never-neither invariant as any pointer."""
    if not (isinstance(slot, dict) and isinstance(slot.get("by_ref"), dict)):
        return []
    eo = slot["by_ref"].get("owned")
    eb = slot["by_ref"].get("borrowed")
    if not (eo is True or isinstance(eb, dict)):
        return [f"{label}: {name}.by_ref element must be owned and/or borrowed, "
                "never neither"]
    return []


def _ptr_invariant_errors(field: str, ptr: dict, field_type: str) -> list[str]:
    """Hard-reject the IMPOSSIBLE / inconsistent shapes in one field's `ptr`
    block. Ownership *dependencies* are STRUCTURAL (unrepresentable otherwise):
    `lifetime` nests under `borrowed`, element-ownership under `array.by_ref` —
    so only shape validity, borrowed-requires-lifetime, and const/mutable
    remain.

    `scalar`, `array` and `string` are three INDEPENDENT path-existential
    questions ("is there any execution path where..."), so any combination is
    legal: a `char *` that is a counted buffer when a length is passed and a
    NUL-terminated string when it is -1 sets both.

    Dual ownership is allowed: `owned` and `borrowed` may BOTH be set
    (runtime-conditional), likewise `array.by_ref.owned`+`.borrowed`."""
    e: list[str] = []
    flabel = f"field {field!r}"
    scalar = ptr.get("scalar")
    array = ptr.get("array")
    # `scalar` and `array` share one by_val/by_ref cardinality grammar (see
    # _shape_slot_errors): scalar points at ONE pointee, array at a buffer.
    e += _shape_slot_errors(flabel, scalar, "scalar")
    e += _shape_slot_errors(flabel, array, "array")
    # `string` is the one remaining explicit-bool discriminant (scalar/array are
    # now shape objects) -- reject a null left where false was meant.
    if not isinstance(ptr.get("string"), bool):
        e.append(f"{flabel}: string must be an explicit boolean (true/false, not null)")
    # Floor: a pointer is at least one of scalar / array / string.
    if not (scalar is not None or array is not None or ptr.get("string")):
        e.append(f"{flabel}: a pointer must set at least one of "
                 "scalar / array / string (none are set)")
    borrowed = ptr.get("borrowed")
    if isinstance(borrowed, dict) and not borrowed.get("lifetime"):
        e.append(f"field {field!r}: borrowed set but lifetime unset")
    if "lifetime" in ptr:
        e.append(f"field {field!r}: `lifetime` (is_dropper/is_disposer/is_cloner) "
                 "is a SYMBOL-level block in syms.json -- a struct field's "
                 "lifecycle derives from its field-type's record, not from the "
                 "field")
    owned = ptr.get("owned")
    # `owned` is the ownership FACT -- reject a null left where false was meant.
    if not isinstance(owned, bool):
        e.append(f"field {field!r}: owned must be an explicit boolean "
                 "(true/false, not null)")
    # A pointer is owned and/or borrowed, NEVER neither: a non-owned reference is
    # a borrow with a lifetime (its own arg's, when call-scoped). `owned:false` +
    # `borrowed:null` is invalid.
    if not (owned is True or isinstance(borrowed, dict)):
        e.append(f"field {field!r}: a pointer must be owned and/or borrowed, "
                 "never neither (a non-owned reference is a borrow with a lifetime)")
    # Same invariant on any by_ref ELEMENT pointer (scalar.by_ref or array.by_ref).
    e += _shape_by_ref_element_errors(flabel, scalar, "scalar")
    e += _shape_by_ref_element_errors(flabel, array, "array")
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
    """Ingest an agent *findings* doc into `ownership-store.json` — the schema
    boundary, so the agent never opens the store.

    `src` is a path, or ``"-"`` for stdin. The findings doc is the flat,
    name-keyed agent shape (`fields: {name: {ptr, refcount, locked_by}}` +
    optional `_comment_agent`). We HARD-REJECT (and apply nothing) on structural
    contradictions, unknown field names and ptr-invariant violations; otherwise
    partial-merge (only the fields mentioned) under a lock + atomic rename.

    Validation reads the COMPOSED record and the write goes to the store: the
    two halves the old single file conflated. That is what makes the store safe
    to keep structure-free -- nothing is checked against a stored copy of the
    layout that could have gone stale."""
    from crustify import store as _store

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

    # Validate against the COMPOSED skeleton. The store holds no structure, so
    # this is the only thing that knows the type's field layout and each
    # field's C type -- which is what makes "unknown field" and the ptr
    # invariants checkable at all.
    entry = next((e for e in _entries(layout, target, "types") if _id_match(e)),
                 None)
    if entry is None:
        entry = _universe_entry(layout, target, "types", _id_match)
    if entry is None:
        raise SystemExit(
            f"--update: no type {tag!r} in the CodeQL universe"
            f"{f' at {defined_in}' if defined_in else ''}.")

    field_by_name = {fld.get("name"): fld for fld in entry.get("fields") or []}
    errors: list[str] = []
    for fname, fa in (f.get("fields") or {}).items():
        if fname not in field_by_name:
            errors.append(f"unknown field {fname!r} (not in {tag}'s layout)")
            continue
        bad = set(fa) - set(_FIELD_AGENT_KEYS)
        if bad:
            errors.append(f"field {fname!r}: unknown key(s) {sorted(bad)}")
        if "ptr" in fa and isinstance(fa["ptr"], dict):
            errors += _ptr_invariant_errors(
                fname, fa["ptr"], field_by_name[fname].get("type") or "")
        if fa.get("locked_by") is not None:
            errors += _locked_by_errors(
                f"field {fname!r} locked_by", fa["locked_by"])
        if "refcount" in fa and not isinstance(fa["refcount"], bool):
            errors.append(f"field {fname!r}: refcount must be a boolean")
    if errors:
        raise SystemExit(
            "--update REJECTED — fix and re-run:\n  - " + "\n  - ".join(errors))

    def _apply(doc: dict) -> None:
        # Keyed by the COMPOSED entry's identity, not the caller's spelling: a
        # type reached by a typedef alias, or with a null `defined_in`, must
        # land on the same record the overlay will look for.
        rec = _store.upsert_type(doc, entry.get("name") or entry.get("type"),
                                 entry.get("defined_in"))
        if "_comment_agent" in f:
            rec["_comment_agent"] = f["_comment_agent"]
        by_name = {x.get("name"): x for x in rec.setdefault("fields", [])}
        for fname, fa in (f.get("fields") or {}).items():
            dst = by_name.get(fname)
            if dst is None:
                dst = {"name": fname}
                rec["fields"].append(dst)
                by_name[fname] = dst
            for k in _FIELD_AGENT_KEYS:
                if k in fa:
                    dst[k] = fa[k]

    _store.update(layout, _apply)
    print(f"updated {tag} in {_store.path(layout)}")


def _borrow_arg_ref_errors(label: str, borrowed, valid_args) -> list[str]:
    """If a borrowed lifetime names an arg (`arg:<name>`, optionally with a
    `->path` suffix), that `<name>` must be a real arg of the symbol, referenced
    BY NAME. `valid_args` is the set of the symbol's pointer-arg names (a
    non-pointer arg has no storage to borrow; the composer names every arg, real
    or synthetic `arg<pos>`, so a name always exists). The positional form
    `arg:<idx>` is rejected -- names are the one canonical spelling. Pass `None`
    to skip (caller lacks arg context, e.g. standalone template checks)."""
    if valid_args is None or not isinstance(borrowed, dict):
        return []
    lt = borrowed.get("lifetime")
    if not isinstance(lt, str) or not lt.startswith("arg:"):
        return []
    ident = lt[len("arg:"):].split("->", 1)[0].strip()
    if ident in valid_args:
        return []
    if ident.isdigit():
        return [f"{label}: borrowed lifetime {lt!r} uses a positional index; "
                f"reference the arg BY NAME (one of {sorted(valid_args)})"]
    return [f"{label}: borrowed lifetime {lt!r} names {ident!r}, not a valid "
            f"arg of this symbol (expected one of {sorted(valid_args)})"]


def _sym_ptr_invariant_errors(label: str, blk: dict, const: bool,
                              is_ret: bool, arg_names=None) -> list[str]:
    """Hard-reject the IMPOSSIBLE shapes in one symbol `ptr_args[*].ptr` /
    `ptr_ret.ptr` (or a global `ptr`) block. These are the SAME structural
    invariants a struct field's `ptr` obeys (see check_types_consistency):
    `scalar`/`array` are null|{by_val}|{by_ref} (scalar = one pointee, array = a
    buffer); string explicit + the {scalar,array,string} floor; borrowed⟹lifetime; owned is an explicit bool; const⟹mutable≠true.

    `owned`+`borrowed` is NOT rejected: an arg or return may be BOTH to mean
    runtime-conditional dual ownership (owned on one path, borrowed on another);
    likewise scalar/array.by_ref.owned+borrowed. (`is_ret` is retained for
    call-site symmetry; the invariants are uniform across args and returns.)"""
    e: list[str] = []
    # `scalar` and `array` share one by_val/by_ref cardinality grammar (see
    # _shape_slot_errors): scalar points at ONE pointee, array at a buffer. A
    # submitted block replaces the prior WHOLESALE, so it must be complete --
    # `string` is an explicit bool and a pointer sets at least one of
    # {scalar, array, string} (the floor, enforceable per-submission).
    scalar = blk.get("scalar")
    array = blk.get("array")
    e += _shape_slot_errors(label, scalar, "scalar")
    e += _shape_slot_errors(label, array, "array")
    if not isinstance(blk.get("string"), bool):
        e.append(f"{label}: string must be an explicit boolean (true/false, not null)")
    if not (scalar is not None or array is not None or blk.get("string")):
        e.append(f"{label}: a pointer must set at least one of "
                 "scalar / array / string (none are set)")
    borrowed = blk.get("borrowed")
    if isinstance(borrowed, dict) and not borrowed.get("lifetime"):
        e.append(f"{label}: borrowed set but lifetime unset")
    e.extend(_borrow_arg_ref_errors(label, borrowed, arg_names))
    owned = blk.get("owned")
    # `owned` is the ownership FACT -- reject a null left where false was meant.
    if not isinstance(owned, bool):
        e.append(f"{label}: owned must be an explicit boolean (true/false, not null)")
    # A pointer is owned and/or borrowed, NEVER neither: a non-owned reference is
    # a borrow with a lifetime (the arg's own name, e.g. `arg:file`, when call-scoped).
    if not (owned is True or isinstance(borrowed, dict)):
        e.append(f"{label}: a pointer must be owned and/or borrowed, never neither "
                 "(a non-owned reference is a borrow with a lifetime)")
    # Same owned∨borrowed invariant + arg-ref check on any by_ref ELEMENT
    # (scalar.by_ref -- a `T**` -- or array.by_ref -- a container).
    for slot, sname in ((scalar, "scalar"), (array, "array")):
        e += _shape_by_ref_element_errors(label, slot, sname)
        if isinstance(slot, dict) and isinstance(slot.get("by_ref"), dict):
            e.extend(_borrow_arg_ref_errors(
                f"{label} {sname}.by_ref element", slot["by_ref"].get("borrowed"),
                arg_names))
    if const and blk.get("mutable") is True:
        e.append(f"{label}: const pointee but mutable == true")
    # The lifecycle role is a property of the SYMBOL, not of one of its pointer
    # records — it lives on the entry's top-level `lifetime`, which names its
    # subject arg in `for`.
    if "lifetime" in blk:
        e.append(f"{label}: `lifetime` (is_dropper/is_disposer/is_cloner) is a "
                 "SYMBOL-level block, not a ptr key — submit it at the top level "
                 "of the findings, naming this arg in `lifetime.for`")
    return e


def _lifetime_errors(label: str, lf, arg_ptr_by_name: dict | None) -> list[str]:
    """Validate a SYMBOL-level `lifetime` block: which lifecycle-primitive role
    this function/callback plays, and on which arg.

    `null` is both "no lifecycle role" and the composer's unprocessed state, so
    it is always accepted. A non-null block is
    ``{for, is_dropper, is_disposer, is_cloner}``:

      - **`for`** -- the arg the role acts on, BY NAME. Same vocabulary as a
        borrowed pointer's `arg:<name>` source, so every arg-dependent fact in
        the schema references args one way; the positional form is rejected.
      - **`is_dropper`** (bool) -- frees the arg's own STORAGE (a full dtor).
      - **`is_disposer`** (bool) -- frees the storage of the arg's FIELDS but
        KEEPS the arg's own storage (a teardown / `*_cleanup` / reset).
      - **`is_cloner`** -- null | {deep, upref}: `deep` copies the arg into a
        fresh allocation, `upref` bumps its refcount. Both modes may be set at
        once: a body that branches between them, or an untyped `void *` whose
        concrete element decides at runtime.

    `is_dropper` and `is_disposer` are MUTUALLY EXCLUSIVE -- the arg's storage is
    either released or retained, never both, so a full destructor is `is_dropper`
    alone (it disposes the fields on the way, but the observable contract is that
    the allocation is gone) and a cleanup that resets the fields in place is
    `is_disposer` alone. A block that asserts NO role is rejected: submit `null`.

    Interaction rules with the named arg's ownership (`arg_ptr_by_name` maps arg
    name -> its post-merge `ptr` block; pass `None` to skip when the caller lacks
    arg context):
      - `is_dropper`  => that arg is `owned` (you free the storage of what you own).
      - `is_cloner`   => that arg is `borrowed` (it reads the source to copy it).
      - `is_disposer` => EITHER (a full dtor owns it; a `*_cleanup` borrows it).
    An arg whose `ptr` is still `null` (unanalyzed) is exempt from these — the
    ownership fact does not exist yet to contradict.
    """
    e: list[str] = []
    if lf is None:
        return e
    if not isinstance(lf, dict):
        e.append(f"{label}: must be null or "
                 "{for, is_dropper, is_disposer, is_cloner}")
        return e
    bad = set(lf) - _LIFETIME_KEYS
    if bad:
        e.append(f"{label}: unknown key(s) {sorted(bad)}")
    for k in ("is_dropper", "is_disposer"):
        v = lf.get(k)
        if v is not None and not isinstance(v, bool):
            e.append(f"{label}.{k}: must be a boolean")
    if lf.get("is_dropper") is True and lf.get("is_disposer") is True:
        e.append(f"{label}: is_dropper and is_disposer are mutually exclusive — "
                 "a routine either frees the arg's storage (is_dropper, which "
                 "subsumes tearing its fields down) or keeps it and resets the "
                 "fields (is_disposer), never both")
    cloned = lf.get("is_cloner")
    if cloned is not None:
        if not isinstance(cloned, dict) or (set(cloned) - {"deep", "upref"}):
            e.append(f"{label}.is_cloner: must be null or {{deep, upref}}")
            cloned = None
        else:
            # A submitted `is_cloner` replaces the prior wholesale, so both modes
            # must be stated -- reject a null left where false was meant.
            for mode in ("deep", "upref"):
                if not isinstance(cloned.get(mode), bool):
                    e.append(f"{label}.is_cloner.{mode}: must be an explicit "
                             "boolean")
    any_clone = isinstance(cloned, dict) and bool(
        cloned.get("deep") or cloned.get("upref"))
    # A block that claims nothing is not a finding — `null` is how "this symbol
    # is not a lifecycle primitive" is recorded, and it is what the composer
    # already emits.
    if not (lf.get("is_dropper") is True or lf.get("is_disposer") is True
            or any_clone):
        e.append(f"{label}: asserts no role — set at least one of is_dropper / "
                 "is_disposer / is_cloner.{deep,upref}, or submit `lifetime: "
                 "null` for a symbol that is not a lifecycle primitive")

    subject = lf.get("for")
    if not isinstance(subject, str) or not subject.strip():
        e.append(f"{label}.for: required — name the arg this role acts on "
                 "(a bare arg name, not `arg:<name>` and not a position)")
        return e
    if arg_ptr_by_name is None:
        return e
    if subject.startswith("arg:") or subject.isdigit():
        e.append(f"{label}.for: {subject!r} — use the BARE arg name (the "
                 "`arg:` prefix belongs to a borrowed lifetime, and the "
                 f"positional form is rejected); expected one of "
                 f"{sorted(arg_ptr_by_name)}")
        return e
    if subject not in arg_ptr_by_name:
        e.append(f"{label}.for: {subject!r} is not a pointer arg of this symbol "
                 f"(expected one of {sorted(arg_ptr_by_name)})")
        return e

    # Cross-check against the subject arg's ownership, as it will stand AFTER
    # this update (a findings doc may set the role and the ownership together).
    blk = arg_ptr_by_name[subject]
    if not isinstance(blk, dict):
        return e   # arg still unanalyzed — no ownership fact to contradict
    if lf.get("is_dropper") is True and blk.get("owned") is not True:
        e.append(f"{label}: is_dropper requires arg {subject!r} to be owned "
                 "(you free the storage of what you own)")
    if any_clone and not isinstance(blk.get("borrowed"), dict):
        e.append(f"{label}: is_cloner requires arg {subject!r} to be borrowed "
                 "(it reads the source to copy it)")
    return e


def _locked_by_errors(label: str, lb) -> list[str]:
    """Validate a `locked_by` block: null | {lock, lock_op, unlock_op}. `lock`
    names the guarding lock object; `lock_op`/`unlock_op` are lists of the real
    acquire/release functions (the read-vs-write discipline lives in which ops are
    listed). Shared by globals (syms.json) and, later, struct fields (types.json)."""
    e: list[str] = []
    if not isinstance(lb, dict):
        e.append(f"{label}: must be null or {{lock, lock_op, unlock_op}}")
        return e
    bad = set(lb) - _LOCKED_BY_KEYS
    if bad:
        e.append(f"{label}: unknown key(s) {sorted(bad)}")
    if "lock" in lb and not isinstance(lb["lock"], str):
        e.append(f"{label}.lock: must be a string (the guarding lock's name)")
    for k in ("lock_op", "unlock_op"):
        if k in lb and not (
                isinstance(lb[k], list) and all(isinstance(x, str) for x in lb[k])):
            e.append(f"{label}.{k}: must be a list of function names")
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

    from crustify import store as _store

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

    holder = {"n": 0}

    def _is_primary(e: dict) -> bool:
        # Identity is `defined_in or canonical_decl(declared_in)`: a callback
        # (and other header-only decls) has a null `defined_in`, so a caller's
        # file identifies it via `declared_in`.
        if e.get("name") != name or (e.get("variant") or 0) != 0:
            return False
        if defined_in is None or e.get("defined_in") == defined_in:
            return True
        return e.get("defined_in") is None and defined_in in (
            e.get("declared_in") or [])

    # The primary (variant 0), from the COMPOSED records: signature, arg names
    # and kind all come from there, which is what the validation below checks
    # against. The store holds no structure to check against.
    entry = next((e for e in _entries(layout, target, "symbols")
                  if _is_primary(e)), None)
    if entry is None:
        entry = _universe_entry(layout, target, "symbols", _is_primary)
    if entry is None:
        raise SystemExit(
            f"--update: no symbol {name!r} in the CodeQL universe"
            f"{f' at {defined_in}' if defined_in else ''}.")

    def _apply(doc: dict) -> None:
        errors: list[str] = []
        ekind = entry.get("kind") or ""

        # `ptr` / `locked_by`: GLOBALS only. A global has no call boundary, so it
        # carries no ptr_args/ptr_ret; instead a pointer global gets a single
        # `ptr` block (same shape as a ptr_args record) and any lock-guarded
        # global gets `locked_by`.
        is_global = ekind.startswith("global")
        gptr = f.get("ptr")
        if gptr is not None:
            if not is_global:
                errors.append(
                    f"ptr: {name!r} is {ekind!r}, not a global — the singular "
                    f"`ptr` block is for globals; functions/callbacks use "
                    f"ptr_args/ptr_ret")
            elif not isinstance(gptr, dict):
                errors.append("ptr: must be an object")
            else:
                bad = set(gptr) - _PTR_AGENT_KEYS
                if bad:
                    errors.append(f"ptr: unknown key(s) {sorted(bad)}")
                errors.extend(_sym_ptr_invariant_errors(
                    "ptr", gptr, "const" in (entry.get("type") or ""),
                    is_ret=False, arg_names=set()))
        if "locked_by" in f and f["locked_by"] is not None:
            if not is_global:
                errors.append(
                    f"locked_by: {name!r} is {ekind!r}, not a global — a struct "
                    f"field's lock binding lives on its field record (types.json)")
            else:
                errors.extend(_locked_by_errors("locked_by", f["locked_by"]))

        arg_by_pos = {str(a.get("position")): a
                      for a in entry.get("ptr_args") or []}
        # Valid `arg:<ref>` lifetime targets: each pointer arg's NAME (names are
        # the one canonical spelling; the positional `arg:<idx>` form is rejected).
        # A borrowed lifetime that names an arg (on an arg OR the return) must
        # land on one of these.
        valid_args = {a.get("name") for a in arg_by_pos.values() if a.get("name")}
        pr = f.get("ptr_ret")
        has_args = bool(entry.get("ptr_args"))

        def _post_merge_arg_ptrs(args_f) -> dict:
            """Arg name -> the `ptr` block as it will stand AFTER this update.

            `lifetime.for` names an arg whose ownership the role must agree with,
            and a findings doc routinely sets both at once — so the cross-check
            reads the SUBMITTED block where there is one and the on-disk block
            otherwise, rather than the pre-update state alone."""
            out = {}
            for a in arg_by_pos.values():
                if not a.get("name"):
                    continue
                out[a["name"]] = a.get("ptr")
            for pos, rec in (args_f or {}).items():
                a = arg_by_pos.get(str(pos))
                if a is not None and a.get("name") and isinstance(rec, dict) \
                        and "ptr" in rec:
                    out[a["name"]] = rec["ptr"]
            return out

        # Validates one ownership contract — the primary's (`where=""`) or a
        # fork's. Both shapes are identical, so forks reuse it verbatim.
        # `has_lt` distinguishes an omitted `lifetime` (leave as-is) from an
        # explicit `null` (clear the role), which matter differently on merge.
        def _check_ptr(args_f, ret_f, where, lifetime_f=None, has_lt=False):
            for pos, rec in (args_f or {}).items():
                if not isinstance(rec, dict):
                    errors.append(f"{where}ptr_args[{pos}]: must be an object")
                    continue
                if str(pos) not in arg_by_pos:
                    errors.append(
                        f"{where}ptr_args: no pointer arg at position {pos} "
                        f"in {name!r}")
                    continue
                bad = set(rec) - {"ptr"}
                if bad:
                    errors.append(
                        f"{where}ptr_args[{pos}]: unknown key(s) {sorted(bad)} "
                        "(the ownership block nests under `ptr`)")
                blk = rec.get("ptr")
                if blk is None:
                    continue
                if not isinstance(blk, dict):
                    errors.append(f"{where}ptr_args[{pos}].ptr: must be an object")
                    continue
                bad2 = set(blk) - _PTR_AGENT_KEYS
                if bad2:
                    errors.append(
                        f"{where}ptr_args[{pos}].ptr: unknown key(s) {sorted(bad2)}")
                errors.extend(_sym_ptr_invariant_errors(
                    f"{where}ptr_args[{pos}].ptr", blk,
                    bool(arg_by_pos[str(pos)].get("const")), is_ret=False,
                    arg_names=valid_args))
            if ret_f is not None:
                if not isinstance(ret_f, dict):
                    errors.append(f"{where}ptr_ret: must be an object")
                elif entry.get("ptr_ret") is None:
                    errors.append(f"{where}ptr_ret: {name!r} has no pointer return")
                else:
                    bad = set(ret_f) - {"ptr"}
                    if bad:
                        errors.append(f"{where}ptr_ret: unknown key(s) {sorted(bad)} "
                                      "(the ownership block nests under `ptr`)")
                    blk = ret_f.get("ptr")
                    if isinstance(blk, dict):
                        bad2 = set(blk) - _PTR_AGENT_KEYS
                        if bad2:
                            errors.append(
                                f"{where}ptr_ret.ptr: unknown key(s) {sorted(bad2)}")
                        errors.extend(_sym_ptr_invariant_errors(
                            f"{where}ptr_ret.ptr", blk,
                            bool(entry["ptr_ret"].get("const")), is_ret=True,
                            arg_names=valid_args))
                    elif blk is not None:
                        errors.append(f"{where}ptr_ret.ptr: must be an object")

            # `lifetime`: SYMBOL-level, and only meaningful where there is a call
            # boundary to act on — a global's ownership has no acting method, and
            # a macro has no args at all.
            if has_lt and lifetime_f is not None:
                if is_global or ekind == "macro":
                    errors.append(
                        f"{where}lifetime: {name!r} is {ekind!r} — a lifecycle "
                        f"role is a property of a function/callback acting on an "
                        f"arg; a type's Drop/Clone is reverse-derived from those")
                elif not has_args:
                    errors.append(
                        f"{where}lifetime: {name!r} has no pointer args, so there "
                        f"is nothing for `for` to name")
                else:
                    errors.extend(_lifetime_errors(
                        f"{where}lifetime", lifetime_f,
                        _post_merge_arg_ptrs(args_f)))

        _check_ptr(f.get("ptr_args"), pr, "",
                   f.get("lifetime"), "lifetime" in f)

        # Forks (callbacks only): split the typedef by ownership cluster.
        forks = f.get("forks")
        if forks is not None:
            if entry.get("kind") != "callback":
                errors.append(
                    f"forks: only a callback may fork ({name!r} is {ekind!r})")
            elif not isinstance(forks, list):
                errors.append("forks: must be a list")
            else:
                # Full invoker set = the composed primary's, union whatever
                # prior forks already claimed. The composed primary carries the
                # WHOLE set (the subtraction that used to live on disk is
                # applied at read time now), so the union is belt-and-braces
                # for a store written by an older build.
                all_calls = set((entry.get("used_by") or {}).get("call") or [])
                for r in doc.get("symbols") or []:
                    if (r.get("name") == name
                            and (defined_in is None
                                 or r.get("defined_in") == defined_in)
                            and (r.get("variant") or 0) >= 1):
                        all_calls |= set(r.get("callsites") or [])
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
                               f"forks[{i}].",
                               fk.get("lifetime"), "lifetime" in fk)

        if errors:
            raise SystemExit(
                "--update REJECTED — fix and re-run:\n  - "
                + "\n  - ".join(errors))

        # Merge the primary into its store record (partial, idempotent): only
        # the slots/args mentioned. `_apply_ptr_agent` works on a full record,
        # so it runs against a scratch copy of the composed entry and only the
        # agent-owned result is kept.
        rec = _store.upsert_sym(doc, entry["name"], entry.get("defined_in"))
        scratch = json.loads(json.dumps(entry))
        _apply_ptr_agent(scratch, f.get("ptr_args"), pr, f, "lifetime" in f)
        _harvest_sym_agent(rec, scratch)
        # Globals: the singular `ptr` IS the ownership block (no composer keys
        # mixed in -- name/type/const live at the entry level), so it is
        # replaced wholesale. `locked_by` is one cohesive block, likewise
        # (null clears it).
        if gptr is not None:
            rec["ptr"] = gptr
        if "locked_by" in f:
            rec["locked_by"] = f["locked_by"]

        # Forks (idempotent replace): drop this symbol's prior fork records,
        # then store one per cluster. Only the fork's OWN judgement and its
        # callsites are stored -- the composer structure it inherits is applied
        # by `manifests._materialize_forks` at read time, so a fork can never
        # carry a frozen copy of a signature that has since changed.
        if forks is not None:
            key = (entry["name"], entry.get("defined_in") or "")
            doc["symbols"] = [
                r for r in doc["symbols"]
                if not ((r.get("name"), r.get("defined_in") or "") == key
                        and (r.get("variant") or 0) >= 1)]
            for i, fk in enumerate(forks, start=1):
                fr = _store.upsert_sym(doc, entry["name"],
                                       entry.get("defined_in"), variant=i)
                fscratch = json.loads(json.dumps(entry))
                _apply_ptr_agent(fscratch, fk.get("ptr_args"), fk.get("ptr_ret"),
                                 fk, "lifetime" in fk)
                _harvest_sym_agent(fr, fscratch)
                fr["callsites"] = sorted(fk.get("callsites") or [])
            holder["n"] = len(forks)

    _store.update(layout, _apply)

    n = holder["n"]
    tail = f" (+{n} fork{'s' if n != 1 else ''})" if n else ""
    print(f"updated {name} in {_store.path(layout)}{tail}")


def _harvest_sym_agent(rec: dict, full: dict) -> None:
    """Copy the agent-owned slots off a fully-shaped symbol record into its
    store record, dropping every composer key on the way.

    `_apply_ptr_agent` is written against the full record (it needs
    `ptr_args[i].name` / `position` to place a submission), so a submission is
    applied to a scratch copy of the composed entry and harvested here. Keeps
    one implementation of the merge rules rather than a second one that knows
    only the store shape."""
    from crustify import store as _store
    for k in ("lifetime",):
        if full.get(k) is not None:
            rec[k] = full[k]
    args = [{"name": a.get("name"), "ptr": a["ptr"]}
            for a in (full.get("ptr_args") or [])
            if isinstance(a, dict) and a.get("ptr") and a.get("name")]
    if args:
        rec["ptr_args"] = args
    ret = full.get("ptr_ret")
    if isinstance(ret, dict) and ret.get("ptr"):
        rec["ptr_ret"] = {"ptr": ret["ptr"]}
    if full.get("_comment_agent"):
        rec["_comment_agent"] = full["_comment_agent"]


def _records(target, kind, names, files, *, wrap_only=False,
             port_only=False) -> None:
    # record(s): always the whole record.
    from crustify import manifests as _m

    load = _load_type_entry if kind == "type" else _load_sym_entry
    recs: list = []
    for nm in names:
        layout, node, _bk = _resolve(target, kind=kind, name=nm, files=files,
                                     with_ops=False)
        entry = load(layout, target, node.id, node.defined_in)
        if entry is None:
            raise SystemExit(f"query {kind}: no manifest entry for {nm!r}.")
        # `_analysis.pending` is stamped scope-agnostically; under a scope
        # filter it must count the same fields `--fields` shows, or it reports
        # work the caller's scope never touches.
        if kind == "type" and (wrap_only or port_only):
            keep = _field_keep_set(layout, target, node.id, node.defined_in,
                                   wrap_only=wrap_only, port_only=port_only)
            entry = dict(entry)
            entry["_analysis"] = _m.analysis_state(
                entry, "types", entry.get("_analysis", {}).get("submitted", False),
                keep)
        recs.append(entry)
    print(json.dumps(recs[0] if len(recs) == 1 else recs, indent=2))


def _load_type_entry(layout, target, tag: str, defined_in: str | None) -> dict | None:
    """The raw ``types.json`` manifest entry for ``tag`` (preferring the one whose
    ``defined_in`` matches, to disambiguate a same-tag collision)."""
    def _find(pool: list) -> dict | None:
        fallback = None
        for e in pool:
            if (e.get("name") or e.get("type")) != tag:
                continue
            if defined_in and e.get("defined_in") == defined_in:
                return e
            fallback = fallback or e
        return fallback

    return (_find(_entries(layout, target, "types"))
            or _find(_universe_entries(layout, target, "types")))


def _resolve(target, *, kind: str, name: str, files: list[str] | None,
             with_ops: bool = True):
    """``(layout, node, by_key)`` for one type/symbol, resolved from the composed
    records (never the dag — see the note above). For a *type*, ``by_key`` is
    populated with the type's op nodes so :func:`dag.ordered_ops` can serve
    ``--ops`` — it selects them out of ``by_key`` by lifecycle name, the dag
    storing none. Raises ``SystemExit`` on miss/ambiguity.

    ``with_ops=False`` skips that: reverse-deriving the lifecycle needs the
    SYMBOL records as well as the type ones, so a plain record lookup was
    composing the whole symbol side to build an op list nothing then read."""
    from crustify import dag as D
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

    # existence + defined_in are composer-filled, so this walks the composed
    # records rather than a tree of files.
    arr, tagkey = (("types", "name") if kind == "type"
                   else ("symbols", "name"))

    def _scan(pool: list) -> dict:
        uniq: dict = {}                               # defined_in -> entry (dedup)
        for e in pool:
            if e.get(tagkey) != name:
                continue
            if file_set and e.get("defined_in") not in file_set:
                continue
            # Dedup by defining file; for a forked callback (several same-file
            # entries) prefer the primary (variant 0).
            df = e.get("defined_in")
            if df not in uniq or (e.get("variant") or 0) < (uniq[df].get("variant") or 0):
                uniq[df] = e
        return uniq

    uniq = _scan(_entries(layout, target, kind))
    if not uniq:
        # Out of the target's scope but in the CodeQL universe — readable, so
        # its ownership can be recorded. See :func:`_universe_entry`.
        uniq = _scan(_universe_entries(layout, target, kind))

    def _mk(e: dict):
        return D.Node(id=name, node_kind=kind, subkind=str(e.get("kind") or "symbol"),
                      defined_in=e.get("defined_in"),
                      layer=0, dep_types=[], dep_syms=[])

    node = _pick([_mk(e) for e in uniq.values()])
    by_key = {node.key: node}
    if kind == "type" and with_ops:
        from compose import scope
        # Method surface = the type's lifecycle, reverse-derived from the
        # symbols that carry the role — never a stored `ops` list.
        op_names = scope.type_method_syms(
            uniq.get(node.defined_in) or {},
            scope.build_lifecycle_index(_entry_pair(layout, target)))
        if op_names:
            sidx = _syms_index(layout, target)
            for nm in op_names:
                se = sidx.get(nm) or {}
                onode = D.Node(id=nm, node_kind="symbol",
                               subkind=str(se.get("kind") or "symbol"),
                               defined_in=se.get("defined_in"), layer=0,
                               dep_types=[], dep_syms=[])
                by_key[onode.key] = onode
    return layout, node, by_key


def _syms_index(layout, target) -> dict:
    """``name -> first symbol record`` for pre-dag op resolution."""
    idx: dict = {}
    for e in _entries(layout, target, "symbols"):
        idx.setdefault(e.get("name"), e)
    return idx


def _load_sym_entry(layout, target, name: str, defined_in: str | None) -> dict | None:
    """The symbol record for ``name`` (preferring the one whose ``defined_in``
    matches, to disambiguate same-named file-local statics)."""
    def _find(pool: list) -> dict | None:
        fallback = None
        for e in pool:
            if e.get("name") != name:
                continue
            if defined_in and e.get("defined_in") == defined_in:
                return e
            fallback = fallback or e
        return fallback

    return (_find(_entries(layout, target, "symbols"))
            or _find(_universe_entries(layout, target, "symbols")))


def _dag_loc(by_key, by_name, names, files, layer, as_json, keep=None,
             ops_of=None) -> None:
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
    # `ops_of` maps a type tag -> its lifecycle op NAMES, reverse-derived from
    # the analysis tree (`load_type_meta`). The dag stores no ops: it is a
    # deterministic artifact of the C, and a type's lifecycle is agent-submitted.
    ops_of = ops_of or {}
    op_names: set = set()
    for n in by_key.values():
        if n.node_kind == "type":
            op_names |= ops_of.get(n.id) or set()

    def is_folded_op(n) -> bool:
        return n.node_kind == "symbol" and n.id in op_names

    def nops(n) -> int:
        # A type's method surface is exactly its reverse-derived lifecycle;
        # there is no second op list to union in.
        return len(ops_of.get(n.id) or ())

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
        from crustify import dag as D
        file_set = set(files or [])
        D.require_unambiguous(names, by_key, by_name, file_set,
                              stage="query dag --loc")
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
    (`Node.origin()`), so dag nodes and scope entries collide on the same key."""
    if not (wrap_only or port_only):
        return None
    from compose import scope as _sc
    from crustify import scope as _scope_mod
    sj = _scope_mod.try_build(layout, target)
    keys = _sc.scope_membership(sj, "port" if port_only else "wrap") if sj is not None else set()

    def keep(n) -> bool:
        return _sc.origin_key(n.id, n.defined_in, None) in keys
    return keep


def _group_of(n) -> str:
    """Which output group a dag node belongs to (see :func:`query_dag`)."""
    if n.node_kind == "type":
        return "types"
    sk = n.subkind or ""
    if sk == "callback":
        return "callbacks"
    if sk.startswith("macro"):
        return "macros"
    if sk.startswith("global"):
        return "globals"
    return "functions"          # function_*, plus bare/external symbols


def query_dag(
    target: Path,
    *,
    names: list[str] | None = None,
    files: list[str] | None = None,
    depth: int | None = None,
    scc: str | None = None,
    layer: int | None = None,
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

    from crustify import dag as D
    from crustify.layout import Layout

    layout = Layout.discover(target)
    dag = D.build(layout, target, stage="query dag")
    by_key, by_name = D.load_nodes(dag)
    keep = _scope_predicate(layout, target, wrap_only, port_only)

    # ── mode: LoC view ─────────────────────────────────────────────────
    if loc:
        ops_of = {t: lc for t, (_f, lc)
                  in D.load_type_meta(_entry_pair(layout, target)).items()}
        _dag_loc(by_key, by_name, names, files, layer, False, keep, ops_of)
        return

    def _emit(rows: list) -> None:
        """rows: list[(node, depth|None)] -> JSON grouped by what the caller
        does with each kind.

        The groups ARE the routing: `types` go to the type wrapper, `callbacks`
        and `functions` to the symbol wrapper, `macros` to nobody -- bindgen
        owns their `-sys` shims, so no stage facades them. They stay a group
        rather than being dropped so a caller can see the closure is complete;
        every wrap/port consumer just ignores the key.

        `layer` and `depth` are emitted unconditionally because they exist
        NOWHERE else: a types.json / syms.json record carries neither, so
        piping an id into `query types` cannot recover them. Everything else a
        caller might want (fields, ops, ownership, casts) IS in those records,
        which is why this view stays thin -- `id` + `defined_in` is enough to
        look one up unambiguously, including a file-local static whose name
        repeats across TUs.
        """
        rows.sort(key=lambda r: (r[0].layer, r[0].id) if r[1] is None
                  else (r[1], r[0].layer, r[0].id))
        out: dict[str, list] = {k: [] for k in
                                ("types", "callbacks", "functions",
                                 "globals", "macros")}
        seen: set[str] = set()
        for n, d in rows:
            if n.id in seen:
                continue
            seen.add(n.id)
            # `defined_in` already falls back to declared_in[0] upstream: the
            # dag composer fills it when the manifest entry has no definition
            # site (239 such entries here — the DEFINE_STACK_OF instances and
            # friends). What stays null is only `external` / `builtin`, which
            # has no manifest entry to fall back to.
            rec = {"id": n.id, "layer": n.layer, "defined_in": n.defined_in}
            if d is not None:
                rec["depth"] = d
            out[_group_of(n)].append(rec)
        print(json.dumps({k: v for k, v in out.items() if v}, indent=2))

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
    D.require_unambiguous(names, by_key, by_name, file_set, stage="query dag")
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
        twins: list = []
        for k in start:
            twins.extend(getattr(by_key[k], attr))
        rows = []
        for dk in dict.fromkeys(twins):             # dedup, preserve order
            n = by_key.get(dk)
            if n is not None and n.node_kind == "type" and (keep is None or keep(n)):
                rows.append((n, None))
        _emit(rows)
        return

    # ── mode: transitive dependency closure (BFS) ──────────────────────
    def _dep_keys(n) -> list:
        # Both sides are `(name, defined_in)` node keys, so a dep resolves by
        # lookup. No name-scan: a TU-local type used to fan out to every
        # same-tagged node in the tree, pulling unrelated TUs into the closure.
        return [dk for dk in (*n.dep_types, *n.dep_syms) if dk in by_key]

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

    from crustify import scope as _scope_mod
    layout = Layout.discover(target)
    doc = _scope_mod.build(layout, target, stage="query files")

    port_files = wrap_files = None
    if port_only or not wrap_only:
        port_files = sorted(scope_mod.load_port_paths(doc))
    if wrap_only or not port_only:
        wrap_files = sorted(set((doc.get("wrap") or {}).get("files") or []))

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

