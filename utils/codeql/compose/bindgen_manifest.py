"""Deterministically scaffold the ``-sys`` FFI crates from the analysis tree.

This is the **bindgen** stage's composer half (no LLM). It partitions the
target's wrap-scope (FFI) surface by ``linked_in`` into one ``<lib>-sys``
crate per link artifact (``libssl-sys``, ``libcrypto-sys``, …) and emits,
per crate, everything that can be produced *without* invoking a compiler:

  - ``Cargo.toml``                     (write-if-absent; manual-edit artifact)
  - ``build.rs``                       bindgen invocation + ``wrap_static_fns``
                                       + ``cc`` for the macro shim TU, with the
                                       allowlists / opaque set / blocklist baked
                                       in inside ``crustify:allowlist`` markers
  - ``bindgen.h``                      the master include closure + the
                                       ``crustify:macros`` block where the agent
                                       fills `static inline crustify_<NAME>`
                                       shims (made linkable by wrap_static_fns)
  - ``src/lib.rs``                     re-export bindings + ``use <dep>_sys::*``
  - ``crustify-bindgen.json``          the agent's worklist (macros,
                                       non-opaque types to verify, const macros)

Everything that needs a build — ``cargo check``, inspecting ``bindings.rs``,
opaque/non-opaque fix-ups, ``macro_constant`` const-shim recovery — is the
**agent's** job and is intentionally NOT done here.

Scope + annotations
-------------------
The repo-root analysis tree is cumulative across targets, so scope comes from
the target's ``scope.json`` via the same ``syms``/``types`` composers the
analyze pipeline uses (``FilterSpec(scope_json_path=…)``). Those give the
per-target in-scope **identities**; the agent-filled **annotations** (the
``kind`` of each macro, ``linked_in``, ``opaque_in``/``non_opaque_in``,
``fields``) live only on disk, so we read the annotated entries back from the
analysis tree, intersected with the in-scope set.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from . import scope
from .filter_spec import FilterSpec
from .syms_manifest import compose as syms_compose
from .types_manifest import compose as types_compose

# Managed-block markers (same reconciliation contract as scaffold / bindgen.h).
_ALLOW_START = "// crustify:allowlist:start"
_ALLOW_END = "// crustify:allowlist:end"
_MACROS_H_START = "/* crustify:macros:start */"   # the macro-shim block in bindgen.h
_MACROS_H_END = "/* crustify:macros:end */"
_EXTRA_START = "/* crustify:extra-includes:start */"
_EXTRA_END = "/* crustify:extra-includes:end */"
_INC_START = "/* crustify:includes:start */"
_INC_END = "/* crustify:includes:end */"
# Agent-owned allowlist overrides in build.rs (seeded empty, preserved across
# composer regenerations). The agent adds opacity fix-ups here when bindgen's
# actual output needs them — the composer's own arrays are best-effort seeds.
_ALLOW_AGENT_START = "// crustify:allowlist-agent:start"
_ALLOW_AGENT_END = "// crustify:allowlist-agent:end"
_USE_START = "// crustify:foreign-use:start"
_USE_END = "// crustify:foreign-use:end"

# Real C types bindgen can emit (others are analyzer-synthetic pseudo-kinds).
# A `callback` is NOT here — it is a SYMBOL (function-pointer typedef in
# syms.json), allowlisted via the symbol loop below.
_BINDABLE_TYPE_KINDS = frozenset({"struct", "union", "enum"})
# Sym kinds → routing.
_BINDABLE_FUNCS = frozenset({"function_exported"})
_BINDABLE_VARS = frozenset({"global_extern", "macro_constant"})
# Only HEADER inlines are bindgen'd. A `function_inline_tu` (inline defined in a
# .c) has internal linkage — callable only within its own TU — so a Rust caller
# can arise only inside the ported version of that same TU, by which point
# dep-order porting has already brought the inline in as Rust. It can never be
# FFI-needed (and the syms composer already excludes it from wrap output via
# `_WRAP_DISALLOWED_FN_KINDS`); listing it here would only emit a dead
# wrap_static_fns wrapper. Same TU-local reasoning as `global_static` below.
_INLINE_KINDS = frozenset({"function_inline_header"})
# Header-template suffixes that resolve to a generated `.h` in a configured
# tree (e.g. OpenSSL's `Configure` expands `opensslv.h.in` → `opensslv.h`).
_HEADER_TEMPLATE_SUFFIXES = (".h.in",)
_MACRO_SHIM_KINDS = frozenset({"macro_symbol", "macro_misc"})
# NOTE: file-local `static` globals (`global_static`) get NO bindgen treatment.
# A static is name-visible only in its own TU, so it appears in no header and
# cannot enter the header-narrowed wrap closure; and within a port file the
# deps-dag orders it below its users, so it is always ported before anything
# depends on it. It can therefore never be wrap-scope — no accessor shim is ever
# needed. (Would only resurface under sub-file/per-function port granularity.)

# C primitives / qualifiers stripped when extracting a referenced type tag.
_NON_TAG = frozenset({
    "const", "volatile", "struct", "union", "enum", "unsigned", "signed",
    "void", "char", "short", "int", "long", "float", "double", "_Bool",
    "size_t", "ssize_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "intptr_t", "uintptr_t",
})


def _sys_crate(lib: str) -> str:
    return f"{lib}-sys"


def _sys_mod(lib: str) -> str:
    return f"{lib}-sys".replace("-", "_")


def _ref_tags(type_str: str | None) -> list[str]:
    """Extract candidate user-type tags from a C type string (field/arg type)."""
    if not type_str:
        return []
    s = re.sub(r"[*\[\]()]", " ", type_str)
    return [t for t in s.split()
            if re.match(r"^[A-Za-z_]\w*$", t) and t not in _NON_TAG]


# ------------------------------------------------------------------ data model

class LibPlan:
    """Everything one ``<lib>-sys`` crate needs."""

    __slots__ = (
        "lib", "allow_types", "allow_funcs", "allow_vars",
        "opaque_types", "nonopaque_types", "includes",
        "macro_worklist", "const_macros",
        "foreign_libs", "blocklist",
    )

    def __init__(self, lib: str) -> None:
        self.lib = lib
        self.allow_types: set[str] = set()      # tags + typedef aliases
        self.allow_funcs: set[str] = set()
        self.allow_vars: set[str] = set()
        self.opaque_types: set[str] = set()     # tag only (fully opaque)
        self.nonopaque_types: set[str] = set()  # tag only (layout required)
        self.includes: set[str] = set()         # header paths for bindgen.h
        self.macro_worklist: list[dict] = []
        self.const_macros: set[str] = set()     # for agent const-shim recovery
        self.foreign_libs: set[str] = set()     # other libs whose types we ref
        self.blocklist: set[str] = set()        # foreign tags+typedefs to NOT
                                                # emit (imported via use <dep>)
                                                # (absolute, from build.json)


class Plan(NamedTuple):
    libs: dict[str, LibPlan]


class Stats(NamedTuple):
    libs: int
    files_written: int
    skipped_existing: int


# ---------------------------------------------------------------------- compose

def _load_inscope_annotated(
    by_dir: dict[Path, list[dict]],
    analysis_root: Path,
    filename: str,
    coll_key: str,
    id_key: str,
    *,
    keys: set | None,
) -> list[dict]:
    """Return on-disk *annotated* entries for the composer's in-scope set.

    ``by_dir`` comes from a ``*_compose`` call (the per-target in-scope
    identities, grouped by dir). Scope membership is read from ``scope.json``
    (the authoritative, deduped closure) as ``keys`` — the origin-keyed set from
    :func:`scope.scope_membership` — keep an entry iff its
    ``scope.origin_key(id, defined_in, declared_in)`` is in ``keys``. ``keys`` is
    built ``synthetic=False`` for the FFI surface (bindgen binds real C entities
    only — never the string/array synthetics). ``None`` keeps every
    in-scope-by-dir entry.
    """
    out: list[dict] = []
    for mdir, entries in by_dir.items():
        inscope_ids = {e.get(id_key) for e in entries}
        disk = analysis_root / mdir / filename
        if not disk.exists():
            continue
        try:
            doc = json.loads(disk.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get(coll_key, []):
            eid = e.get(id_key)
            if eid not in inscope_ids:
                continue
            if keys is not None and scope.origin_key(
                    eid, e.get("defined_in"), e.get("declared_in")) not in keys:
                continue
            out.append(e)
    return out


def _crate_index(repo_root: Path | None) -> dict[str, list[tuple[str, str | None, set]]]:
    """``crates.json`` member name -> [(crate, def_file, decl_files_set)].

    crates.json (``repo_root/crustify/crates.json``, authored by the
    scaffolder, populated before bindgen runs) is the placement authority: its
    crate names ARE the link-unit / ``linked_in`` library keys (``libssl`` /
    ``libcrypto`` / ``libc``). We index every ``.rs`` member so an entity
    resolves to its owning crate (== library) — replacing the per-entity
    ``linked_in`` field. Empty when no repo_root / crates.json (callers fall
    back to the entry's own ``linked_in``)."""
    idx: dict[str, list[tuple[str, str | None, set]]] = defaultdict(list)
    if repo_root is None:
        return idx
    path = repo_root / "crustify" / "crates.json"
    if not path.exists():
        return idx
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return idx
    for crate, c in (doc.get("crates") or {}).items():
        for m in (c.get("modules") or {}).values():
            for r in (m.get("rs") or {}).values():
                df = r.get("def_file")
                decls = set(r.get("decl_files") or [])
                for names in (r.get("members") or {}).values():
                    for nm in names or []:
                        idx[nm].append((crate, df, decls))
    return idx


def _crate_depends_on(repo_root: Path | None) -> dict[str, set[str]]:
    """``crates.json`` crate -> set of its declared ``depends_on`` crates — the
    authored link-dependency graph (a ``-sys`` crate's foreign deps come straight
    from here, not from re-deriving them by scanning type references). Empty when
    no repo_root / crates.json."""
    out: dict[str, set[str]] = {}
    if repo_root is None:
        return out
    path = repo_root / "crustify" / "crates.json"
    if not path.exists():
        return out
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return out
    for crate, c in (doc.get("crates") or {}).items():
        out[crate] = set(c.get("depends_on") or [])
    return out


def _lib_of(
    idx: dict[str, list[tuple[str, str | None, set]]],
    name: str,
    def_file: str | None,
    decl_files: list[str] | None,
    fallback: str | None,
) -> str | None:
    """Owning crate (== library) of an entity from the crates.json index,
    disambiguated by ``def_file`` then ``decl_file``; ``fallback`` (the entry's
    own ``linked_in``, still present on symbols) when crates.json has no match —
    so coverage never regresses against the old field."""
    cands = idx.get(name)
    if not cands:
        return fallback
    if def_file:
        for crate, df, _decls in cands:
            if df == def_file:
                return crate
    for dh in decl_files or []:
        for crate, _df, decls in cands:
            if dh in decls:
                return crate
    return cands[0][0]


def compose(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    analysis_root: Path,
    filter_spec: FilterSpec | None = None,
    *,
    lib_filter: list[str] | None = None,
    repo_root: Path | None = None,
) -> Plan:
    """Build the per-``<lib>-sys`` bindgen plan for ONE target.

    Args:
      csv_dir_t1/t2: repo-root CodeQL CSV dirs (scope + reach).
      analysis_root: ``<repo_root>/.crustify/analysis`` (annotation source).
      filter_spec: pass ``FilterSpec(scope_json_path=<target scope.json>)``.
      lib_filter: optional ``linked_in`` restriction (post-scoping).
      repo_root: the repository root; used to resolve per-library
        ``include_dirs`` from ``<repo_root>/.crustify/build.json`` into
        absolute bindgen ``-I`` clang args. When None, clang args are
        omitted (the agent's cargo-check loop surfaces the gap).
    """
    if filter_spec is None:
        filter_spec = FilterSpec()

    syms_by_dir, _syms_scope, _ = syms_compose(csv_dir_t1, csv_dir_t2, filter_spec)
    types_by_dir, _types_scope, _ = types_compose(csv_dir_t1, csv_dir_t2, filter_spec)

    # FFI surface = the WRAP closure from scope.json (authoritative, deduped),
    # EXCLUDING synthetics (string/array clusters are Rust abstractions, not
    # C entities bindgen can bind). Keyed (name|type, defined_in); sym and type
    # buckets resolved separately so a name can't cross-match the wrong kind.
    sj = filter_spec.scope_json_path
    wrap_sym_keys = (scope.scope_membership(
        sj, "wrap", kinds=("functions", "globals", "macros"), synthetic=False)
        if sj else None)
    wrap_type_keys = (scope.scope_membership(
        sj, "wrap", kinds=("types",), synthetic=False) if sj else None)
    wrap_syms = _load_inscope_annotated(
        syms_by_dir, analysis_root, "syms.json", "symbols", "name",
        keys=wrap_sym_keys,
    )
    wrap_types = _load_inscope_annotated(
        types_by_dir, analysis_root, "types.json", "types", "type",
        keys=wrap_type_keys,
    )

    # Alias → owning lib, and alias → all-names (tag + typedefs), over wrap
    # types only (these are the bindgen'd types). The all-names map lets us
    # blocklist *every* spelling of a foreign type so bindgen emits none of
    # them and the `use <dep>_sys::*` import is unambiguous.
    # Library routing is now crates.json-driven (crate name == linked_in key);
    # the entry's own `linked_in` is only a fallback (types no longer carry it).
    crate_idx = _crate_index(repo_root)

    alias_to_lib: dict[str, str] = {}
    alias_to_names: dict[str, set[str]] = {}
    for t in wrap_types:
        lib = _lib_of(crate_idx, t["type"], t.get("defined_in"),
                      t.get("declared_in"), t.get("linked_in"))
        if not lib:
            continue
        names = {t["type"], *(t.get("typedef") or [])}
        for n in names:
            alias_to_lib[n] = lib
            alias_to_names[n] = names

    def note_foreign(lp: "LibPlan", type_str: str | None) -> None:
        """Record any foreign-owned types referenced by a field/arg string:
        add the owning lib as a dep and blocklist every spelling of the type."""
        for ref in _ref_tags(type_str):
            owner = alias_to_lib.get(ref)
            if owner and owner != lp.lib:
                lp.foreign_libs.add(owner)
                lp.blocklist |= alias_to_names.get(ref, {ref})

    want = set(lib_filter) if lib_filter else None
    libs: dict[str, LibPlan] = {}

    def plan_for(lib: str) -> LibPlan | None:
        if want is not None and lib not in want:
            return None
        if lib not in libs:
            libs[lib] = LibPlan(lib)
        return libs[lib]

    # ---- types ----
    for t in wrap_types:
        lib = _lib_of(crate_idx, t["type"], t.get("defined_in"),
                      t.get("declared_in"), t.get("linked_in"))
        if not lib:
            continue
        lp = plan_for(lib)
        if lp is None:
            continue
        tag = t["type"]
        kind = t.get("kind")
        if kind in _BINDABLE_TYPE_KINDS:
            lp.allow_types.add(tag)
            for td in t.get("typedef") or []:
                lp.allow_types.add(td)
            # opaque vs layout-required, from the consumer footprint (a
            # field-access heuristic, meaningful only for aggregates).
            if t.get("non_opaque_in"):
                lp.nonopaque_types.add(tag)
            else:
                lp.opaque_types.add(tag)
        # Synthetic generic instantiations (STACK_OF/LHASH) are NOT explicitly
        # allowlisted — bindgen pulls them in transitively (as incomplete,
        # pointer-only tags) from the structs that reference them.
        #
        # Include EVERY declaring header (same as the symbol paths below) — the
        # `#include` set is a union, so bindgen dedups and a port gets the full
        # struct definition wherever its body-bearing header lives, not a lone
        # forward handle. No canonical pick: all decl headers go in.
        for h in t.get("declared_in") or []:
            _add_header(lp, h)
        _add_header(lp, t.get("defined_in"))
        # Foreign-owned field types → dep + blocklist (dedup across -sys).
        for f in t.get("fields") or []:
            note_foreign(lp, f.get("type"))

    # ---- symbols ----
    for s in wrap_syms:
        lib = _lib_of(crate_idx, s["name"], s.get("defined_in"),
                      s.get("declared_in"), s.get("linked_in"))
        if not lib:
            continue
        lp = plan_for(lib)
        if lp is None:
            continue
        name, kind = s["name"], s.get("kind")
        if kind in _BINDABLE_FUNCS:
            lp.allow_funcs.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind in _BINDABLE_VARS:
            lp.allow_vars.add(name)
            if kind == "macro_constant":
                lp.const_macros.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind in _INLINE_KINDS:
            # bindgen `wrap_static_fns` only wraps allowlisted static/inline
            # functions — so the inline must be in ALLOWED_FUNCTIONS too (not
            # just have its header included), else wrap_static_fns silently
            # emits nothing for it.
            lp.allow_funcs.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind in _MACRO_SHIM_KINDS:
            lp.macro_worklist.append(_worklist_macro(s))
            _add_header(lp, s.get("defined_in"))
        elif kind == "callback":
            # A callback (function-pointer typedef) is bindgen-emitted as a
            # type. It's always NON-OPAQUE: "opaque" would emit a size-matched
            # blob, discarding the signature — the one thing we need (and cheap,
            # its args are pointers pulling in no layout). defined_in is null
            # (a header typedef), so routing rides on the declaring headers.
            lp.allow_types.add(name)
            lp.nonopaque_types.add(name)
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        # Foreign refs from function signatures (arg/ret pointer types).
        for pa in s.get("ptr_args") or []:
            note_foreign(lp, pa.get("type"))
        pr = s.get("ptr_ret")
        if pr:
            note_foreign(lp, pr.get("type"))

    # ---- synthetic clusters → their member C functions ----
    # A string/array cluster's TYPE is a Rust abstraction (`CStr`/`CVec`) bindgen
    # never binds — so it is correctly excluded from the wrap-type seed above. But
    # its `ctors`/`dtor`/`ops` are REAL C functions the wrapper calls through
    # `ffi::`, and the wrap-FUNCTION seed (synthetic=False) only catches those that
    # are ALSO independent wrap-scope functions. A cluster op reachable *only* via
    # the cluster (e.g. `git__strndup` / `git__substrdup`, members of
    # `git__strdup_string` but not standalone wrap functions) would otherwise be
    # absent from ALLOWED_FUNCTIONS and bindgen would never emit it, leaving the
    # wrapper's ctor unbindable. The clusters aren't in scope.json (a dag/analysis
    # concept) and are always wrap-scope, so source them straight from the
    # analysis types tree and allowlist their member functions + declaring headers.
    for tj in sorted(analysis_root.rglob("types.json")):
        try:
            doc = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        for c in doc.get("types") or []:
            if c.get("kind") not in scope.SYNTHETIC_KINDS:
                continue
            lib = _lib_of(crate_idx, c["type"], c.get("defined_in"),
                          c.get("declared_in"), c.get("linked_in"))
            if not lib:
                continue
            lp = plan_for(lib)
            if lp is None:
                continue
            lc = scope.lifetime(c)
            fns = set(lc.get("ctors") or []) | set(c.get("ops") or [])
            dt = lc.get("dtor") or {}
            for k in ("storage", "fields"):
                if dt.get(k):
                    fns.add(dt[k])
            lp.allow_funcs |= fns
            # The cluster's declaring header(s) hold these functions' prototypes.
            for h in c.get("declared_in") or []:
                _add_header(lp, h)
            _add_header(lp, c.get("defined_in"))

    # NOTE: no explicit "layout closure" is needed. bindgen's allowlist is
    # transitive — allowlisting a struct pulls in (and lays out, from the
    # included headers) every type it references, recursively. And a
    # blocklisted type used *by value* is still sized correctly from its C
    # definition (bindgen just doesn't emit a Rust copy; the field references
    # the imported `use <dep>_sys::*` type). An earlier hand-rolled closure
    # here was a workaround for a bindgen 0.70 opacity bug, now fixed in 0.72;
    # it has been removed (it also wrongly duplicated foreign value types).

    # Foreign-dep attribution (crates.json `depends_on`). `note_foreign` above
    # only sees types referenced by THIS crate's wrap entities' signatures/
    # fields — it misses a foreign type embedded by a PORT struct (e.g.
    # `git_odb.lock: pthread_mutex_t`; `git_odb` is port, never iterated). So
    # take the dep set straight from the authored crate graph and blocklist the
    # wrap types homed to those deps, so bindgen imports them from the dep's
    # `-sys` instead of re-minting them locally. Filtered to deps that actually
    # emit a `-sys` (an empty foreign crate — e.g. libz here — produces no crate
    # to depend on). `alias_to_lib`/`alias_to_names` already map every wrap type
    # to its owning crate + all spellings (built over the field-walk-complete
    # wrap closure).
    emitted = set(libs)
    crate_deps = _crate_depends_on(repo_root)
    for lib, lp in libs.items():
        fdeps = (crate_deps.get(lib) or set()) & emitted
        lp.foreign_libs |= fdeps
        for tag, owner in alias_to_lib.items():
            if owner in fdeps:
                lp.blocklist |= alias_to_names.get(tag, {tag})

    return Plan(libs=libs)


def _add_header(lp: LibPlan, header: str | None) -> None:
    if header and header.endswith((".h", *_HEADER_TEMPLATE_SUFFIXES)):
        lp.includes.add(header)


def _worklist_macro(s: dict) -> dict:
    return {
        "name": s["name"],
        "kind": s.get("kind"),
        "defined_in": s.get("defined_in"),
        "declared_in": s.get("declared_in"),
        "used_by": s.get("used_by"),
        "depends_on": s.get("depends_on"),
    }


# ---------------------------------------------------------------------- writing

def _merge_block(path: Path, start: str, end: str, header: str,
                 body: str = "") -> bool:
    """Create ``path`` with a managed ``start``/``end`` block, or leave an
    existing file untouched (the agent owns its block contents). Returns True
    when created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    block = f"{start}\n{body}{end}\n" if body else f"{start}\n{end}\n"
    path.write_text(header + "\n\n" + block)
    return True


_TRAILING_INCLUDES = (
    '#include "bindgen_extra.h"',
)


def _seed_include_order(includes: set[str]) -> list[str]:
    """First-run seed: the headers in a stable (sorted) but otherwise
    arbitrary order. Header dependency order is target-specific and
    naming-sensitive (it can't be inferred generically), so the composer
    does NOT attempt any semantic ordering — the agent reorders the
    agent-owned ``crustify:includes`` block on the first build error."""
    return [f'#include "{_inc(h)}"' for h in sorted(includes)]


def _bindgen_h(lp: LibPlan, existing: str | None) -> str:
    """Render bindgen.h. The ``#include`` list lives in an agent-owned
    ``crustify:includes`` block: header dependency order *and membership* are
    dependency-sensitive and need judgement (the agent may reorder, or drop a
    header that's double-included transitively). The composer seeds the block
    on first creation only and never modifies it afterwards. Everything else
    is composer-owned.
    """
    # Recover the agent's current include block: from the managed markers if
    # present, else (migration) from a marker-less file's include lines.
    existing_lines: list[str] = []
    if existing:
        if _INC_START in existing and _INC_END in existing:
            body = existing.split(_INC_START, 1)[1].split(_INC_END, 1)[0]
            existing_lines = [l.strip() for l in body.splitlines()
                              if l.strip().startswith("#include")]
        else:
            existing_lines = [
                l.strip() for l in existing.splitlines()
                if l.strip().startswith("#include")
                and l.strip() not in _TRAILING_INCLUDES
            ]

    if existing_lines:
        # Preserve the agent's block **verbatim** — it owns ordering AND
        # membership (it may have removed a header that's double-included
        # transitively, e.g. record.h via ssl_local.h, or added one). An
        # additive merge would fight those edits, so the composer never
        # re-adds/reorders once the block exists. New in-scope headers from
        # a later scope expansion are the agent's to add during its verify
        # loop.
        block_lines = existing_lines
    else:
        block_lines = _seed_include_order(lp.includes)

    # Macro shims live in an agent-owned block AFTER every include (so each
    # macro is in scope): `static inline RET crustify_<NAME>(...) { return
    # NAME(...); }`. build.rs `wrap_static_fns` makes them linkable, and the
    # `crustify_.*` allowlist binds them — so no separate bindgen_macros.{h,c}.
    # Preserved verbatim across regenerations (the composer seeds it empty).
    macro_body = ""
    if existing and _MACROS_H_START in existing and _MACROS_H_END in existing:
        macro_body = existing.split(_MACROS_H_START, 1)[1] \
                             .split(_MACROS_H_END, 1)[0].strip("\n")

    lines = [
        f"/* {_sys_crate(lp.lib)} bindgen master header. */",
        "/* The include list below is an agent-owned block: the agent may "
        "add,", "   remove, or reorder #include lines to make the closure "
        "parse (e.g.", "   prepend <stdint.h> for missing int types, drop a "
        "transitively", "   double-included header). The composer seeds it "
        "once and never", "   edits it afterward. Clang -I paths live in "
        "AGENT_CLANG_ARGS in", "   build.rs; the symbol/type allowlist is "
        "composer-owned there. */",
        "",
        _INC_START,
        *block_lines,
        _INC_END,
        "",
        *_TRAILING_INCLUDES,
        "",
        "/* Macro shims (`static inline crustify_<NAME>` over header macros, made",
        "   linkable by build.rs wrap_static_fns) + const-shim recovery. "
        "Agent-filled. */",
        _MACROS_H_START,
        *([macro_body] if macro_body else []),
        _MACROS_H_END,
    ]
    return "\n".join(lines) + "\n"


def _inc(header: str) -> str:
    # Template headers resolve to their generated form in a configured
    # tree (OpenSSL's `Configure` turns `foo.h.in` into `foo.h`).
    for suf in _HEADER_TEMPLATE_SUFFIXES:
        if header.endswith(suf):
            return header[: -len(suf)] + ".h"
    return header


def _rust_arr(name: str, items: set[str]) -> str:
    body = "".join(f'    "{x}",\n' for x in sorted(items))
    return f"const {name}: &[&str] = &[\n{body}];\n"


_AGENT_BLOCK_COMMENT = (
    "// Agent-owned. The composer emits NO best-effort seed for these:\n"
    "//   - AGENT_CLANG_ARGS: every bindgen/cc `-I` include path (and any\n"
    "//     other clang flag). Include-path discovery — repo root, generated\n"
    "//     headers (e.g. a CMake `build/` tree), out-of-tree deps, sysroot —\n"
    "//     is a compiler-in-the-loop search the agent owns via its verify\n"
    "//     loop; build.json `include_dirs` is only a hint.\n"
    "//   - AGENT_ALLOWED_TYPES / AGENT_OPAQUE_TYPES / AGENT_BLOCKLIST:\n"
    "//     opacity fix-ups added when `cargo check` + bindings.rs show a\n"
    "//     struct opaque/missing. The composer never edits this block.\n"
    "//   - AGENT_LINK_ARGS: the native-library link directives (per target).\n"
    "//     Each entry is a `cargo:` directive body — e.g.\n"
    "//     `rustc-link-lib=dylib=git2`, `rustc-link-search=native={repo}/build`\n"
    "//     (`{repo}` expands to the repo root). Determined from build.json's\n"
    "//     link config / the built library's location, or left EMPTY when the\n"
    "//     staticlib is linked into the C build and needs no standalone link.\n"
)


def _agent_block(existing: str | None) -> str:
    """Render the agent-owned block, preserving any values an earlier agent
    run wrote and seeding newly-introduced arrays empty.

    Reconstructed from the parsed arrays (rather than copied verbatim) so a
    crate scaffolded before a new agent array existed picks it up on the
    next regen — otherwise build.rs would reference an undefined const."""
    return (
        _ALLOW_AGENT_START + "\n"
        + _AGENT_BLOCK_COMMENT
        + _rust_arr("AGENT_ALLOWED_TYPES", _existing_arr(existing, "AGENT_ALLOWED_TYPES"))
        + _rust_arr("AGENT_OPAQUE_TYPES", _existing_arr(existing, "AGENT_OPAQUE_TYPES"))
        + _rust_arr("AGENT_BLOCKLIST", _existing_arr(existing, "AGENT_BLOCKLIST"))
        + _rust_arr("AGENT_CLANG_ARGS", _existing_arr(existing, "AGENT_CLANG_ARGS"))
        + _rust_arr("AGENT_LINK_ARGS", _existing_arr(existing, "AGENT_LINK_ARGS"))
        + _ALLOW_AGENT_END + "\n"
    )


def _existing_arr(existing: str | None, name: str) -> set[str]:
    """Parse a composer-owned `const NAME: &[&str] = &[ … ];` array from an
    existing build.rs, so cross-target runs can UNION rather than clobber a
    shared -sys crate's allowlist."""
    if not existing:
        return set()
    m = re.search(r"const " + re.escape(name) + r":\s*&\[&str\]\s*=\s*&\[(.*?)\];",
                  existing, re.S)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def _build_rs(lp: LibPlan, existing: str | None = None) -> str:
    # Cross-target additive: a shared -sys crate's allowlist is the UNION of
    # every target's wrap surface in that library — never shrink it.
    ex_allowed = _existing_arr(existing, "ALLOWED_TYPES")
    ex_opaque = _existing_arr(existing, "OPAQUE_TYPES")
    allow_types = lp.allow_types | ex_allowed
    allow_funcs = lp.allow_funcs | _existing_arr(existing, "ALLOWED_FUNCTIONS")
    allow_vars = lp.allow_vars | _existing_arr(existing, "ALLOWED_VARS")
    # A type stays opaque only if NO target needs its layout. The set of
    # types a prior run wanted non-opaque is (its ALLOWED − its OPAQUE).
    ex_nonopaque = ex_allowed - ex_opaque
    opaque_types = ((ex_opaque | lp.opaque_types)
                    - lp.nonopaque_types - ex_nonopaque)
    blocklist = lp.blocklist | _existing_arr(existing, "BLOCKLIST_FOREIGN")
    allow = (
        _ALLOW_START + "\n"
        + _rust_arr("ALLOWED_TYPES", allow_types)
        + _rust_arr("ALLOWED_FUNCTIONS", allow_funcs)
        + _rust_arr("ALLOWED_VARS", allow_vars)
        + _rust_arr("OPAQUE_TYPES", opaque_types)
        + _rust_arr("BLOCKLIST_FOREIGN", blocklist)
        + _ALLOW_END + "\n"
    )
    agent = _agent_block(existing)
    return f'''//! Generated by crustify bindgen. The {_ALLOW_START[3:]} block is
//! composer-owned (best-effort seed); the {_ALLOW_AGENT_START[3:]} block is
//! agent-owned and preserved across regenerations. Edit includes via bindgen.h.
use std::path::{{Path, PathBuf}};

{allow}
{agent}
/// Resolve `AGENT_CLANG_ARGS` into absolute, **location-independent** include
/// flags. Repo-relative `-I` tokens are joined to the repo root derived from
/// `CARGO_MANIFEST_DIR` (never the CWD/worktree — so a path recorded by an agent
/// running inside an isolated git worktree still resolves after that worktree is
/// pruned), and the crate-local stable generated headers (`.gen-headers`) are
/// prepended. Non-`-I` flags pass through. Composer-owned and identical for every
/// -sys crate: agents only ever record RELATIVE `-I` tokens in AGENT_CLANG_ARGS,
/// never absolute paths.
fn resolved_clang_args() -> Vec<String> {{
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    // <crate> -> rust -> crustify -> repo root
    let repo_root = manifest.ancestors().nth(3).unwrap().to_path_buf();
    let mut args = vec![format!("-I{{}}", manifest.join(".gen-headers").display())];
    for a in AGENT_CLANG_ARGS {{
        match a.strip_prefix("-I") {{
            Some(p) if !Path::new(p).is_absolute() =>
                args.push(format!("-I{{}}", repo_root.join(p).display())),
            _ => args.push((*a).to_string()),
        }}
    }}
    args
}}

fn main() {{
    println!("cargo:rerun-if-changed=bindgen.h");

    // Native-library link flags are AGENT-OWNED, per target (see
    // `AGENT_LINK_ARGS` in the crustify:allowlist-agent block). Each entry is a
    // `cargo:` directive body; the literal token `{{repo}}` expands to the repo
    // root, so a relative `rustc-link-search` an agent records inside an
    // isolated worktree stays valid after the worktree is pruned. Leave EMPTY
    // when the staticlib is linked into the C build — which provides the native
    // symbols from its own objects — and no standalone Rust link is wanted.
    {{
        let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
        let repo = manifest.ancestors().nth(3).unwrap().display().to_string();
        for a in AGENT_LINK_ARGS {{
            println!("cargo:{{}}", a.replace("{{repo}}", &repo));
        }}
    }}

    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let clang_args = resolved_clang_args();

    let mut builder = bindgen::Builder::default()
        .header("bindgen.h")
        .wrap_static_fns(true)
        // Pin the generated extern wrappers next to OUT_DIR so the cc step
        // below finds them; bindgen's default is a temp dir, which left cc with
        // no input once the bindgen_macros.c shim TU was removed.
        .wrap_static_fns_path(out.join("extern.c"))
        .layout_tests(false)
        .derive_default(false);
    // Clang include paths (-I) are agent-owned: the agent discovers WHICH dirs
    // in its verify loop and records them as relative tokens in AGENT_CLANG_ARGS;
    // resolved_clang_args() makes them absolute & location-independent.
    for a in &clang_args {{ builder = builder.clang_arg(a.as_str()); }}
    for t in ALLOWED_TYPES {{ builder = builder.allowlist_type(t); }}
    for f in ALLOWED_FUNCTIONS {{ builder = builder.allowlist_function(f); }}
    // Agent-emitted FFI shims/accessors/const-shims live behind this prefix;
    // without it the closed allowlist filters every wrapper out of bindings.rs.
    builder = builder.allowlist_function("crustify_.*");
    for v in ALLOWED_VARS {{ builder = builder.allowlist_var(v); }}
    // Fully-opaque types: keep the newtype, drop the layout. Under a closed
    // allowlist each opaque tag must also be allowlisted (already above).
    for t in OPAQUE_TYPES {{ builder = builder.opaque_type(t); }}
    // Types owned by a dependency -sys crate: don't re-emit; import via
    // `pub use <dep>_sys::*` in src/lib.rs (one Rust type per C type).
    for t in BLOCKLIST_FOREIGN {{ builder = builder.blocklist_type(t); }}
    // Agent-owned opacity fix-ups (preserved across composer regenerations).
    for t in AGENT_ALLOWED_TYPES {{ builder = builder.allowlist_type(t); }}
    for t in AGENT_OPAQUE_TYPES {{ builder = builder.opaque_type(t); }}
    for t in AGENT_BLOCKLIST {{ builder = builder.blocklist_type(t); }}

    let bindings = builder.generate().expect("bindgen failed");
    bindings.write_to_file(out.join("bindings.rs")).unwrap();

    // Compile the wrap_static_fns-generated extern wrappers (the macro shims'
    // `static inline crustify_<NAME>` in bindgen.h are wrapped into here). Only
    // when wrap_static_fns actually emitted an extern.c — a crate with no
    // wrapped static/inline fns has nothing to compile, and calling cc.compile
    // with no input files fails on an empty `ar` archive.
    let extern_c = out.join("extern.c");
    if extern_c.exists() {{
        let mut cc = cc::Build::new();
        cc.file(&extern_c);
        cc.include(".");
        for a in &clang_args {{ cc.flag(a.as_str()); }}
        cc.compile("{lp.lib}_shims");
    }}
}}
'''


def _link_name(lib: str) -> str:
    return lib[3:] if lib.startswith("lib") else lib


def _lib_rs(lp: LibPlan) -> str:
    uses = "".join(
        f"pub use {_sys_mod(f)}::*;\n" for f in sorted(lp.foreign_libs)
    )
    return f'''//! FFI bindings for {lp.lib} (generated skeleton).
#![allow(non_snake_case, non_camel_case_types, non_upper_case_globals)]
#![allow(dead_code, improper_ctypes)]
// Generated bindgen output — not ours to lint; clippy on it is noise that
// otherwise cascades into every crate that runs `cargo clippy -- -D warnings`.
#![allow(clippy::all)]

{_USE_START}
{uses}{_USE_END}

include!(concat!(env!("OUT_DIR"), "/bindings.rs"));
'''


def _cargo_toml(lp: LibPlan) -> str:
    deps = "".join(
        f'{_sys_crate(f)} = {{ path = "../{_sys_crate(f)}" }}\n'
        for f in sorted(lp.foreign_libs)
    )
    return f'''[package]
name = "{_sys_crate(lp.lib)}"
version = "0.0.0"
edition = "2024"
links = "{_link_name(lp.lib)}"

[dependencies]
{deps}
[build-dependencies]
bindgen = {{ version = "0.72", features = ["experimental"] }}
cc = "1.0"
'''


def _merge_cargo_deps(existing: str | None, lp: LibPlan) -> str:
    """Reconcile the foreign-lib ``[dependencies]`` lines into an existing
    Cargo.toml without clobbering manual edits.

    Cargo.toml is a manual-edit artifact (versions, extra deps), so we never
    regenerate it wholesale once present. But ``foreign_libs`` can grow on a
    re-run (a target reaches a type owned by another lib), and ``src/lib.rs``
    *is* regenerated with the matching ``pub use <dep>_sys::*`` — leaving an
    unresolved import if the dep line is missing. So we additively insert any
    ``<dep>-sys = { path = … }`` line not already present under
    ``[dependencies]``, preserving everything else verbatim.
    """
    if existing is None:
        return _cargo_toml(lp)
    want = {_sys_crate(f): f'{_sys_crate(f)} = {{ path = "../{_sys_crate(f)}" }}'
            for f in sorted(lp.foreign_libs)}
    missing = [line for crate, line in want.items()
               if not re.search(rf"^\s*{re.escape(crate)}\s*=", existing,
                                re.MULTILINE)]
    if not missing:
        return existing
    lines = existing.splitlines()
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.strip() == "[dependencies]":
            out.extend(missing)
            inserted = True
    if not inserted:                      # no [dependencies] section — append one
        out += ["", "[dependencies]", *missing]
    return "\n".join(out) + "\n"


def _worklist_json(lp: LibPlan) -> str:
    doc = {
        "library": lp.lib,
        "macros": lp.macro_worklist,
        "non_opaque_types": sorted(lp.nonopaque_types),
        "const_macros": sorted(lp.const_macros),
        "foreign_libs": sorted(lp.foreign_libs),
    }
    return json.dumps(doc, indent=2) + "\n"


def write_plan(plan: Plan, rust_root: Path) -> Stats:
    crates_root = rust_root  # -sys crates live directly under the shared rust/
    written = skipped = 0

    def emit(path: Path, content: str, *, overwrite: bool) -> None:
        nonlocal written, skipped
        if path.exists() and not overwrite:
            skipped += 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written += 1

    for lib, lp in sorted(plan.libs.items()):
        root = crates_root / _sys_crate(lib)
        # Manual-edit artifact: created once, then only the foreign-lib
        # `[dependencies]` lines are reconciled additively (lib.rs's matching
        # `pub use <dep>_sys::*` is regenerated, so the dep must stay in sync).
        ct = root / "Cargo.toml"
        emit(ct, _merge_cargo_deps(ct.read_text() if ct.exists() else None, lp),
             overwrite=True)
        # Generated each run — composer-owned, except the agent-owned
        # `crustify:allowlist-agent` block which is preserved verbatim.
        brs = root / "build.rs"
        emit(brs, _build_rs(lp, brs.read_text() if brs.exists() else None),
             overwrite=True)
        # bindgen.h: composer-owned shell, but its include list is an
        # agent-owned additive block — preserve the agent's ordering.
        bh = root / "bindgen.h"
        emit(bh, _bindgen_h(lp, bh.read_text() if bh.exists() else None),
             overwrite=True)
        emit(root / "src" / "lib.rs", _lib_rs(lp), overwrite=True)
        emit(root / "crustify-bindgen.json", _worklist_json(lp), overwrite=True)
        # Agent-owned managed blocks — created empty, never clobbered.
        if _merge_block(root / "bindgen_extra.h", _EXTRA_START, _EXTRA_END,
                        f"/* {_sys_crate(lib)} opaque/non-opaque fix-up "
                        "includes added by the bindgen agent. */"):
            written += 1
        else:
            skipped += 1
        # Macro shims now live in bindgen.h's own crustify:macros block (see
        # _bindgen_h) — no separate bindgen_macros.{h,c} files. File-local
        # statics get no accessor (see _GLOBAL_SHIM note) — no bindgen_globals.h.

    # Register the -sys crates in the shared repo-root workspace (additive
    # with the scaffold source crates already present).
    from .scaffold_manifest import sync_workspace
    sync_workspace(rust_root)
    return Stats(libs=len(plan.libs), files_written=written,
                 skipped_existing=skipped)


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scaffold the -sys FFI crates for one target."
    )
    ap.add_argument("--t1", type=Path, required=True)
    ap.add_argument("--t2", type=Path, required=True)
    ap.add_argument("--analysis-root", type=Path, required=True)
    ap.add_argument("--scope-json", type=Path, required=True)
    ap.add_argument("--rust-root", type=Path, required=True)
    ap.add_argument("--libs", nargs="+", default=None,
                    help="Restrict to these linked_in libraries.")
    ap.add_argument("--repo-root", type=Path, default=None,
                    help="Repo root; resolves build.json include_dirs into "
                         "absolute bindgen -I clang args.")
    args = ap.parse_args()

    spec = FilterSpec(scope_json_path=args.scope_json)
    plan = compose(args.t1, args.t2, args.analysis_root, spec,
                   lib_filter=args.libs, repo_root=args.repo_root)
    stats = write_plan(plan, args.rust_root)
    for lib, lp in sorted(plan.libs.items()):
        print(f"  {_sys_crate(lib):16} types={len(lp.allow_types):4} "
              f"funcs={len(lp.allow_funcs):4} vars={len(lp.allow_vars):4} "
              f"opaque={len(lp.opaque_types):3} non_opaque={len(lp.nonopaque_types):3} "
              f"macros={len(lp.macro_worklist):3} "
              f"deps={sorted(lp.foreign_libs)}")
    print(f"bindgen: {stats.libs} -sys crate(s), {stats.files_written} file(s) "
          f"written, {stats.skipped_existing} preserved → {args.rust_root}")


if __name__ == "__main__":
    main()
