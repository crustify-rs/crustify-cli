"""Compose the symbol records, grouped by source stem.

Each stem group lists every symbol (function, macro, global, or callback — a
function-pointer typedef) defined or declared in its file(s).

**Scope gates EMISSION, never CONTENT.** Which records are emitted (and
how they're tagged / post-filtered by `--target-only` / `--import-only`) is a
scope decision; every record that IS emitted carries its full
codebase-wide composition. This mirrors the type manifest, whose
`opaque_in` / `non_opaque_in` footprints are COMPLETE for port AND wrap.

One entry shape:

  - **Base**: name, kind, declared_in, defined_in, type, ptr_args,
    ptr_ret.
  - **Plus, for every emitted record**: macros add `used_by` (call
    sites); functions add `used_by` (call + ref reach) and
    `depends_on` (flat `syms` list with `{name, defined_in,
    declared_in}` records + `[{type, fields: []}]` types list);
    globals add `used_by` (accessors). Macro `body` is never emitted —
    the agent reads the expansion from source.

The reach relations are scope-agnostic raw lookups and the
field-access index is repo-wide, so full composition costs a few dict
hits per record; the total stays bounded by what scope admits.

A **callback** is signature-shaped and fully built by the composer
regardless of scope (CodeQL identifies it deterministically — a
typedef whose unwrap chain reaches a `RoutineType`): it carries
`ptr_args`/`ptr_ret`, `used_by.{call,ref}`, and a signature
`depends_on`, with `defined_in: null`. The agent fills only its
per-arg/return ownership.

Per-source-file partitioning rule lives in `path_partition.py`.
The merge primitive (`manifest_merge.merge_manifest_file`) handles
cross-target evolution via field-level union — a wrap entry that
gains target-section additions in a later target invocation gets the new
fields added without overwriting any composer-filled or
agent-annotated fields already present.

Wrap-side kind exclusion: `function_static`, `function_inline_tu`,
and `global_static` are TU-bounded and cannot appear in wrap output;
the composer filters them when emitting wrap entries.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import scope
from .filter_spec import FilterSpec, is_seed
from .manifest_merge import symbol_key
from .path_partition import manifest_dir_for
from .reach import Reach


# Kinds disallowed in wrap manifests by C semantics — TU-bounded.
_WRAP_DISALLOWED_FN_KINDS = {"function_static", "function_inline_tu"}
_WRAP_DISALLOWED_GLOBAL_KINDS = {"global_static"}


def _decls_list(decl_files_pipe: str) -> list[str]:
    return scope.parse_decl_files(decl_files_pipe)


def _is_scalar_typedef(type_name: str, by_name: dict[str, dict]) -> bool:
    """Drop typedefs whose chain terminates at a primitive scalar
    (`size_t`, `uint8_t`, `time_t`, …) from `depends_on.types` —
    bindgen handles them as value aliases.
    """
    row = by_name.get(type_name)
    if row is None or row["kind"] != "typedef":
        return False
    terminal = scope.resolve_typedef(type_name, by_name)
    if terminal is None:
        return False
    if terminal["kind"] != "typedef":
        return False
    return terminal.get("unaliased_kind", "") == "primitive"


def _is_callback_typedef(type_name: str, by_name: dict[str, dict]) -> bool:
    """True for a function-pointer typedef (`unaliased_kind == "callback"`,
    stamped deterministically by `entities/types.ql`).

    A callback is a SIGNATURE, not an aggregate: it carries `ptr_args`/
    `ptr_ret` and is emitted as its own `kind:"callback"` symbol entry. So a
    consumer's dependency on one belongs in `depends_on.syms`, alongside its
    callees — NOT in `depends_on.types`, which is the aggregate-layout surface.
    `_callback_deps` routes it there.

    Walks the alias chain (same shape as `_is_scalar_typedef`), because
    `unaliased_kind` is stamped only on the row that ENDS the chain: for
    `typedef int (*SSL_verify_cb)(...)` the base reaches no named `UserType`,
    so `aliases` is "" and the kind lands as "callback"; but for a chained
    `typedef SSL_verify_cb my_cb;` the base IS a named `UserType`, so `my_cb`
    carries `aliases="SSL_verify_cb"` and an EMPTY `unaliased_kind`. A
    direct-row check would miss `my_cb`. The CSV layer already treats both as
    callbacks — `callback_call_sites.ql` and `callback_signature_type_uses.ql`
    both use a `routineOf` that walks `TypedefType` — so `compose` gates
    callback enumeration on this same predicate, keeping filter and
    enumeration in agreement: every tag dropped from `.types` here has a
    callback entry to point at.
    """
    row = by_name.get(type_name)
    if row is None or row["kind"] != "typedef":
        return False
    terminal = scope.resolve_typedef(type_name, by_name)
    if terminal is None or terminal["kind"] != "typedef":
        return False
    return terminal.get("unaliased_kind", "") == "callback"


def _callback_deps(
    reach: Reach,
    name: str,
    def_file: str,
    by_name: dict[str, dict],
    *,
    sig_types: list[tuple[str, str, str, str]] | None = None,
    invoked: bool = True,
) -> list[dict[str, Any]]:
    """The callbacks this entity depends on, as `depends_on.syms` records
    (`{name, defined_in, declared_in}`), sorted like `_compose_dep_syms`.

    Two sources, because a consumer reaches a callback two ways:

      1. **Named in the signature** -- from `signature_type_uses.csv`. This
         cannot come from `ptr_args`: `edges/function_pointer_args.ql` renders
         a function-pointer parameter's pointee as the synthetic marker
         `"(routine)"`, erasing which typedef it was. `signature_type_uses`
         keeps the identity (a `TypedefType` is a `UserType`, so
         `reachableUserType` binds it directly).
      2. **Invoked** -- from `callback_call_sites.csv`, via
         `reach.callbacks_invoked_by`. An indirect call through a function
         pointer IS a call, so it belongs in `depends_on.syms` beside the
         direct callees. This is the only source for a callback reached
         through a STRUCT FIELD (`s->psk_server_callback(...)`): the invoker
         names the typedef nowhere in its own record, and the owning struct's
         `fields[]` is only the target-accessed subset. Pass `invoked=False` for
         a callback's own entry -- a typedef invokes nothing.

    `defined_in` is always null -- a callback is a header typedef with no
    definition site -- so consumers key it by its canonical declaration,
    exactly as they do for any other declaration-only symbol.
    """
    if sig_types is None:
        sig_types = reach.types_in_signature_of(name, def_file)
    names = {t for t, _k, _f, _p in sig_types if _is_callback_typedef(t, by_name)}
    if invoked:
        names |= reach.callbacks_invoked_by(name, def_file)
    out: list[dict[str, Any]] = []
    for cb in sorted(names):
        row = by_name.get(cb) or {}
        out.append({
            "name": cb,
            "defined_in": None,
            "declared_in": sorted(_decls_list(row.get("decl_files", ""))),
        })
    return out


def _resolve_dep_type_tag(
    type_name: str,
    by_name: dict[str, dict],
) -> str:
    """Resolve a signature type tag to the canonical struct/union/enum
    tag when possible; typedef aliases collapse to the terminal
    aggregate's name so consumer lookups match the type manifest.
    """
    row = by_name.get(type_name)
    if row is None or row["kind"] != "typedef":
        return type_name
    terminal = scope.resolve_typedef(type_name, by_name)
    if terminal is not None and terminal["kind"] in {"struct", "union", "enum"}:
        tag = terminal.get("name", "")
        if tag and not tag.startswith("("):
            return tag
    return type_name


class FieldAccessIndex:
    """Body field-accesses per enclosing function, disambiguated by the
    enclosing function's file.

    File-local statics that share a name (e.g. `normalize_options` in
    odb.c vs blame.c vs describe.c) must NOT pool their accessed structs —
    a name-only key conflates them and leaks unrelated types into every
    same-named function's `depends_on.types`. `multidef` is the set of
    names defined in >1 file; for those we look up by `(name, def_file)`
    (a static's body accesses occur in its own def_file). For single-def
    names — including inline-header functions, whose accesses are recorded
    at each includer rather than the header — we fall back to the
    name-union so nothing is dropped.
    """

    def __init__(self, by_site: dict[tuple[str, str], dict[str, list[str]]],
                 multidef: set[str]) -> None:
        self._by_site = by_site
        self._multidef = set(multidef)
        # Order-preserving name-union for single-def lookups.
        self._by_name: dict[str, dict[str, list[str]]] = {}
        for (n, _af), structs in by_site.items():
            agg = self._by_name.setdefault(n, {})
            for s, fields in structs.items():
                m = agg.setdefault(s, [])
                for fld in fields:
                    if fld not in m:
                        m.append(fld)

    def body_accesses(self, name: str, def_file: str) -> dict[str, list[str]]:
        if name in self._multidef:
            return self._by_site.get((name, def_file), {})
        return self._by_name.get(name, {})


def _load_field_access_index(
    csv_dir_t2: Path,
    multidef: set[str],
) -> "FieldAccessIndex":
    """Build a `FieldAccessIndex` keyed by `(enclosing_name, access_file)`
    from `t2/field_accesses.csv`.

    Iteration order is preserved (insertion-ordered dicts): the first
    time the CSV mentions a (function, struct) pair fixes that struct's
    position in the outer dict, and the first access of each field
    fixes that field's position in the inner list. Because the CodeQL
    query emits rows in source-position order (sorted by access_line),
    this yields natural body-order traversal for the consumer with no
    sort step.

    Anonymous declaring types (`struct_name == ""` or starting with
    `(unnamed`) are skipped — they have no stable identity downstream
    consumers can reference, and field accesses on them are surfaced
    under their named outer struct by the underlying CodeQL query
    when cpp-all flattens anonymous inner types.

    Returns an empty dict when the CSV is absent (e.g. a target with
    no completed `analyze extract-ql` run).
    """
    import csv as _csv
    # `fa_with_root.csv` when present: it re-keys an access through an
    # ANONYMOUS embedded member (`s->ext.hostname`) onto the outermost NAMED
    # container and supplies the dotted `field_path`, so those accesses land in
    # `depends_on.types[].fields` under the same qualified name
    # `entities/fields.ql` puts in the parent's `fields[]`. With the flat CSV
    # they carried `struct_name = "(unnamed …)"` and were dropped by the filter
    # below. Falls back to the flat CSV for a pre-existing extraction.
    path = csv_dir_t2 / "fa_with_root.csv"
    rooted = path.is_file()
    if not rooted:
        path = csv_dir_t2 / "field_accesses.csv"
    if not path.exists():
        return FieldAccessIndex({}, multidef)
    by_site: dict[tuple[str, str], dict[str, list[str]]] = {}
    with open(path, newline="") as f:
        for row in _csv.DictReader(f):
            enclosing = row.get("enclosing_name", "")
            access_file = row.get("access_file", "")
            if rooted and row.get("root_struct_name"):
                struct = row["root_struct_name"]
                field = row.get("field_path") or row.get("field_name", "")
            else:
                struct = row.get("struct_name", "")
                field = row.get("field_name", "")
            if not enclosing or not struct or struct.startswith("("):
                continue
            structs = by_site.setdefault((enclosing, access_file), {})
            fields = structs.setdefault(struct, [])
            if field and field not in fields:
                fields.append(field)
    return FieldAccessIndex(by_site, multidef)


def _compose_dep_types(
    reach: Reach,
    name: str,
    def_file: str,
    by_name: dict[str, dict],
    field_access_index: "FieldAccessIndex | None",
    *,
    sig_types: list[tuple[str, str, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build the flat `[{type, fields}]` list combining this function's
    signature type uses with the struct fields its body actually
    touches (per `t2/field_accesses.csv`).

    Three sources, unioned without sorting:

      1. **Signature types** — from `reach.types_in_signature_of(...)`,
         iterated in signature parameter order. Each emitted with its
         body-access fields list (or `[]` if the body doesn't touch any
         field of the type — opaque-use case).
      2. **Body field-accessed types** — structs the body reaches into
         (per `field_accesses.csv`) that don't appear in the signature
         (e.g. a `void *baton` cast then field access, or a global of
         struct type). Iterated in CSV first-encounter order.
      3. **Body-level named types** — user types the body names via local
         var / cast / sizeof (`local_type_uses.csv`) without reaching into
         a field. The Rust port must still name these, so they're real
         deps; emitted with an empty fields list. Empty for callbacks.

    Function-pointer typedefs are excluded from all three sources: a callback
    is signature-shaped, not aggregate-shaped, so it is emitted as a
    `depends_on.syms` record by `_callback_deps` instead. Struct fields of
    function-pointer type never reach here anyway — the field parser renders
    them as the synthetic `..(*)(..)`, not as the typedef tag.

    No final sort — first-encounter order is preserved end-to-end so
    consumers see a deterministic, source-faithful traversal with no
    additional processing.
    """
    body_accesses = (
        field_access_index.body_accesses(name, def_file)
        if field_access_index is not None else {}
    )

    if sig_types is None:
        sig_types = reach.types_in_signature_of(name, def_file)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for type_name, _type_kind, _type_def_file, _pos in sig_types:
        if _is_scalar_typedef(type_name, by_name):
            continue
        if _is_callback_typedef(type_name, by_name):
            continue                       # → depends_on.syms
        tag = _resolve_dep_type_tag(type_name, by_name)
        if not tag or tag.startswith("(") or tag in seen:
            continue
        seen.add(tag)
        out.append({
            "type": tag,
            "fields": list(body_accesses.get(tag, [])),
        })

    # Body-touched types not present in the signature, in CSV row
    # order (anonymous tags already filtered at index load time).
    for struct, fields in body_accesses.items():
        if struct in seen:
            continue
        seen.add(struct)
        out.append({"type": struct, "fields": list(fields)})

    # Body-level user types named via local var / cast / sizeof
    # (`local_type_uses.csv`) but NOT reached through a field — e.g. a cast
    # target, a local of a wrap struct, a `sizeof(T)`. The Rust port must name
    # these too, so they belong in `depends_on` alongside signature + field
    # types. No field list (not a field reach-in). Empty for callbacks (no
    # body), so the callback reuse path is unaffected.
    for type_name, _kind, _tdf, _use in reach.types_in_body_of(name, def_file):
        if _is_scalar_typedef(type_name, by_name):
            continue
        if _is_callback_typedef(type_name, by_name):
            continue                       # → depends_on.syms
        tag = _resolve_dep_type_tag(type_name, by_name)
        if not tag or tag.startswith("(") or tag in seen:
            continue
        seen.add(tag)
        out.append({"type": tag, "fields": []})

    return out


def _compose_dep_syms(
    forward_syms: set[tuple[str, str]],
    sym_index: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the flat `[{name, defined_in, declared_in}]` callee list.
    `sym_index` is `(name, def_file) → row` over T1
    functions/globals/macros, used to retrieve `decl_files`.
    Functions and globals carry `decl_files`; macros don't (their
    declaration site IS their def_file), so for macro rows we
    fall back to `[def_file]` as the declared_in list.
    """
    out: list[dict[str, Any]] = []
    for name, def_file in sorted(forward_syms):
        row = sym_index.get((name, def_file))
        if row is None:
            decls: list[str] = []
        elif "decl_files" in row:
            decls = sorted(_decls_list(row["decl_files"]))
        else:
            # Macro row — declared at def_file.
            decls = [def_file] if def_file else []
        out.append({
            "name": name,
            "defined_in": def_file or None,
            "declared_in": decls,
        })
    return out


def _null_ptr_agent():
    """The agent-fillable ownership slot on a `ptr_args[*]` / `ptr_ret` record,
    emitted `null` by the composer.

    `null` is the UNANALYZED state, so "has this pointer been through the
    wrapper?" is a null check on one key -- an all-null keyed skeleton would be
    indistinguishable from a block the agent filled with nulls, and is not
    submittable anyway (a submitted block replaces the prior WHOLESALE and must
    be complete, so the agent never patches individual keys into a skeleton).

    Once filled it is `{scalar, array, string, owned, borrowed, nullable,
    mutable, note}` -- the SAME structured sub-object a struct field's `ptr`
    carries (see types_manifest._null_ptr_skeleton), so a pointer is described
    identically whether it sits in a struct or crosses a call boundary. The
    composer's structural keys (position/name/type/const/depth) stay OUTSIDE it,
    which is what lets the whole block be replaced. The lifecycle-primitive role
    a method plays is NOT here but on the symbol entry's top-level `lifetime`
    (see `_null_lifetime`), which names its subject arg."""
    return None


def _null_lifetime():
    """The entry-level `lifetime` slot on a call-boundary symbol (function /
    callback), emitted `null` by the composer.

    A lifecycle primitive is a property of the SYMBOL, not of one of its pointer
    records: `SSL_free` IS a dropper, and the arg it drops is named by
    `lifetime.for` -- by NAME, the same way a borrowed `ptr` names its source
    (`arg:<name>`), so both arg-dependent facts use one vocabulary. Null means
    "no lifecycle role" and is also the unprocessed state; a filled block is
    `{for, is_dropper, is_disposer, is_cloner}` and must assert at least one
    role. Globals and macros have no call boundary and carry no such slot."""
    return None


def _compose_ptr_args(reach: Reach, fn_name: str, fn_def_file: str) -> list[dict]:
    out: list[dict] = []
    for arg in reach.ptr_args_of(fn_name, fn_def_file):
        out.append({**arg, "ptr": _null_ptr_agent()})
    return out


def _compose_ptr_ret(reach: Reach, fn_name: str, fn_def_file: str) -> dict | None:
    ret = reach.ptr_ret_of(fn_name, fn_def_file)
    if ret is None:
        return None
    return {**ret, "ptr": _null_ptr_agent()}


# ------------------------------------------------------------------ per-kind composers


def _base_function(row: dict, reach: Reach) -> dict[str, Any]:
    name = row["name"]
    def_file = row["def_file"]
    return {
        "name": name,
        "kind": row["linkage"],
        "declared_in": sorted(_decls_list(row["decl_files"])),
        "defined_in": def_file or None,
        "type": row["signature"],
        # Composer-filled: does this function take a trailing `...`? Terminal —
        # a CodeQL fact off the declaration, never agent-edited. The `signature`
        # string does not reliably show it. Absent column (a pre-`is_variadic`
        # extraction) reads as false.
        "is_variadic": str(row.get("is_variadic") or "0") == "1",
        "ptr_args": _compose_ptr_args(reach, name, def_file),
        "ptr_ret": _compose_ptr_ret(reach, name, def_file),
        # Agent-filled lifecycle role of the WHOLE symbol, naming its subject arg
        # in `for` (see _null_lifetime). Null until a wrapper sets it.
        "lifetime": _null_lifetime(),
        # Body line span from functions.csv (0 when the column is absent — a
        # pre-`loc` extraction — or for a declaration-only extern). Drives the
        # port bin-packer's lines-of-code budget.
        "loc": int(row.get("loc") or 0),
    }


def _port_additions_function(
    row: dict,
    reach: Reach,
    by_name: dict[str, dict],
    sym_index: dict[tuple[str, str], dict[str, Any]],
    field_access_index: "FieldAccessIndex",
) -> dict[str, Any]:
    name = row["name"]
    def_file = row["def_file"]
    callers = reach.all_callers_of(name, def_file)
    refs = reach.all_referrers_of(name, def_file) - callers

    forward_syms: set[tuple[str, str]] = set()
    forward_syms |= reach.callees_of(name, def_file)
    forward_syms |= reach.addr_targets_of(name, def_file)
    forward_syms |= reach.macros_expanded_by(name, def_file)
    for gname, gdef, _kind in reach.globals_used_by(name, def_file):
        forward_syms.add((gname, gdef))

    return {
        "used_by": {
            "call": sorted(callers),
            "ref": sorted(refs),
        },
        "depends_on": {
            # Callees/refs, then the callback typedefs this function's
            # signature names (a callback is signature-shaped — it belongs
            # with the syms, not in `.types`).
            "syms": (
                _compose_dep_syms(forward_syms, sym_index)
                + _callback_deps(reach, name, def_file, by_name)
            ),
            "types": _compose_dep_types(
                reach, name, def_file, by_name, field_access_index,
            ),
        },
    }


def _base_global(row: dict) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": row["linkage"],
        "declared_in": sorted(_decls_list(row["decl_files"])),
        "defined_in": row["def_file"] or None,
        "type": row["type"],
        # Agent-filled: `ptr` iff the global is itself a pointer (same ownership
        # block as a ptr_args record); `locked_by` iff it is accessed under a lock.
        "ptr": None,
        "locked_by": None,
        "loc": 1,  # a global counts as 1 LoC for the port batch budget
    }


def _port_additions_global(
    row: dict,
    reach: Reach,
    by_name: dict[str, dict],
    sym_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Global additions. Forward deps come from two places:

      - **its declared type** (`global_type_uses.csv`) — a
        `const version_info tls_version_table[]` depends on `version_info`.
      - **its initializer** (`function_addresses.csv` / `global_accesses.csv`)
        — a file-scope dispatch table takes the address of every handler it
        names. Recovering this is why `enclosingNameOf` in those two queries
        falls back to the initialised VARIABLE: a file-scope access has no
        enclosing function, so it used to be attributed to "" and dropped,
        leaving every table (`ext_defs` and its 129 handlers) a false leaf.

    Both were previously hardcoded empty even though the type index existed.
    """
    name = row["name"]
    def_file = row["def_file"]
    accessors = reach.all_accessors_of(name, def_file)

    forward: set[tuple[str, str]] = set()
    forward |= reach.addr_targets_of(name, def_file)
    for gname, gdef, _kind in reach.globals_used_by(name, def_file):
        if (gname, gdef) != (name, def_file):        # no self-edge
            forward.add((gname, gdef))
    forward = {(n, df or "") for n, df in forward}

    dep_types: list[dict[str, Any]] = []
    seen: set[str] = set()
    for type_name, _kind, _tdf in reach.types_in_global_type(name, def_file):
        if _is_scalar_typedef(type_name, by_name):
            continue
        if _is_callback_typedef(type_name, by_name):
            continue                                  # → depends_on.syms
        tag = _resolve_dep_type_tag(type_name, by_name)
        if not tag or tag.startswith("(") or tag in seen:
            continue
        seen.add(tag)
        dep_types.append({"type": tag, "fields": []})

    cb_types = [
        t for t, _k, _f in reach.types_in_global_type(name, def_file)
        if _is_callback_typedef(t, by_name)
    ]
    dep_syms = _compose_dep_syms(forward, sym_index)
    for cb in sorted(set(cb_types)):
        r = by_name.get(cb) or {}
        dep_syms.append({
            "name": cb,
            "defined_in": None,
            "declared_in": sorted(_decls_list(r.get("decl_files", ""))),
        })
    return {
        "used_by": {"call": None, "ref": sorted(accessors)},
        "depends_on": {"syms": dep_syms, "types": dep_types},
    }


def _base_macro(row: dict) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": "macro",  # deterministic + terminal, and the ONLY classification
                          # a macro carries -- what it expands to is read from
                          # the source at `defined_in`, never stored.
        "declared_in": [row["def_file"]] if row["def_file"] else [],
        "defined_in": row["def_file"] or None,
        "type": None,
        "loc": 0,  # macros aren't ported → 0 LoC for the port batch budget
    }


