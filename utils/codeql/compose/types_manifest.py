"""Compose the type records, grouped by source stem.

Mirrors `syms_manifest.py`'s seed + closure model, adapted to types:

  - **Port-scope types** (`defined_in ∈ scope.json`) are the porting
    subjects → **extended schema**: full declared-field layout. They
    will be rewritten as native Rust types.
  - **Import types** (reached by target code) get the **base
    schema** with `fields[]` **narrowed to the target-accessed subset**
    (the FFI surface).

Every struct entry (port and wrap) carries two consumer footprints,
`opaque_in` and `non_opaque_in` (`{file: [symbols]}`), partitioning
the functions that touch the type by whether they access a field:

  - `non_opaque_in` — touchers that read/write a field (layout users).
  - `opaque_in` — touchers that only hold the type as an opaque handle
    (forwarders, allocate-and-return ctors, delegating wrappers).

Both footprints carry the COMPLETE cross-codebase set for port AND wrap
types — scope-agnostic, like the manifest as a whole. The per-target scope
view (narrowing to the in-scope universe) is applied at READ time by
`query types --users`, never baked here. The footprints are the deterministic
candidate pool from which the type wrapper derives lifecycle + per-field
accessors (no `ops` list on a concrete type).

Field schema (per `fields[]` entry):

  - `{name}`                              scalar single
  - `{name, type, ref, array?}`           scalar array / by-value aggregate
  - `{name, type, ref:"pointer", ptr:{…}, array?}`   pointer (single/array)
  - anonymous-aggregate-typed fields are emitted as value fields
    carrying the raw `(unnamed …)` type string; deep nested inlining
    (the `anon` block) is deferred — see docs/TODO.md.

`ref` is `"value"` / `"pointer"` (element reference kind); `array` is
`{size:N|null}` (multiplicity). The `ptr` block is agent-filled
(ownership / nullability / lifetime / synthetic-type), composer emits
a null skeleton. Composer fills name / type / ref / array; the type
agent fills ops / lifecycle / placement (unchanged) plus every `ptr`
block.

Closure (seed mode): target type seeds pull the transitive set of
types referenced by their non-scalar fields (cycle-safe). Filter mode
(`--scope`, no seed flags): emit all port types + all wrap types
passing the 7-scenario reach gate.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import scope
from . import macro_families
from .filter_spec import FilterSpec, is_seed
from .manifest_merge import type_key
from .path_partition import manifest_dir_for
from .reach import Reach


# ------------------------------------------------------------------ field parsing

_ARRAY_DIM_RE = re.compile(r"\[(\d*)\]")
_ARRAY_SUFFIX_RE = re.compile(r"(\[\d*\])+\s*$")


def _parse_field_type(
    field_type: str, is_scalar: bool, by_name: dict[str, dict] | None = None,
    ptr_depth: int = 0,
) -> dict[str, Any]:
    """Decompose a CodeQL `Type.toString()` field type into structural
    parts.

    Returns ``{elem, ref, array, const, anon}``:
      - `elem`  — element type string (array dimension stripped)
      - `ref`   — `"pointer"` / `"value"` / `None` (None = scalar single)
      - `array` — `{"size": N|None}` or `None`
      - `const` — bool (const-qualified somewhere in the type)
      - `anon`  — bool (element is an anonymous aggregate)

    Pointer detection is `ptr_depth > 0` — the authoritative count from
    `entities/fields.ql`, which walks the real CodeQL `Type`. The literal `*`
    in `field_type` is only a fallback for a pre-`ptr_depth` extraction: the
    string is `Type.toString()`, and C hides the star behind a name in two
    common shapes it therefore cannot show —

      - an OBJECT-pointer typedef (`typedef struct _filesec *filesec_t;`),
        which types.csv also cannot disambiguate (`aliasOf` unwraps the star,
        making `typedef T *P` identical to `typedef T P`);
      - a bare function pointer (`int (*ctrl)(BIO *, int, long, void *)`),
        modelled as `FunctionPointerIshType`, not a `PointerType`.

    Both used to collapse to a bare `{name}` scalar — no `ptr` block, so no
    ownership analysis for something that *is* a pointer.

    `is_scalar` answers a DIFFERENT question ("does the type contain an
    aggregate?") and is only consulted for non-pointer fields.
    """
    ft = (field_type or "").strip()
    const = ft.startswith("const ") or " const" in ft

    # Strip trailing array dimension(s); record the outermost size.
    array: dict[str, Any] | None = None
    dims = _ARRAY_DIM_RE.findall(ft)
    if dims and _ARRAY_SUFFIX_RE.search(ft):
        ft = _ARRAY_SUFFIX_RE.sub("", ft).strip()
        outer = dims[0]
        array = {"size": int(outer) if outer else None}

    anon = ft.startswith("(") or "(unnamed" in ft or "(anonymous" in ft
    # `ptr_depth` is authoritative; the literal `*` keeps a pre-`ptr_depth`
    # extraction working (it under-reports exactly the hidden-star shapes).
    is_pointer = ptr_depth > 0 or "*" in ft

    if is_pointer:
        ref: str | None = "pointer"
    elif anon:
        ref = "value"
    elif is_scalar and array is None:
        ref = None  # scalar single → bare {name}
    else:
        ref = "value"  # scalar array or by-value aggregate

    return {"elem": ft, "ref": ref, "array": array, "const": const, "anon": anon}


def _null_ptr_skeleton():
    """The agent-fillable ownership slot on a pointer FIELD, emitted `null` by
    the composer.

    `null` is the UNANALYZED state, so "has this field been through the
    wrapper?" is a null check on one key — an all-null keyed skeleton would be
    indistinguishable from a block the agent filled with nulls, and is not
    submittable anyway (a submitted block replaces the prior WHOLESALE and must
    be complete). Once filled it is the same `{scalar, array, string, owned,
    borrowed, nullable, mutable, note}` block a `ptr_args[*]`/`ptr_ret` record
    carries (see syms_manifest._null_ptr_agent), so a pointer is described
    identically whether it sits in a struct or crosses a call boundary."""
    return None


# A field type string CodeQL cannot name: a bare function pointer
# (`..(*)(..)`) or an anonymous aggregate (`(unnamed …)`). Consumers resolving
# edges off the string get nothing from these.
_UNNAMEABLE_ELEM_RE = re.compile(r"^\(|\(\s*\*")


def _compose_field(
    field_name: str, field_type: str, is_scalar: bool,
    by_name: dict[str, dict] | None = None, ptr_depth: int = 0,
    sig_types: list[str] | None = None,
) -> dict[str, Any]:
    """Build one `fields[]` entry from raw `(name, type, is_scalar)`.

    Fields carry their structural shape (`type` / `ref` / `array` / `ptr`) only;
    the wrapper derives accessors from the field layout directly — there are no
    per-field C getter/setter lists."""
    parsed = _parse_field_type(field_type, is_scalar, by_name, ptr_depth)
    ref = parsed["ref"]
    if ref is None:
        return {"name": field_name}  # scalar single

    entry: dict[str, Any] = {"name": field_name, "type": parsed["elem"], "ref": ref}
    if parsed["array"] is not None:
        entry["array"] = parsed["array"]
    # `sig_types` — the user types named INSIDE an unnameable field type, i.e.
    # a bare function pointer's signature (`int (*ctrl)(BIO *, …)` -> `BIO`).
    # `type` renders as `..(*)(..)`, so a consumer resolving edges off that
    # string gets nothing and the field looks like a dependency leaf. Emitted
    # only when the string is unnameable — an ordinary `T *` needs no help.
    if sig_types and _UNNAMEABLE_ELEM_RE.search(parsed["elem"] or ""):
        entry["sig_types"] = sorted(set(sig_types))
    if ref == "pointer":
        entry["ptr"] = _null_ptr_skeleton()
    return entry


def _canonical_tag(type_str: str, by_name: dict[str, dict]) -> str | None:
    """Resolve a field element type to its canonical struct/union/enum
    tag for closure cross-referencing. Returns None for scalars,
    primitives, anonymous, or unresolvable types.
    """
    s = (type_str or "").strip()
    if not s or s.startswith("("):
        return None
    # Strip const + pointer stars → base name.
    s = re.sub(r"\bconst\b", "", s)
    s = s.replace("*", "").strip()
    if not s or " " in s and s.split()[-1] in {"int", "char", "long", "short", "void", "double", "float"}:
        # primitive multi-word (e.g. "unsigned char") → not a user type
        # unless it's a tag; fall through to typedef resolution below.
        pass
    base = s.split()[-1] if " " in s else s
    # Prefer the full token if it's a known type; else the last word.
    candidate = s if s in by_name else base
    row = by_name.get(candidate)
    if row is None:
        return None
    if row["kind"] == "typedef":
        terminal = scope.resolve_typedef(candidate, by_name)
        if terminal is not None and terminal["kind"] in {"struct", "union", "enum"}:
            tag = terminal.get("name", "")
            return tag if tag and not tag.startswith("(") else None
        return None
    if row["kind"] in {"struct", "union", "enum"}:
        return candidate
    return None


def _forward_type_tags(
    field_entries: list[dict[str, Any]], by_name: dict[str, dict],
) -> set[str]:
    """Canonical tags referenced by a type's non-scalar fields — the
    one-hop forward edge set for the closure."""
    tags: set[str] = set()
    for f in field_entries:
        t = f.get("type")
        if not t:
            continue
        tag = _canonical_tag(t, by_name)
        if tag:
            tags.add(tag)
    return tags


# ------------------------------------------------------------------ field composition

def _compose_fields_full(
    reach: Reach, struct_name: str, struct_def_file: str,
    by_name: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Full declared-field layout for a type, **scope-agnostic**.

    The manifest carries the complete struct layout for both port and
    wrap types: port types are reimplemented natively (every field
    matters), and wrap types need the full layout for the bindgen
    layout closure (a container goes opaque if bindgen lacks a type for
    any field). The agent's *analysis* surface is narrowed separately
    via the per-target focus (see `_port_touched_field_names` +
    `analyze._analysis_focus`), so a full layout here does not expand
    the agent's workset. Falls back to the access-narrowed set when
    fields.csv has no full-body definition in the DB.
    """
    declared = reach.struct_fields(struct_name, struct_def_file)
    if declared:
        return [
            _compose_field(
                name, ftype, scalar, by_name, depth,
                [t for t, _k, _f in reach.types_in_field(
                    struct_name, struct_def_file, name)],
            )
            for name, ftype, scalar, depth in declared
        ]
    # No full-body definition in the DB — fall back to accessed fields.
    return _compose_fields_wrap(
        reach, struct_name, struct_def_file, port_only=False, by_name=by_name,
    )


def _port_touched_field_names(
    reach: Reach, struct_name: str, struct_def_file: str,
) -> list[str]:
    """The field names target code actually reaches into — the
    deterministic per-target analysis surface handed to the type agent
    via `focus.fields`. Transient (target-specific): never persisted to
    the scope-agnostic manifest."""
    records = reach.field_access_records(struct_name, struct_def_file)
    if not records:
        return []
    accessed = {
        field for _enc, access_file, field, _kind in records
        if reach._is_port(access_file)  # noqa: SLF001 — intra-package use
    }
    return sorted(accessed)


def _compose_fields_wrap(
    reach: Reach, struct_name: str, struct_def_file: str,
    *, port_only: bool = True, by_name: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Access-narrowed field set for an import type — only the
    fields some (port, when `port_only`) symbol actually reaches into.
    This is the FFI surface.
    """
    records = reach.field_access_records(struct_name, struct_def_file)
    if not records:
        return []
    if port_only:
        accessed = {
            field for _enc, access_file, field, _kind in records
            if reach._is_port(access_file)  # noqa: SLF001 — intra-package use
        }
    else:
        accessed = {field for _enc, _file, field, _kind in records}
    out: list[dict[str, Any]] = []
    for name in sorted(accessed):
        meta = reach.field_type_of(struct_name, struct_def_file, name)
        if meta is None:
            out.append({"name": name})
            continue
        ftype, scalar, depth = meta
        out.append(_compose_field(
            name, ftype, scalar, by_name, depth,
            [t for t, _k, _f in reach.types_in_field(
                struct_name, struct_def_file, name)],
        ))
    return out


def _type_consumers(
    reach: Reach, struct_name: str, struct_def_file: str,
    by_name: dict[str, dict], in_scope: set[str] | None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Partition the functions that *touch* this type into two
    `{file: [symbols]}` footprints, returned as `(opaque_in,
    non_opaque_in)`.

    A function *touches* the type if it mentions it in its **signature**
    (param/return) or **body** (local/cast). The partition key is
    whether it also accesses a field of the type:

      - **`non_opaque_in`** — touchers that access ≥1 field (need the
        concrete layout). Source: field-access inversion. This is the
        layout footprint.
      - **`opaque_in`** — touchers that never access a field (hold the
        type as an opaque handle): forwarders, allocate-and-return
        ctors, delegating wrappers (`SSL_SESSION_dup` → `dup_intern`).

    Together the two sets are the complete touch surface; the agent
    populates `ops` by reasoning over both (non-opaque = strong
    op signal; opaque = lifecycle/forwarder candidates).

    `in_scope` (when not None) restricts both footprints to the
    admitted symbol universe (port-defined ∪ target-reachable). Applied
    for **import-section** types, whose only relevant consumers are the
    ones the port reaches. **Port-scope** types pass `in_scope=None` to
    keep the full cross-codebase footprint — required for ABI/layout
    completeness and to retain out-of-scope lifecycle ops (a wrap-side
    `up_ref`/`dup`) the Rust reimplementation must still honor.
    """
    # non-opaque: functions that access the type's fields (layout users).
    # `non_opaque_names` is the UNFILTERED accessor set so that an
    # out-of-scope field user is still excluded from `opaque_in`.
    non_opaque_names: set[str] = set()
    non_by_file: dict[str, set[str]] = defaultdict(set)
    for enclosing, access_file, _field, _kind in reach.field_access_records(
        struct_name, struct_def_file,
    ):
        if not enclosing:
            continue
        non_opaque_names.add(enclosing)
        if access_file and (in_scope is None or enclosing in in_scope):
            non_by_file[access_file].add(enclosing)

    # opaque: signature/body touchers that never access a field.
    opq_by_file: dict[str, set[str]] = defaultdict(set)
    for tname, tdef in _type_keys_for(struct_name, struct_def_file, by_name):
        touchers = (
            reach.functions_using_type(tname, tdef)
            | reach.functions_using_type_in_body(tname, tdef)
        )
        for fn_name, fn_def in touchers:
            if fn_name in non_opaque_names:
                continue
            if in_scope is not None and fn_name not in in_scope:
                continue
            opq_by_file[fn_def or ""].add(fn_name)

    opaque_in = {f: sorted(s) for f, s in sorted(opq_by_file.items())}
    non_opaque_in = {f: sorted(s) for f, s in sorted(non_by_file.items())}
    return opaque_in, non_opaque_in


# ------------------------------------------------------------------ typedef + reach helpers (unchanged)

def _first(decl_files_pipe: str) -> str | None:
    # Canonical declaration (in-repo header > source > external), not the
    # alphabetical [0] which biases toward .c / build/ artifacts.
    return scope.canonical_decl(scope.parse_decl_files(decl_files_pipe))


def _typedef_aliases_by_terminal(
    types_rows: list[dict], by_name: dict[str, dict],
) -> dict[str, list[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for r in types_rows:
        if r["kind"] != "typedef":
            continue
        if r["name"].startswith("("):
            continue
        if not r["aliases"] or r["aliases"] not in by_name:
            continue
        terminal = scope.resolve_typedef(r["aliases"], by_name)
        if terminal is None or terminal["kind"] not in {"struct", "union", "enum"}:
            continue
        if terminal["name"].startswith("("):
            continue
        out[terminal["name"]].add(r["name"])
    return {k: sorted(v) for k, v in out.items()}


def _aliases_pointing_at(target_tag: str, by_name: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for name, row in by_name.items():
        if row["kind"] != "typedef":
            continue
        terminal = scope.resolve_typedef(name, by_name)
        if terminal is not None and terminal["kind"] in {"struct", "union", "enum"} \
                and terminal["name"] == target_tag:
            out.append(name)
    return out


def _type_keys_for(name: str, def_file: str, by_name: dict[str, dict]) -> list[tuple[str, str]]:
    keys = [(name, def_file)]
    for alias in _aliases_pointing_at(name, by_name):
        keys.append((alias, ""))
    return keys


def _import_reachable(
    reach: Reach, struct_name: str, struct_def_file: str,
    by_name: dict[str, dict], target_paths: set[str],
) -> bool:
    """7-scenario reach gate — an out-of-target type is included iff any
    fires, i.e. iff the target actually touches it."""
    type_keys = _type_keys_for(struct_name, struct_def_file, by_name)
    if reach.port_field_access_files(struct_name, struct_def_file):
        return True
    for tname, tdef in type_keys:
        for _fn, fn_def_file in reach.functions_using_type(tname, tdef):
            if fn_def_file in target_paths:
                return True
        for _fn, fn_def_file in reach.functions_using_type_in_body(tname, tdef):
            if fn_def_file in target_paths:
                return True
    for tname, tdef in type_keys:
        for fn_name, fn_def_file in reach.functions_using_type(tname, tdef):
            if fn_def_file not in target_paths and reach.is_function_port_reachable(fn_name, fn_def_file):
                return True
        # Body usage by a target-REACHABLE (non-target-file) function. import_closure
        # pulls these in via depends_on.types (field_access_index over the
        # body), so WITHOUT this the per-type gate diverges from the wrap
        # surface and the type lands in import.types with no record
        # (git_config_entry, git_error, git_pool_page, error_threadstate — used
        # only in the bodies of target-reachable functions, never their
        # signatures). No path guard: anything legitimately import-section and
        # needed by the target earns a record, external or not (an external
        # type like pthread_mutex_t is already admitted by-value via S5; the
        # scaffolder decides bindgen-vs-record home downstream, not this gate).
        for fn_name, fn_def_file in reach.functions_using_type_in_body(tname, tdef):
            if fn_def_file not in target_paths and reach.is_function_port_reachable(fn_name, fn_def_file):
                return True
    port_fields = reach.port_touched_fields()
    for tname, tdef in type_keys:
        if reach.fields_referencing_type(tname, tdef) & port_fields:
            return True
    port_globals = reach.port_accessed_globals()
    for tname, tdef in type_keys:
        if reach.globals_referencing_type(tname, tdef) & port_globals:
            return True
    return False


# ------------------------------------------------------------------ skeletons

def _struct_skeleton(
    struct_name: str, typedefs: list[str],
    declared_in: str | None, defined_in: str | None,
    fields: list[dict[str, Any]], kind: str = "struct",
) -> dict[str, Any]:
    # `kind` is "struct" or "union" — a union takes the SAME skeleton (it has a
    # member layout, footprints, casts) and only differs in the tag. Unions get
    # no agent analysis (only structs carry per-field work); their
    # per-field `ptr` slots stay null.
    return {
        "name": struct_name, "typedef": typedefs, "kind": kind,
        "declared_in": declared_in, "defined_in": defined_in,
        "casted": {"to": [], "from": []},
        "fields": fields,
    }


def _enum_skeleton(
    name: str, declared_in: str | None, defined_in: str | None, typedefs: list[str],
) -> dict[str, Any]:
    e = {
        "name": name, "typedef": typedefs, "kind": "enum",
        "declared_in": declared_in, "defined_in": defined_in,
        "casted": {"to": [], "from": []}, "fields": [],
    }
    return e


def _build_struct_entry(
    reach: Reach, name: str, def_file: str,
    declared_in: str | None, defined_in: str | None,
    typedefs: list[str], in_target: bool,
    by_name: dict[str, dict], kind: str = "struct",
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str], list[str] | None]:
    """Build a struct-shaped manifest entry, its field-type forward edges, and
    the transient target-touched field subset — the single path shared by the
    named-struct loop, the anonymous-struct-typedef loop, and unions (`kind`
    "union" — same member layout / footprints, only the tag differs).

    A struct's `defined_in` / `fields` / consumer footprints / forward edges
    must be identical whether its identity comes from a C tag (`struct foo`) or
    from the typedef that names an inline anonymous `struct { … }`
    (`typedef struct { … } git_cache;`). Keeping both callers on this one
    helper is what prevents the two from drifting (the drift that left
    anonymous-typedef structs with `defined_in: null` and `fields: []`).

    `ops` stays the skeleton's empty list — agent-populated from the consumer
    footprints below. Returns ``(entry, fields, forward, touched)``; ``touched``
    (the wrap-only target-touched field subset, returned out-of-band via
    focus_by_key) is None for target types.
    """
    fields = _compose_fields_full(reach, name, def_file, by_name)
    entry = _struct_skeleton(name, typedefs, declared_in, defined_in, fields, kind)
    # Consumer footprints, partitioned opaque vs non-opaque — the COMPLETE
    # cross-codebase footprint for both port and wrap types (scope-agnostic,
    # like the manifest as a whole). The per-target scope view is applied at
    # READ time by `query types --users` (filter to scope.json), not baked here.
    entry["opaque_in"], entry["non_opaque_in"] = _type_consumers(
        reach, name, def_file, by_name, None,
    )
    touched = None if in_target else _port_touched_field_names(reach, name, def_file)
    forward = _forward_type_tags(fields, by_name)
    return entry, fields, forward, touched


# ------------------------------------------------------------------ orchestration

def compose(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    filter_spec: FilterSpec | None = None,
) -> tuple[dict[Path, list[dict[str, Any]]], dict[Path, str]]:
    """Returns ``(entries_by_dir, dir_scope)``:

      - ``entries_by_dir``: ``{manifest_dir: [entries]}`` ready for the
        merge primitive.
      - ``dir_scope``: ``{manifest_dir: TARGETED | IMPORTED}`` parallel
        map. Orchestrator consumers (the type wrapper's manifests-list
        input contract) read this to tag each manifest with its scope
        without persisting the tag to disk. Assumes a stem-group
        manifest dir carries entries of a single scope only; the
        mixed-scope case (a stem-group split by
        `config.out_of_scope.paths`) is tracked in docs/TODO.md.

    See module docstring for the seed/closure + port/wrap model.
    """
    if filter_spec is None:
        filter_spec = FilterSpec()
    target_paths = (
        scope.load_targeted_paths(filter_spec.scope_json_path)
        if filter_spec.scope_json_path is not None else set()
    )
    scope_enabled = filter_spec.scope_json_path is not None
    seed_mode = filter_spec.is_seed_mode()

    # v2: port type classification is precomputed in scope.json's `types`
    # entity set; membership lookup on (name, def_file) replaces the
    # per-row classify()/classify_type() pass. target_paths (files) still
    # drives wrap-side reachability.
    tgt_types = (
        scope.load_entities(filter_spec.scope_json_path, scope.TARGETED, "types")
        if filter_spec.scope_json_path is not None else set()
    )
    # The composed IMPORTED surface, as an admission floor. `_import_reachable`
    # asks its seven questions of the CodeQL edges directly; the closure ALSO
    # admits a type through the transitive field-walk (a target struct's field
    # whose type is private), which no scenario there covers. A scope.json
    # entry with no manifest record is unschedulable, so the surface is
    # authoritative: whatever it lists gets a record. Empty when scope.json is
    # absent.
    imtgt_types = (
        scope.load_entities(filter_spec.scope_json_path, scope.IMPORTED, "types")
        if filter_spec.scope_json_path is not None else set()
    )

    def _in_import_surface(c: dict) -> bool:
        return (c["name"], c["def_file"] or "") in imtgt_types or \
            _import_reachable(reach, c["name"], c["def_file"], by_name,
                              target_paths)

    types_rows = scope.load_csv(csv_dir_t1 / "types.csv")
    by_name = scope.build_types_index(types_rows)
    reach = Reach(csv_dir_t2, target_paths, csv_dir_t1=csv_dir_t1)
    typedefs_for = _typedef_aliases_by_terminal(types_rows, by_name)

    # ---- Pass 1: enumerate candidate entries ----
    # Each candidate: {entry, in_target, key, name, def_file, forward, target_file}
    candidates: list[dict[str, Any]] = []
    # Deduped by (name, def_file), NOT name alone: a file-local struct
    # (e.g. `struct entry` in indexer.c) must not be masked by a distinct
    # same-named struct in another file (e.g. xdiff's). Same (tag, def_file)
    # identity principle as the symbol composers.
    seen: set[tuple[str, str]] = set()

    def _is_port(r: dict) -> bool:
        return scope_enabled and (r["name"], r["def_file"]) in tgt_types

    for r in types_rows:
        if r["kind"] not in {"struct", "union", "enum"}:
            continue
        if r["name"].startswith("(") or not r["name"]:
            continue
        # Upgrade a forward-decl-only row (empty def_file) to its definition
        # row; a row that already carries its own def_file is kept as-is so
        # distinct same-named structs across files stay separate candidates.
        if not r["def_file"]:
            r = by_name.get(r["name"], r)
        cand_id = (r["name"], r["def_file"] or "")
        if cand_id in seen:
            continue
        seen.add(cand_id)
        decls = scope.parse_decl_files(r["decl_files"])
        # The entry's stored `declared_in` FIELD gets the FULL decl list (set
        # just before append) so the scaffolder reasons over every decl site
        # and no instantiation header is dropped. The skeleton builders only
        # STORE that field — they don't read it. This LOCAL is the single
        # canonical pick for the one place that needs one file: the typedef
        # loop's `target_file`.
        declared_in = scope.canonical_decl(decls)
        defined_in = r["def_file"] or None
        in_target = _is_port(r)

        # Named struct/union/enum identity comes from the C tag here; the
        # anonymous-typedef loop below mirrors this via the same
        # _build_struct_entry path. A union takes the struct path (member layout
        # + footprints) but keeps `kind: union` so the wrapper worklist skips it.
        if r["kind"] == "enum":
            entry = _enum_skeleton(r["name"], declared_in, defined_in, typedefs_for.get(r["name"], []))
            fields, forward, touched = [], set(), None
        else:
            entry, fields, forward, touched = _build_struct_entry(
                reach, r["name"], r["def_file"], declared_in, defined_in,
                typedefs_for.get(r["name"], []), in_target, by_name, r["kind"],
            )

        entry["declared_in"] = decls          # full decl list on the stored field
        candidates.append({
            "entry": entry, "in_target": in_target,
            "key": (scope.entry_tag(entry), entry.get("defined_in") or ""),
            "name": r["name"], "def_file": r["def_file"],
            "forward": forward,
            "target_file": r["def_file"] or _first(r["decl_files"]),
            "touched": touched,
        })

    # Anonymous-base typedefs (struct/union/enum) where the typedef IS the
    # identity. (Function-pointer typedefs — `unaliased_kind = "callback"` — are
    # signature-shaped symbols, emitted by the SYMBOL composer into syms.json,
    # not here.)
    seen_td: set[str] = set()
    for r in types_rows:
        if r["kind"] != "typedef" or r["name"].startswith("(") or not r["name"]:
            continue
        unalias = r.get("unaliased_kind", "")
        if unalias not in {"struct_anonymous", "union_anonymous",
                           "enum_anonymous"} or r["name"] in seen_td:
            continue
        seen_td.add(r["name"])
        in_target = scope_enabled and (r["name"], r["def_file"]) in tgt_types
        decls = scope.parse_decl_files(r["decl_files"])
        # The entry's stored `declared_in` FIELD gets the FULL decl list (set
        # just before append) so the scaffolder reasons over every decl site
        # and no instantiation header is dropped. The skeleton builders only
        # STORE that field — they don't read it. This LOCAL is the single
        # canonical pick for the one place that needs one file: the typedef
        # loop's `target_file`.
        declared_in = scope.canonical_decl(decls)
        # The typedef name IS this type's identity (the anonymous base has no
        # tag), and types.ql now resolves the inline aggregate's body site, so
        # the typedef carries a real def_file — the header/TU the anonymous
        # `struct { … }` lives in. An anonymous-typedef struct is otherwise a
        # normal struct: build it through the SAME _build_struct_entry path as a
        # named one (identity = [typedef name]) so the two cannot drift.
        defined_in = r["def_file"] or None
        identity = [r["name"]]
        forward: set[str] = set()
        touched: list[str] | None = None
        if unalias in {"struct_anonymous", "union_anonymous"}:
            entry, _fields, forward, touched = _build_struct_entry(
                reach, r["name"], r["def_file"], declared_in, defined_in,
                identity, in_target, by_name,
                "union" if unalias == "union_anonymous" else "struct",
            )
        else:  # enum_anonymous
            entry = _enum_skeleton(r["name"], declared_in, defined_in, identity)
        entry["declared_in"] = decls          # full decl list on the stored field
        candidates.append({
            "entry": entry, "in_target": in_target,
            "key": (scope.entry_tag(entry), entry.get("defined_in") or ""),
            "name": r["name"], "def_file": r["def_file"],
            "forward": forward,
            "target_file": r["def_file"] or declared_in or "",
            "touched": touched,
        })

    # Index candidates by canonical tag for closure recursion.
    by_tag: dict[str, dict[str, Any]] = {c["name"]: c for c in candidates}

    # ---- Pass 2 + 3: seeds + closure (seed mode), or gate (filter mode) ----
    emit_keys: set[tuple[str, str]] = set()

    if seed_mode:
        seed_keys: set[tuple[str, str]] = set()
        for c in candidates:
            if not is_seed(c["entry"], filter_spec, name_key="name"):
                continue
            # Wrap-scope seed admission gate: a non-port seed must be reachable
            # from port code per the scope.json. `--unscoped` bypasses it (same
            # intent as the filter-mode branch below), so an explicitly named
            # type that no port file reaches is still emitted.
            if scope_enabled and not filter_spec.unscoped and not c["in_target"]:
                if not _in_import_surface(c):
                    continue
            seed_keys.add(c["key"])
        # Transitive field-type closure. Seeded from target seeds
        # only (wrap seeds, like wrap symbol seeds, don't expand), but
        # once inside the closure we follow EVERY neighbour's forward
        # edges — including wrap types, via their port-narrowed fields
        # — so the full reached surface (e.g. a wrap type reached
        # through another wrap type's accessed field) is captured.
        # Skipped when `--name` is the selector: name seeds are precise
        # (the named type only, no closure). See FilterSpec.expand_closure.
        closure_keys: set[tuple[str, str]] = set()
        if filter_spec.expand_closure():
            worklist = [c for c in candidates if c["key"] in seed_keys and c["in_target"]]
            visited_tags: set[str] = set()
            while worklist:
                c = worklist.pop()
                for tag in c["forward"]:
                    if tag in visited_tags:
                        continue
                    visited_tags.add(tag)
                    nbr = by_tag.get(tag)
                    if nbr is None:
                        continue
                    if nbr["key"] not in seed_keys:
                        closure_keys.add(nbr["key"])
                    worklist.append(nbr)
        emit_keys = seed_keys | closure_keys
    else:
        for c in candidates:
            # `--unscoped` emits every candidate, skipping the out-of-scope
            # reachability drop (repo-wide inventory). scope.json still
            # classifies port/wrap for the entries that qualify.
            if not scope_enabled or filter_spec.unscoped:
                emit_keys.add(c["key"])
            elif c["in_target"]:
                emit_keys.add(c["key"])
            elif _in_import_surface(c):
                emit_keys.add(c["key"])

    # ---- Pass 4: emit, applying --targeted-only / --imported-only ----
    # Per-dir section is tracked alongside emission: a dir is TARGETED if
    # any of its emitted entries belongs to it, else IMPORTED. This
    # mirrors the convention used by syms_manifest.compose() and is
    # subject to the mixed-scope-in-one-dir limitation tracked in
    # docs/TODO.md.
    entries_by_dir: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    dir_scope: dict[Path, str] = {}
    # Out-of-band, target-specific analysis surface keyed by entry identity
    # (type tag, defined_in). Consumed by analyze._analysis_focus; never
    # persisted to the scope-agnostic manifest.
    focus_by_key: dict[tuple[str, str], list[str]] = {}
    for c in candidates:
        if c["key"] not in emit_keys:
            continue
        if filter_spec.port_only and not c["in_target"]:
            continue
        if filter_spec.wrap_only and c["in_target"]:
            continue
        rel_dir = manifest_dir_for(c["target_file"])
        if rel_dir is None:
            continue
        entries_by_dir[rel_dir].append(c["entry"])
        if c["in_target"]:
            dir_scope[rel_dir] = scope.TARGETED
        else:
            dir_scope.setdefault(rel_dir, scope.IMPORTED)
        if c.get("touched") is not None:
            focus_by_key[(scope.entry_tag(c["entry"]), c["entry"].get("defined_in") or "")] = \
                c["touched"]

    # Fill the raw cast graph per entry, keyed by tag (edges/casts.ql via
    # Reach). `casted.to` = tags this type is cast into; `casted.from` = tags
    # cast into it. Stored verbatim, unclassified — consumers disambiguate
    # engine erasure / downcast / ASN1 punning.
    for entries in entries_by_dir.values():
        for e in entries:
            if "casted" in e:
                _tag = scope.entry_tag(e)
                e["casted"] = {"to": reach.casts_to(_tag),
                               "from": reach.casts_from(_tag)}
        entries.sort(key=lambda e: scope.entry_tag(e) or "")

    # Template-by-macro families: `generated_by` names the macro that minted
    # this type. The inverse `generates` lives on the MACRO's own symbol record
    # -- the macro is already an entity, so there is no synthetic type to mint
    # and no `(name, file)` collision with the macro's own node. Unlike `casted`
    # the relation is DIRECTED: an instance always depends on its generator, so
    # nothing downstream has to infer which side comes first.
    _fams = macro_families.load(csv_dir_t1.parent)
    _gen_of = macro_families.generated_by(_fams)
    for entries in entries_by_dir.values():
        for e in entries:
            key = (scope.entry_tag(e), e.get("defined_in") or "")
            e["generated_by"] = _gen_of.get(key)
    return entries_by_dir, dir_scope, focus_by_key


_COMMENT = (
    "Factual skeleton emitted by compose/types_manifest.py. Port-scope "
    "types (defined_in in scope.json) carry the full declared-field "
    "layout and will be ported to native Rust. Wrap-scope types carry "
    "the base schema with `fields[]` narrowed to the target-accessed "
    "subset (FFI surface). Each field: {name} (scalar single) | "
    "{name,type,ref,array?} (value/array) | "
    "{name,type,ref:pointer,ptr:{…},array?} (pointer). `ptr` ownership "
    "block is agent-filled (composer emits null skeleton); `owned` is the "
    "*buffer* ownership, `container`=this pointer holds a collection of "
    "element pointers and `owned_elem` (when container) = the struct owns/"
    "frees the *elements* (a stack/map that owns its array but borrows its "
    "payloads is `owned:true, container:true, owned_elem:false`). Every struct "
    "carries two consumer footprints {file:[symbols]}: `non_opaque_in` "
    "(touchers that access a field — layout users) and `opaque_in` "
    "(touchers that hold the type opaquely — forwarders, ctors, "
    "wrappers). Wrap types restrict both to the in-scope universe "
    "(port-defined ∪ target-reachable); port types keep the full "
    "footprint. The agent fills each pointer field's `ptr` ownership block and "
    "any guarded field's `locked_by`; a type stores NO lifecycle of its own -- "
    "which routines drop/dispose/clone it is reverse-derived from the acting "
    "symbols (`query symbols --lifetime-for <TAG>`). "
    "`casted` {to,from} is the composer-filled raw struct<->struct cast graph "
    "(see _comment_casted) — engine erasure, downcasts "
    "and ASN1 punning all coexist there, unclassified. "
    "Anonymous-aggregate fields carry the raw type string; deep inlining "
    "deferred (see docs/TODO.md)."
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Emit per-stem types.json manifests at the repo-root tier."
    )
    ap.add_argument("--t1", type=Path, required=True, help="Directory with T1 CSVs.")
    ap.add_argument("--t2", type=Path, required=True, help="Directory with T2 CSVs.")
    ap.add_argument("--scope", type=Path, default=None,
                    help="Optional path to scope.json (enables port-aware analysis).")
    ap.add_argument("--out-root", type=Path, required=True,
                    help="Parent directory of the repo-root analysis tree.")
    args = ap.parse_args()

    entries_by_dir, dir_scope, _focus = compose(
        args.t1, args.t2, FilterSpec(scope_json_path=args.scope),
    )

    from collections import Counter
    total = 0
    kinds: Counter[str] = Counter()
    with_consumers = 0
    port_dirs = 0
    wrap_dirs = 0
    for rel_dir, entries in sorted(entries_by_dir.items()):
        total += len(entries)
        if dir_scope.get(rel_dir) == scope.TARGETED:
            port_dirs += 1
        else:
            wrap_dirs += 1
        for e in entries:
            kinds[e["kind"]] += 1
            if e.get("non_opaque_in") or e.get("opaque_in"):
                with_consumers += 1
    print(f"types: {len(entries_by_dir)} manifest dirs ({port_dirs} port, "
          f"{wrap_dirs} wrap), {total} entries "
          f"({with_consumers} with consumers) → {args.out_root}")
    for k, c in sorted(kinds.items()):
        print(f"  {k:<10} {c}")


if __name__ == "__main__":
    main()