def _port_additions_macro(row: dict, reach: Reach) -> dict[str, Any]:
    """Port macro additions. The macro `body` is intentionally NOT
    emitted; the agent reads the expansion from source when needed.
    """
    name = row["name"]
    def_file = row["def_file"]
    fn_sites = reach.all_macro_call_sites(name, def_file)
    file_sites = reach.all_macro_file_sites(name, def_file)
    return {
        "used_by": {
            "call": sorted(fn_sites | file_sites),
            "ref": [],
        },
        "depends_on": {"syms": [], "types": []},
    }


def _base_callback(
    row: dict, reach: Reach, by_name: dict[str, dict],
) -> dict[str, Any]:
    """Symbol-shaped entry for a function-pointer typedef (``kind:"callback"``).

    A callback IS a signature, not a struct — CodeQL identifies it
    deterministically (`types.ql` stamps `unaliased_kind = "callback"` on any
    typedef whose unwrap chain reaches a `RoutineType`), so the composer emits
    it like any other symbol and the agent only fills the per-arg/return
    ownership. `ptr_args`/`ptr_ret`/`depends_on` come from the same
    function-pointer signature CSVs the function composer uses (a callback is a
    subject there); `used_by.{call,ref}` from the function-side inverse +
    callsites. Its `ptr` slots are left `null` like any other symbol's — a
    `const` pointee does pin `mutable` to false, but pre-seeding that would make
    the slot non-null and so indistinguishable from an analyzed one; the
    const⟹`mutable != true` implication is enforced at `--update` instead.

    A callback has no def_file (a header typedef) and is never ported to native
    Rust (it becomes an `extern "C" fn` type), so it is always wrap-shape;
    inclusion is the import-reach gate (`_import_reachable`) in `compose`.
    """
    name = row["name"]
    decl_file = _first_decl(row["decl_files"])

    ptr_args = _compose_ptr_args(reach, name, decl_file)
    ret = _compose_ptr_ret(reach, name, decl_file)

    # call = invokers (indirect-call sites — the contract-bearing set); ref =
    # the remaining signature declarers that only forward/store the pointer.
    # Disjoint (call wins), mirroring the function schema's call/ref split.
    callsites = reach.callback_callsites_of(name, decl_file)
    declarers = {fn for fn, _ in reach.functions_using_type(name, "")}
    cb_sig_types = reach.callback_sig_types_of(name, decl_file)
    dep_types = _compose_dep_types(
        reach, name, decl_file, by_name, None, sig_types=cb_sig_types,
    )
    # A callback whose own signature takes another callback depends on it the
    # same way any consumer does — as a sym, not a type.
    dep_syms = _callback_deps(
        reach, name, decl_file, by_name, sig_types=cb_sig_types, invoked=False,
    )
    return {
        "name": name,
        "kind": "callback",
        "declared_in": sorted(_decls_list(row["decl_files"])),
        "defined_in": None,
        # No C signature string is extracted for a callback; the structured
        # ptr_args/ptr_ret carry the signature shape.
        "type": None,
        "ptr_args": ptr_args,
        "ptr_ret": ret,
        # An invoked callback can itself be a lifecycle primitive (a `free_func`
        # typedef drops its arg), so it carries the same entry-level slot; a fork
        # may override it when invokers realize different contracts.
        "lifetime": _null_lifetime(),
        "used_by": {"call": sorted(callsites), "ref": sorted(declarers - callsites)},
        "depends_on": {"syms": dep_syms, "types": dep_types},
    }


# ------------------------------------------------------------------ orchestration

def _forward_syms_of(
    name: str, def_file: str, reach: Reach,
) -> set[tuple[str, str]]:
    """Return the one-hop forward symbol set of a target symbol.

    Union of every direct edge type CodeQL captures:

      - function calls (`function_calls.csv`)
      - function address references (`function_addresses.csv`)
      - macro expansions (`macro_expansions.csv`)
      - global accesses (`global_accesses.csv`)

    NB: function calls coming from inside expanded macros are
    flattened by CodeQL into the enclosing function's call edges, so
    this set captures everything the symbol "touches" regardless of
    whether the touch was written directly in source or came via a
    macro expansion. See `docs/TODO.md` §2026-06-04 for the
    consequences.

    Returned tuples match `entry_key`-shape: `(name, def_file or "")`.
    """
    fwd: set[tuple[str, str]] = set()
    fwd |= reach.callees_of(name, def_file)
    fwd |= reach.addr_targets_of(name, def_file)
    fwd |= reach.macros_expanded_by(name, def_file)
    for gname, gdef, _kind in reach.globals_used_by(name, def_file):
        fwd.add((gname, gdef))
    # Normalise to entry_key-shape: empty-string def_file when null.
    return {(n, df or "") for n, df in fwd}


def compose(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    filter_spec: FilterSpec | None = None,
) -> tuple[dict[Path, list[dict[str, Any]]], dict[Path, str], dict[tuple[str, str], list[str]]]:
    """Build the per-manifest-dir syms.json entries plus a per-dir
    scope tag.

    Returns ``(entries_by_dir, dir_scope)``:

      - ``entries_by_dir``: ``{manifest_dir: [entries]}`` keyed by
        repo-relative path (e.g. `Path("ssl/statem/statem")`). The
        caller writes one `syms.json` per dir under the analysis
        tree root.
      - ``dir_scope``: ``{manifest_dir: TARGET | IMPORT}`` parallel
        map. Orchestrator consumers (e.g. the symbol wrapper's
        manifests-list input contract) read this to tag each
        manifest with its scope without persisting the tag to disk.
        Assumes a stem-group manifest dir carries entries of a
        single scope only; the mixed-scope case (a stem-group split
        by `config.out_of_scope.paths`) is tracked in docs/TODO.md.

    Behaviour modes (driven by `filter_spec`):

      - **No scope.json** (`filter_spec.scope_json_path is None`):
        no port/wrap distinction is made. Every entry emits as
        wrap-shape (base only). No closure expansion. Seeds in
        seed mode are emitted as base-shape regardless. This is
        the "raw symbol inventory" mode for naïve queries.
      - **Scope.json + seed mode** (`scope_json_path` set AND any
        of `dirs`/`files`/`names` set): entries matching the seed
        predicate become seeds. From target seeds, the composer
        computes a one-hop forward closure and adds touched symbols
        to the output. **Seed admission gate**: an import seed
        is admitted only if it is wrap-reachable from port code per
        the scope.json. Port-scope seeds emit with port additions;
        wrap seeds + closure emit as base shape.
      - **Scope.json + filter mode** (`scope_json_path` set, no
        seed flags): emit every reachable entry. Port-scope entries
        emit with port additions; import entries emit as base.

    `--target-only` / `--import-only` post-filters apply in every mode
    (they're emitted-shape filters).
    """
    if filter_spec is None:
        filter_spec = FilterSpec()
    # Unscoped (repo-wide): still classify port/wrap via scope.json, but never
    # DROP an out-of-scope entry. The default keeps the reachability gate.
    unscoped = filter_spec.unscoped
    target_paths = (
        scope.load_target_paths(filter_spec.scope_json_path)
        if filter_spec.scope_json_path is not None
        else set()
    )
    scope_enabled = bool(target_paths) or filter_spec.scope_json_path is not None

    # v2: port/wrap classification is precomputed in scope.json's entity
    # sets (including the header-macro carve-out). Look up (name, def_file)
    # membership instead of re-running classify()/entry_scope() per row.
    # target_paths (the file list) is retained for Reach (file-level
    # port-reachability).
    _sj = filter_spec.scope_json_path
    tgt_funcs = scope.load_entities(_sj, scope.TARGET, "functions") if _sj else set()
    tgt_globals = scope.load_entities(_sj, scope.TARGET, "globals") if _sj else set()
    tgt_macros = scope.load_entities(_sj, scope.TARGET, "macros") if _sj else set()
    # The composed IMPORT surface, as an admission floor — the same role
    # `_in_import_surface` plays on the type side. The reach gates below ask
    # "does target code reach this", which has no answer for a WRAP campaign
    # (`files.import`): there is no target code, and the surface was seeded off
    # the named headers instead. A scope.json entry with no manifest record is
    # unschedulable, so whatever the section lists gets one.
    imp_keys = (
        (scope.load_entities(_sj, scope.IMPORT, "functions")
         | scope.load_entities(_sj, scope.IMPORT, "globals")
         | scope.load_entities(_sj, scope.IMPORT, "macros"))
        if _sj else set())

    def _in_import(r: dict) -> bool:
        return (r.get("name"), r.get("def_file") or "") in imp_keys

    funcs = scope.load_csv(csv_dir_t1 / "functions.csv")
    macros = scope.load_csv(csv_dir_t1 / "macros.csv")
    globals_ = scope.load_csv(csv_dir_t1 / "globals.csv")
    types = scope.load_csv(csv_dir_t1 / "types.csv")
    by_name = scope.build_types_index(types)
    reach = Reach(csv_dir_t2, target_paths)
    # Names defined in >1 file are file-local statics reusing a name; their
    # body field-accesses must be disambiguated by def_file (PITFALLS:
    # multidef conflation leaked unrelated types into depends_on.types).
    from collections import defaultdict as _dd
    _deffiles: dict[str, set] = _dd(set)
    for _r in funcs:
        if _r.get("name") and _r.get("def_file"):
            _deffiles[_r["name"]].add(_r["def_file"])
    multidef = {n for n, fs in _deffiles.items() if len(fs) > 1}
    field_access_index = _load_field_access_index(csv_dir_t2, multidef)

    # (name, def_file) → row index over functions, globals, macros.
    sym_index: dict[tuple[str, str], dict[str, Any]] = {}
    for r in funcs:
        sym_index[(r["name"], r["def_file"])] = r
    for r in globals_:
        sym_index[(r["name"], r["def_file"])] = r
    for r in macros:
        sym_index[(r["name"], r["def_file"])] = r

    seed_mode = filter_spec.is_seed_mode()

    # Pass 1: enumerate every candidate entry across functions /
    # globals / macros. The wrap-reach gate applies only when
    # scope.json IS provided AND we're not in seed mode (seed mode
    # bypasses the gate at enumeration; the seed admission gate in
    # pass 2 re-applies it more precisely).
    candidates: list[dict[str, Any]] = []

    for r in funcs:
        in_target = scope_enabled and (r["name"], r["def_file"]) in tgt_funcs
        if scope_enabled and not in_target and not unscoped:
            if r["linkage"] in _WRAP_DISALLOWED_FN_KINDS:
                continue
            if not seed_mode and not _in_import(r) \
                    and not reach.is_function_port_reachable(
                        r["name"], r["def_file"]):
                continue
        base = _base_function(r, reach)
        # Scope gates EMISSION only: every record we DO emit is composed
        # codebase-wide, mirroring the type footprints (COMPLETE for port AND
        # wrap). The reach relations are scope-agnostic raw lookups
        # ("scope-split is the caller's responsibility") and the field-access
        # index is already repo-wide, so this is a few dict hits per record and
        # the cost stays bounded by what we emit.
        port_add = _port_additions_function(
            r, reach, by_name, sym_index, field_access_index,
        )
        # `forward` drives the one-hop closure -- that IS emission, so it stays
        # gated on port scope.
        fwd = _forward_syms_of(r["name"], r["def_file"], reach) if in_target else None
        target_file = r["def_file"] or _first_decl(r["decl_files"])
        candidates.append({
            "base": base,
            "port_add": port_add,
            "in_target": in_target,
            "forward": fwd,
            "key": (base["name"], base.get("defined_in") or ""),
            "target_file": target_file,
        })

    for r in globals_:
        in_target = scope_enabled and (r["name"], r["def_file"]) in tgt_globals
        if scope_enabled and not in_target and not unscoped:
            if r["linkage"] in _WRAP_DISALLOWED_GLOBAL_KINDS:
                continue
            if not seed_mode and not _in_import(r) \
                    and not reach.is_global_port_reachable(
                        r["name"], r["def_file"]):
                continue
        base = _base_global(r)
        # content: codebase-wide
        port_add = _port_additions_global(r, reach, by_name, sym_index)
        # A global's initializer references drive the closure exactly like a
        # function's callees do (a dispatch table pulls in its handlers).
        fwd = ({(n, df or "") for n, df in reach.addr_targets_of(r["name"], r["def_file"])}
               | {(gn, gd or "") for gn, gd, _k in
                  reach.globals_used_by(r["name"], r["def_file"])}) if in_target else None
        target_file = r["def_file"] or _first_decl(r["decl_files"])
        candidates.append({
            "base": base,
            "port_add": port_add,
            "in_target": in_target,
            "forward": fwd,
            "key": (base["name"], base.get("defined_in") or ""),
            "target_file": target_file,
        })

    for r in macros:
        # Macros can't be re-exported Rust->C, so a macro defined in a HEADER
        # stays a C `#define` (wrap surface, read via bindgen) even when its
        # header is in port scope — anything still in C may `#include` it.
        # Only a TU-local `.c` macro is genuinely "ported": it has no external
        # consumer and bindgen can't see it, so it's inlined into its TU's
        # Rust translation. Hence a macro is target-section iff it's defined in a
        # `.c` (etc.) file that's in scope.
        in_target = scope_enabled and (r["name"], r["def_file"]) in tgt_macros
        if scope_enabled and not in_target and not seed_mode and not unscoped:
            if not _in_import(r) and not reach.is_macro_port_reachable(
                    r["name"], r["def_file"]):
                continue
        base = _base_macro(r)
        port_add = _port_additions_macro(r, reach)    # content: codebase-wide
        fwd = set() if in_target else None
        target_file = r["def_file"]
        candidates.append({
            "base": base,
            "port_add": port_add,
            "in_target": in_target,
            "forward": fwd,
            "key": (base["name"], base.get("defined_in") or ""),
            "target_file": target_file,
        })

    # Callbacks: function-pointer typedefs from the types CSV. Gated on
    # `_is_callback_typedef`, which resolves the alias chain — `unaliased_kind`
    # is stamped only on the row that ENDS it, so a chained `typedef
    # SSL_verify_cb my_cb;` carries an empty kind and a direct-row check would
    # skip it. The CSVs already treat it as a callback (both callback queries
    # walk `TypedefType` in `routineOf`), and `_callback_deps` filters it out
    # of `depends_on.types` — so it must get an entry here, or consumers would
    # point at a node that does not exist. A callback is signature-shaped and
    # never ported (it becomes an `extern "C" fn` type), so it is
    # always wrap; inclusion is the wrap-reach gate (filter mode) or a direct
    # --name seed (seed mode). The same `_import_reachable` gate that admits
    # wrap structs admits a callback (it is keyed by type name).
    from .types_manifest import _import_reachable  # noqa: E402 (avoid import cycle)
    seen_cb: set[str] = set()
    for r in types:
        if r["kind"] != "typedef" or not _is_callback_typedef(r["name"], by_name):
            continue
        name = r["name"]
        if not name or name.startswith("(") or name in seen_cb:
            continue
        seen_cb.add(name)
        if scope_enabled and not seed_mode and (name, "") not in imp_keys \
                and not _import_reachable(
                    reach, name, "", by_name, target_paths):
            continue
        base = _base_callback(r, reach, by_name)
        candidates.append({
            "base": base,
            "port_add": None,
            "in_target": False,         # FFI typedef — never ported
            "forward": None,          # type-deps don't expand the symbol closure
            "key": (base["name"], ""),
            "target_file": _first_decl(r["decl_files"]),
        })

    # Pass 2: identify seeds (seed mode only). When scope.json is
    # provided, apply the **seed admission gate**: a candidate is
    # admitted as a seed only if it's target-section OR wrap-reachable
    # from port code per the scope.json. Without scope.json, no
    # gate (every match is admitted).
    seed_keys: set[tuple[str, str]] = set()
    dropped_seeds: list[str] = []
    if seed_mode:
        for c in candidates:
            if not is_seed(c["base"], filter_spec):
                continue
            # Wrap-scope seed admission gate: a non-port seed must be reachable
            # from the target, per the scope.json.
            # `--unscoped` bypasses it (same intent as the Pass-1 enumeration
            # drop above), so an explicitly named primitive that no port file
            # calls is still emitted. Any seed the gate drops is logged below
            # (never silently omitted).
            if scope_enabled and not unscoped and not c["in_target"]:
                name = c["base"]["name"]
                def_file = c["base"].get("defined_in") or ""
                kind = c["base"].get("kind") or ""
                if kind and kind.startswith("function"):
                    reachable = reach.is_function_port_reachable(name, def_file)
                elif kind and kind.startswith("global"):
                    reachable = reach.is_global_port_reachable(name, def_file)
                elif kind == "callback":
                    reachable = _import_reachable(
                        reach, name, "", by_name, target_paths)
                else:
                    # Macros (kind=null until agent classification).
                    reachable = reach.is_macro_port_reachable(name, def_file)
                if not reachable:
                    dropped_seeds.append(name)
                    continue
            seed_keys.add(c["key"])
    if dropped_seeds:
        print(
            f"syms: {len(set(dropped_seeds))} seed(s) dropped -- named but not "
            f"target-reachable from scope.json (re-run with --unscoped to include "
            f"them): {sorted(set(dropped_seeds))}"
        )

    # Pass 3: compute closure from port seeds (seed mode + scope_enabled).
    # Closure = union of forward_syms for each port seed, minus the
    # seed set itself. Skipped when `--name` is the selector: name seeds
    # are precise (the named symbol only, no closure). See
    # FilterSpec.expand_closure.
    closure_keys: set[tuple[str, str]] = set()
    if seed_mode and scope_enabled and filter_spec.expand_closure():
        for c in candidates:
            if c["key"] in seed_keys and c["in_target"] and c["forward"]:
                for fwd_key in c["forward"]:
                    if fwd_key not in seed_keys:
                        closure_keys.add(fwd_key)

    # Pass 4: emit. Seeds keep their natural shape (port → with port
    # additions; wrap → base only). Closure entries are emitted as
    # wrap-shape (base only) regardless of their original scope.
    #
    # Per-dir scope is tracked alongside the entry emission. The
    # contract assumes a stem-group manifest dir carries entries of
    # a single section only — the TARGET if any emitted entry belongs to
    # it, otherwise IMPORT. This holds for stem-grouped manifest
    # dirs whose files all sit on one side of scope.json. The
    # mixed-scope-in-one-dir case (a stem-group split by
    # `config.out_of_scope.paths`) is a known limitation tracked in
    # docs/TODO.md.
    entries_by_dir: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    dir_scope: dict[Path, str] = {}
    for c in candidates:
        if seed_mode:
            is_seed_entry = c["key"] in seed_keys
            is_closure_entry = (not is_seed_entry) and (c["key"] in closure_keys)
            if not (is_seed_entry or is_closure_entry):
                continue
            emit_port_shape = is_seed_entry and c["in_target"]
        else:
            # Filter mode: emit all candidates passing wrap-reach.
            emit_port_shape = c["in_target"]

        # --target-only / --import-only post-filters.
        if filter_spec.port_only and not emit_port_shape:
            continue
        if filter_spec.wrap_only and emit_port_shape:
            continue

        entry = dict(c["base"])
        # No port/wrap shape fork: an emitted record always carries its
        # codebase-wide composition. `emit_port_shape` below survives only as
        # the SCOPE classification -- it drives the --target-only/--import-only
        # post-filters and the dir_scope tag, never the content.
        if c["port_add"] is not None:
            entry.update(c["port_add"])
        rel_dir = manifest_dir_for(c["target_file"])
        if rel_dir is None:
            continue
        entries_by_dir[rel_dir].append(entry)
        # Promote dir to TARGET the first time we see a target entry
        # under it; default to IMPORT otherwise.
        if emit_port_shape:
            dir_scope[rel_dir] = scope.TARGET
        else:
            dir_scope.setdefault(rel_dir, scope.IMPORT)

    # Stable sort by (kind, name) per dir.
    def _sort_key(e: dict[str, Any]) -> tuple[str, str]:
        return (e.get("kind") or "zz_macro_unclassified", e["name"])

    for entries in entries_by_dir.values():
        entries.sort(key=_sort_key)
    # Third element mirrors types_manifest.compose's signature (the
    # per-target focus map). Symbols have no field-focus, so it's always
    # empty; kept for a uniform `compose_fn` contract in analyze.py.
    return entries_by_dir, dir_scope, {}


def _first_decl(decl_files_pipe: str) -> str:
    # Canonical declaration (in-repo header > source > external), not the
    # alphabetical decls[0] which biases toward .c / build/ artifacts.
    return scope.canonical_decl(_decls_list(decl_files_pipe)) or ""


_COMMENT = (
    "Factual skeleton emitted by compose/syms_manifest.py. One entry "
    "per symbol (function, global, macro, or callback) whose definition "
    "(or, when undefined, declaration) lives in a file of this stem-group. "
    "Base shape covers all entries (name, kind, declared_in, defined_in, "
    "type, ptr_args, ptr_ret). Port-scope functions additionally carry "
    "`used_by` + a body+signature `depends_on`. Wrap-scope functions carry a "
    "SIGNATURE-ONLY `depends_on` (types from the param/return signature — "
    "structs, container instance/engine, callback typedefs — with `syms: []`; "
    "no body analysis, no `used_by`). A `callback` (function-pointer typedef, "
    "identified deterministically by CodeQL) is signature-shaped: it carries "
    "`ptr_args`/`ptr_ret`, `used_by.{call,ref}`, and a signature `depends_on`; "
    "the agent fills only its per-arg/return ownership. The macro `body` is "
    "never emitted; the agent reads the expansion from source."
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Emit per-stem syms.json manifests at the repo-root tier."
    )
    ap.add_argument("--t1", type=Path, required=True, help="Directory with T1 CSVs.")
    ap.add_argument("--t2", type=Path, required=True, help="Directory with T2 CSVs.")
    ap.add_argument(
        "--scope", type=Path, default=None,
        help="Optional path to scope.json (enables port-aware analysis).",
    )
    ap.add_argument(
        "--out-root", type=Path, required=True,
        help="Parent directory of the repo-root analysis tree.",
    )
    args = ap.parse_args()

    entries_by_dir, dir_scope, _focus = compose(
        args.t1, args.t2, FilterSpec(scope_json_path=args.scope),
    )

    from collections import Counter
    total = 0
    port_dirs = 0
    wrap_dirs = 0
    for rel_dir, entries in sorted(entries_by_dir.items()):
        after = len(entries)
        total += after
        if dir_scope.get(rel_dir) == scope.TARGET:
            port_dirs += 1
        else:
            wrap_dirs += 1
    kinds = Counter()
    for entries in entries_by_dir.values():
        for e in entries:
            kinds[e.get("kind") or "macro_unclassified"] += 1
    print(
        f"syms: {len(entries_by_dir)} manifest dirs "
        f"({port_dirs} target-section, {wrap_dirs} import-section), "
        f"{total} entries → {args.out_root}"
    )
    for k, c in sorted(kinds.items()):
        print(f"  {k:<28} {c}")


if __name__ == "__main__":
    main()
