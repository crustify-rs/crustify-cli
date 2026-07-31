"""Deterministically scaffold the ``-sys`` FFI crates from the analysis tree.

The **bindgen** stage is composer-only (no LLM). It partitions the target's
wrap-scope (FFI) surface by owning crate into one ``<lib>-sys`` crate per
link artifact (``libssl-sys``, ``libcrypto-sys``, …) and emits, per crate,
only what the analysis tree already states as fact:

  - ``Cargo.toml``   (write-if-absent; manual-edit artifact) name, ``links``,
                     the ``<dep>-sys`` path deps, the build-deps
  - ``build.rs``     an INCOMPLETE scaffold: the per-kind allowlists and the
                     foreign blocklist as ``const`` arrays inside
                     ``crustify:allowlist`` markers, plus the empty
                     ``crustify:allowlist-agent`` block. No ``fn main`` — the
                     bindgen/cc invocation is not generated (see below)
  - ``bindgen.h``    an INCOMPLETE scaffold: the seeded ``#include`` closure in
                     an owned ``crustify:includes`` block + the empty
                     ``crustify:shims`` block for ``static inline
                     crustify_<NAME>`` shims (made linkable by
                     ``wrap_static_fns``)
  - ``src/lib.rs``   re-export bindings + ``use <dep>_sys::*``

The build.rs BODY is not emitted. Writing out a fixed ``fn main`` (a
``bindgen::Builder`` chain, the ``-I`` resolution, the ``cc`` step for the
``wrap_static_fns`` output) hardcodes decisions that belong to whoever
finishes the crate against a real compiler — bindgen version, flag set,
include-path discovery, link directives. Baking a guess in is error-prone:
it reads as generated-and-correct while being neither. The composer states
WHAT to bind; HOW to bind it is not its call.

No type is marked **opaque**. Opacity was a per-type guess from a field-access
footprint; since bindgen.h includes every declaring header, a type is emitted
with whatever layout those headers give it, and forcing a size-matched blob
only discarded information the headers already carried. Types come out however
the include closure pulls them.

MACROS are routed uniformly into ``ALLOWED_MACROS`` — no arity or kind split.
What a given macro needs (nothing, a const-shim, a ``static inline
crustify_<NAME>`` wrapper) is a property of its BODY, and the body is exactly
what the composer refuses to interpret.

Scope + annotations
-------------------
The repo-root analysis tree is cumulative across targets, so scope comes from
the target's ``scope.json`` via the same ``syms``/``types`` composers the
analyze pipeline uses (``FilterSpec(scope_json_path=…)``). Those give the
per-target in-scope **identities**; the agent-filled **annotations** (the
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
_SHIMS_H_START = "/* crustify:shims:start */"    # the shim block in bindgen.h
_SHIMS_H_END = "/* crustify:shims:end */"
# Pre-rename spellings, read (never written) so an existing bindgen.h's shim
# body survives the first regeneration after the rename.
_SHIMS_H_START_OLD = "/* crustify:macros:start */"
_SHIMS_H_END_OLD = "/* crustify:macros:end */"
_INC_START = "/* crustify:includes:start */"
_INC_END = "/* crustify:includes:end */"
# Allowlist overrides in build.rs, seeded empty and preserved verbatim across
# composer regenerations. Fix-ups here need a compiler in the loop (they answer
# "what did bindgen ACTUALLY emit?"), which is outside the composer's remit.
_ALLOW_AGENT_START = "// crustify:allowlist-agent:start"
_ALLOW_AGENT_END = "// crustify:allowlist-agent:end"
_USE_START = "// crustify:foreign-use:start"
_USE_END = "// crustify:foreign-use:end"

# Real C types bindgen can emit. A `callback` is NOT here — it is a SYMBOL
# (function-pointer typedef in syms.json), routed to ALLOWED_CALLBACKS by the
# symbol loop below; scalar/primitive typedefs lower to Rust primitives.
_BINDABLE_TYPE_KINDS = frozenset({"struct", "union", "enum"})
# Functions and globals are routed by PREFIX, not by an allowlist of kinds.
# Whatever reaches this composer is already the target's wrap closure, and the
# syms composer has already dropped what cannot appear there
# (`_WRAP_DISALLOWED_FN_KINDS`). Re-filtering here by kind duplicated that rule
# in a second place and silently dropped anything the first place ever let
# through — so bind what we are given. `wrap_static_fns` makes a non-exported
# function linkable, so `function_static` / `function_inline_*` need no special
# case beyond being in ALLOWED_FUNCTIONS like any other.
_FUNC_PREFIX = "function"
_VAR_PREFIX = "global"
# Header-template suffixes that resolve to a generated `.h` in a configured
# tree (e.g. OpenSSL's `Configure` expands `opensslv.h.in` → `opensslv.h`).
_HEADER_TEMPLATE_SUFFIXES = (".h.in",)
# MACROS are routed UNIFORMLY into ALLOWED_MACROS — no arity or kind split.
# Which treatment a given macro needs (nothing, a const-shim, a `static inline
# crustify_<NAME>` wrapper) is a property of its BODY, and the body is exactly
# what the composer refuses to interpret; guessing from the head only produced a
# split that had to be re-derived downstream.
_MACRO_PREFIX = "macro"

# Foreign-dep attribution reads the AUTHORED crate graph (`crates.json`
# `depends_on`) and nothing else. An earlier second mechanism also walked every
# `fields[].type` / `ptr_args[].type` / `ptr_ret.type` string, resolved each
# token to its owning crate, and patched `foreign_libs` + `blocklist` on a
# mismatch. It was removed: it could only see types reached from a crate's own
# WRAP entities, so it structurally missed a foreign type embedded by a
# PORT-scope struct — the case that motivated reading the crate graph in the
# first place — and where the graph was correct it was pure duplication
# (verified: neutralising it left the composed plan bit-identical). What it
# uniquely detected was an `depends_on` inconsistent with placement, which is a
# `crates.json` defect and belongs in that file's validation, reported against
# the referencing field, not silently patched here.


def _sys_crate(lib: str) -> str:
    return f"{lib}-sys"


def _sys_mod(lib: str) -> str:
    return f"{lib}-sys".replace("-", "_")


# ------------------------------------------------------------------ data model

class LibPlan:
    """Everything one ``<lib>-sys`` crate needs."""

    __slots__ = (
        "lib", "allow_types", "allow_funcs", "allow_macros", "allow_vars",
        "allow_callbacks", "includes", "foreign_libs", "blocklist",
    )

    def __init__(self, lib: str) -> None:
        self.lib = lib
        # One set per entity kind, kept separate all the way into build.rs:
        # they are different facts about the C surface, and collapsing two of
        # them loses which is which for anyone reading the crate.
        self.allow_types: set[str] = set()      # struct/union/enum tags + aliases
        self.allow_funcs: set[str] = set()      # every function kind
        self.allow_macros: set[str] = set()     # every in-scope macro
        self.allow_vars: set[str] = set()       # global variables
        self.allow_callbacks: set[str] = set()  # function-pointer typedefs
        self.includes: set[str] = set()         # header paths for bindgen.h
        self.foreign_libs: set[str] = set()     # other libs whose types we ref
        self.blocklist: set[str] = set()        # foreign tags+typedefs to NOT
                                                # emit (imported via use <dep>)


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
    ``scope.origin_key(id, defined_in, declared_in)`` is in ``keys``. ``None``
    keeps every in-scope-by-dir entry.
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
    crate names ARE the link-unit library keys (``libssl`` /
    ``libcrypto`` / ``libc``). We index every ``.rs`` member so an entity
    resolves to its owning crate (== library) — replacing the per-entity
    ``build.json`` library keys. Empty when no repo_root / crates.json."""
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
                df = r.get("tu")
                decls = set(r.get("headers") or [])
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
) -> str | None:
    """Owning crate (== library) of an entity from the crates.json index,
    disambiguated by ``def_file`` then ``decl_file``. ``None`` when crates.json
    homes the name nowhere — which is a placement gap, not a routing choice."""
    cands = idx.get(name)
    if not cands:
        return None
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
      lib_filter: optional crate/library restriction (post-scoping).
      repo_root: the repository root; used to resolve per-library
        ``include_dirs`` from ``<repo_root>/.crustify/build.json`` into
        absolute bindgen ``-I`` clang args. When None, clang args are
        omitted (the agent's cargo-check loop surfaces the gap).
    """
    if filter_spec is None:
        filter_spec = FilterSpec()

    syms_by_dir, _syms_scope, _ = syms_compose(csv_dir_t1, csv_dir_t2, filter_spec)
    types_by_dir, _types_scope, _ = types_compose(csv_dir_t1, csv_dir_t2, filter_spec)

    # FFI surface = the WRAP closure from scope.json (authoritative, deduped).
    # Keyed (name|type, defined_in); sym and type buckets resolved separately so
    # a name can't cross-match the wrong kind.
    sj = filter_spec.scope_json_path
    wrap_sym_keys = (scope.scope_membership(
        sj, "wrap", kinds=("functions", "globals", "macros")) if sj else None)
    wrap_type_keys = (scope.scope_membership(
        sj, "wrap", kinds=("types",)) if sj else None)
    wrap_syms = _load_inscope_annotated(
        syms_by_dir, analysis_root, "syms.json", "symbols", "name",
        keys=wrap_sym_keys,
    )
    wrap_types = _load_inscope_annotated(
        types_by_dir, analysis_root, "types.json", "types", "name",
        keys=wrap_type_keys,
    )

    # Alias → owning lib, and alias → all-names (tag + typedefs), over every
    # bindgen-EMITTED entity: wrap types AND wrap callbacks. A callback is a
    # syms.json symbol, but bindgen emits it as a type, so it can be minted
    # twice exactly like a struct — building this map from types.json alone left
    # every dependency-owned callback typedef unblocklistable (68 of them on the
    # ssl target), and bindgen's transitive allowlist can pull one in through any
    # allowlisted signature that names it. The all-names map lets the blocklist
    # cover *every* spelling of a foreign type, so bindgen emits none of them and
    # the `use <dep>_sys::*` import is unambiguous.
    # Library routing is crates.json-driven. There is no per-entry fallback: the
    # retired `linked_in` field is emitted by nothing and present on no entry.
    crate_idx = _crate_index(repo_root)

    alias_to_lib: dict[str, str] = {}
    alias_to_names: dict[str, set[str]] = {}

    def note_alias(lib: str, names: set[str]) -> None:
        for n in names:
            alias_to_lib[n] = lib
            alias_to_names[n] = names

    for t in wrap_types:
        lib = _lib_of(crate_idx, (t.get("name") or t["type"]), t.get("defined_in"),
                      t.get("declared_in"))
        if not lib:
            continue
        note_alias(lib, {(t.get("name") or t["type"]),
                         *(t.get("typedef") or [])})
    for s in wrap_syms:
        if (s.get("kind") or "") != "callback":
            continue
        lib = _lib_of(crate_idx, s["name"], s.get("defined_in"),
                      s.get("declared_in"))
        if lib:
            note_alias(lib, {s["name"]})

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
        lib = _lib_of(crate_idx, (t.get("name") or t["type"]), t.get("defined_in"),
                      t.get("declared_in"))
        if not lib:
            continue
        lp = plan_for(lib)
        if lp is None:
            continue
        tag = (t.get("name") or t["type"])
        kind = t.get("kind")
        if kind in _BINDABLE_TYPE_KINDS:
            lp.allow_types.add(tag)
            for td in t.get("typedef") or []:
                lp.allow_types.add(td)
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

    # ---- symbols ----
    for s in wrap_syms:
        lib = _lib_of(crate_idx, s["name"], s.get("defined_in"),
                      s.get("declared_in"))
        if not lib:
            continue
        lp = plan_for(lib)
        if lp is None:
            continue
        name, kind = s["name"], s.get("kind") or ""
        if kind.startswith(_MACRO_PREFIX):
            lp.allow_macros.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind.startswith(_FUNC_PREFIX):
            # Every function kind lands in ALLOWED_FUNCTIONS. A non-exported one
            # (static / inline) is made linkable by `wrap_static_fns`, which
            # only wraps functions that are ALSO allowlisted — so there is no
            # kind for which allowlisting is wrong.
            lp.allow_funcs.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind.startswith(_VAR_PREFIX):
            lp.allow_vars.add(name)
            _add_header(lp, s.get("defined_in"))
            for h in s.get("declared_in") or []:
                _add_header(lp, h)
        elif kind == "callback":
            # A callback (function-pointer typedef) is bindgen-emitted as a
            # type, but it is a syms.json SYMBOL, not a types.json entry — kept
            # in its own set so that distinction survives into build.rs.
            # defined_in is null (a header typedef), so routing rides on the
            # declaring headers.
            lp.allow_callbacks.add(name)
            for h in s.get("declared_in") or []:
                _add_header(lp, h)

    # NOTE: no explicit "layout closure" is needed. bindgen's allowlist is
    # transitive — allowlisting a struct pulls in (and lays out, from the
    # included headers) every type it references, recursively. And a
    # blocklisted type used *by value* is still sized correctly from its C
    # definition (bindgen just doesn't emit a Rust copy; the field references
    # the imported `use <dep>_sys::*` type). An earlier hand-rolled closure
    # here was a workaround for a bindgen 0.70 opacity bug, now fixed in 0.72;
    # it has been removed (it also wrongly duplicated foreign value types).

    # Foreign-dep attribution — the ONLY mechanism, straight from the authored
    # crate graph. A per-reference walk cannot substitute for it: it would only
    # see types reached from a crate's own WRAP entities and so miss a foreign
    # type embedded by a PORT struct (`rio_poll_builder_st.pfds: pollfd` — that
    # struct is port-scope for the ssl target, so no wrap loop ever visits it).
    # Every wrap type homed to a declared dep is blocklisted, referenced or not,
    # so bindgen imports it from the dep's `-sys` instead of re-minting it.
    # Filtered to deps that actually emit a `-sys` (an empty foreign crate — e.g.
    # libz here — produces no crate to depend on), so "declared dep" and
    # "blocklisted" are not the same set.
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


# ---------------------------------------------------------------------- writing

def _seed_include_order(includes: set[str]) -> list[str]:
    """First-run seed: the headers in a stable (sorted) but otherwise
    arbitrary order. Header dependency order is target-specific and
    naming-sensitive (it can't be inferred generically), so the composer
    does NOT attempt any semantic ordering — the ``crustify:includes`` block
    is reordered by hand on the first build error."""
    return [f'#include "{_inc(h)}"' for h in sorted(includes)]


def _bindgen_h(lp: LibPlan, existing: str | None, *,
               reset: bool = False) -> str:
    """Render bindgen.h. The ``#include`` list lives in a manual-edit
    ``crustify:includes`` block: header dependency order *and membership* are
    dependency-sensitive and need a compiler in the loop (a header may need
    reordering, or dropping because it is double-included transitively). The
    composer seeds the block on first creation only and never modifies it
    afterwards.

    ``reset`` re-seeds that block from this run's scope, discarding whatever
    ordering and membership it held. The shim block is NOT composer-owned and
    survives a reset.
    """
    # Recover the current include block. When the markers are present the body
    # is kept **byte-for-byte** — it owns ordering, membership AND commentary:
    # a header may have been removed as transitively double-included (record.h
    # via ssl_local.h) or as unparseable standalone (a TU-private `*_local.h`
    # fragment), and the note saying WHY is the only record of it. Re-filtering
    # to `#include` lines silently discarded exactly those notes. An additive
    # merge would fight the edits too, so once the block exists the composer
    # never touches it; a later scope expansion's headers are added by hand.
    kept: str | None = None
    if existing and not reset:
        if _INC_START in existing and _INC_END in existing:
            body = existing.split(_INC_START, 1)[1] \
                           .split(_INC_END, 1)[0].strip("\n")
            kept = body if body.strip() else None
        else:
            # Migration: a marker-less file — harvest its include lines.
            harvested = [l.strip() for l in existing.splitlines()
                         if l.strip().startswith("#include")]
            kept = "\n".join(harvested) or None

    block_lines = [kept] if kept else _seed_include_order(lp.includes)

    # Shims live in a block AFTER every include (so every declaration is in
    # scope): `static inline RET crustify_<NAME>(...) { return NAME(...); }`.
    # `wrap_static_fns` makes them linkable and a `crustify_.*` allowlist binds
    # them — so no separate bindgen_macros.{h,c}. Seeded empty, then preserved
    # verbatim across regenerations.
    shim_body = ""
    for start, end in ((_SHIMS_H_START, _SHIMS_H_END),
                       (_SHIMS_H_START_OLD, _SHIMS_H_END_OLD)):
        if existing and start in existing and end in existing:
            shim_body = existing.split(start, 1)[1].split(end, 1)[0].strip("\n")
            break

    lines = [
        f"/* {_sys_crate(lp.lib)} bindgen master header (incomplete scaffold). */",
        "/* Seeded once, then hand-owned: add, remove or reorder #include lines",
        "   to make the closure parse. Clang -I paths live in AGENT_CLANG_ARGS",
        "   in build.rs. */",
        "",
        _INC_START,
        *block_lines,
        _INC_END,
        "",
        "/* Shims: `static inline crustify_<NAME>(...)` over what a Rust caller",
        "   cannot reach through a binding. Linkable via wrap_static_fns; list",
        "   each (or `crustify_.*`) in build.rs AGENT_ALLOWED_FUNCTIONS or the",
        "   closed allowlist drops it. Expected to be needed only for a struct",
        "   defined in a TU (no header, so no layout) and a macro that does not",
        "   expand to a callable symbol. Everything else is either already Rust",
        "   by DAG ordering, or bound in its own -sys. */",
        _SHIMS_H_START,
        *([shim_body] if shim_body else []),
        _SHIMS_H_END,
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


# The seed for the non-composer block: one AGENT_ array mirroring each
# composer array, all empty, each with its own role comment. Written ONCE (see
# `_agent_block`); never regenerated after anything is filled in.
_AGENT_SEED = (
    _ALLOW_AGENT_START + "\n"
    "// Fix-ups a real build reveals — one array mirroring each above. Entries\n"
    "// may be regexes. Seeded empty once; from the first entry anywhere in the\n"
    "// block, the composer never rewrites it.\n"
    "// Also scalar/primitive typedefs: not types.json entities, so never\n"
    "// composer-pulled.\n"
    + _rust_arr("AGENT_ALLOWED_TYPES", set())
    + "// Shims from bindgen.h's crustify:shims block; the allowlist is closed,\n"
      "// so an unlisted shim never reaches bindings.rs. `crustify_.*` covers all.\n"
    + _rust_arr("AGENT_ALLOWED_FUNCTIONS", set())
    + _rust_arr("AGENT_ALLOWED_MACROS", set())
    + "// Also const-shims that lower to a variable.\n"
    + _rust_arr("AGENT_ALLOWED_VARS", set())
    + _rust_arr("AGENT_ALLOWED_CALLBACKS", set())
    + "// Types that came out duplicated.\n"
    + _rust_arr("AGENT_BLOCKLIST_FOREIGN", set())
    + "// bindgen/cc clang flags. Include-path discovery is compiler-in-the-loop;\n"
      "// build.json `include_dirs` is a hint. `-I` relative to the repo root.\n"
    + _rust_arr("AGENT_CLANG_ARGS", set())
    + "// `cargo:` directive bodies, e.g. `rustc-link-lib=dylib=git2`. Empty when\n"
      "// the staticlib is linked into the C build.\n"
    + _rust_arr("AGENT_LINK_ARGS", set())
    + _ALLOW_AGENT_END + "\n"
)


def _agent_block(existing: str | None) -> str:
    """Return the non-composer block for a regenerated build.rs.

    **Write-once.** The composer does not own this block and never edits it: if
    the markers are present, the block comes back BYTE-FOR-BYTE, whatever it
    holds. The seed is written exactly once, when the block does not exist yet.
    No inspection of the contents — not the array names, not whether anything
    is filled in — so there is nothing here that can misread a fix-up.

    The trade-off is deliberate: a crate scaffolded before a new AGENT_ array
    existed never gains it. Nothing in build.rs references the consts (there is
    no `fn main`), so a missing one is inert text, while rewriting a fix-up
    debugged against a real compiler is the worse failure. Delete the block to
    get a fresh seed.
    """
    return _slice_block(existing, _ALLOW_AGENT_START, _ALLOW_AGENT_END) \
        or _AGENT_SEED


def _slice_block(text: str | None, start: str, end: str) -> str | None:
    """The ``start``…``end`` block of ``text`` verbatim, markers included."""
    if not text or start not in text or end not in text:
        return None
    body = text.split(start, 1)[1].split(end, 1)[0]
    return f"{start}{body}{end}\n"


def _existing_arr(existing: str | None, name: str) -> set[str]:
    """Parse a composer-owned `const NAME: &[&str] = &[ … ];` array from an
    existing build.rs, so cross-target runs can UNION rather than clobber a
    shared -sys crate's allowlist."""
    if not existing:
        return set()
    m = re.search(r"const " + re.escape(name) + r":\s*&\[&str\]\s*=\s*&\[(.*?)\];",
                  existing, re.S)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def _build_rs(lp: LibPlan, existing: str | None = None, *,
              reset: bool = False) -> str:
    """Render build.rs.

    The composer owns exactly ONE region of this file: the ``crustify:allowlist``
    block. On a fresh crate it writes the whole scaffold (that block, the seeded
    agent block, and the header comment). On every later run it splices the
    block back in place and leaves the rest of the file BYTE-FOR-BYTE — the same
    contract bindgen.h's include block has. That is what makes the missing
    ``fn main`` completable: whoever writes the ``bindgen::Builder`` chain, the
    ``-I`` resolution and the ``cc`` step writes it into this file, and a later
    ``crustify-cli <target> bindgen`` refreshes the allowlists underneath it without
    touching a line of it.

    ``reset`` drops the cross-target union, so the arrays state exactly this
    run's wrap scope. It does not widen what the composer owns: the body and the
    agent block survive a reset too.
    """
    # Cross-target additive: a shared -sys crate's allowlist is the UNION of
    # every target's wrap surface in that library — never shrink it. Under
    # `reset` the accumulated set is discarded instead, so an entity that has
    # left the scope leaves the array.
    prev = None if reset else existing
    allow_types = lp.allow_types | _existing_arr(prev, "ALLOWED_TYPES")
    allow_funcs = lp.allow_funcs | _existing_arr(prev, "ALLOWED_FUNCTIONS")
    allow_macros = lp.allow_macros | _existing_arr(prev, "ALLOWED_MACROS")
    allow_vars = lp.allow_vars | _existing_arr(prev, "ALLOWED_VARS")
    allow_cbs = lp.allow_callbacks | _existing_arr(prev, "ALLOWED_CALLBACKS")
    blocklist = lp.blocklist | _existing_arr(prev, "BLOCKLIST_FOREIGN")
    allow = (
        _ALLOW_START + "\n"
        "// Composer-owned, from the wrap scope. Regenerated additively each\n"
        "// run — fix-ups go in the agent block below.\n"
        + "// struct / union / enum tags + typedef aliases.\n"
        + _rust_arr("ALLOWED_TYPES", allow_types)
        + "// Every function kind; wrap_static_fns links the static/inline ones.\n"
        + _rust_arr("ALLOWED_FUNCTIONS", allow_funcs)
        + "// Every `#define`, undifferentiated: what each needs depends on its\n"
          "// body. A shim goes in AGENT_ALLOWED_FUNCTIONS, not here.\n"
        + _rust_arr("ALLOWED_MACROS", allow_macros)
        + "// Global variables.\n"
        + _rust_arr("ALLOWED_VARS", allow_vars)
        + "// Function-pointer typedefs (a symbol, but bindgen emits a type).\n"
        + _rust_arr("ALLOWED_CALLBACKS", allow_cbs)
        + "// Owned by a dependency -sys; arrive via `pub use <dep>_sys::*`.\n"
        + _rust_arr("BLOCKLIST_FOREIGN", blocklist)
        + _ALLOW_END
    )
    # Existing file: swap the composer block in place, preserve everything else
    # (header, agent block, and any hand-written `fn main` / helpers) verbatim.
    if existing and _ALLOW_START in existing and _ALLOW_END in existing:
        head, rest = existing.split(_ALLOW_START, 1)
        _, tail = rest.split(_ALLOW_END, 1)
        return head + allow + tail
    return f'''//! {_sys_crate(lp.lib)} bindgen inputs — INCOMPLETE SCAFFOLD.
//!
//! Generated by crustify-cli bindgen, which emits WHAT to bind and not HOW — so
//! there is no `fn main`. The `bindgen::Builder` chain, the `-I` resolution and
//! the `cc` step for `wrap_static_fns` all need a real compiler in the loop,
//! and the analysis tree cannot tell you any of them. The `#include` closure
//! lives in bindgen.h.
//!
//! Only the `crustify:allowlist` block below is composer-owned; a later bindgen
//! run splices it back in and leaves the rest of this file untouched, so it is
//! safe to write the body here.

{allow}

{_agent_block(existing)}'''


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


def write_plan(plan: Plan, rust_root: Path, *, reset: bool = False) -> Stats:
    """Write every crate in ``plan``.

    ``reset`` recomputes the COMPOSER-OWNED state from scratch instead of
    accumulating onto it: build.rs's ``ALLOWED_*`` / ``BLOCKLIST_FOREIGN`` stop
    being a cross-target union, and bindgen.h's ``crustify:includes`` block is
    re-seeded. It does not reach anything the composer does not own — the agent
    block, the shim block and Cargo.toml are identical with and without it.
    """
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
        # Generated each run — composer-owned, except the
        # `crustify:allowlist-agent` block which is preserved verbatim.
        brs = root / "build.rs"
        emit(brs, _build_rs(lp, brs.read_text() if brs.exists() else None,
                            reset=reset), overwrite=True)
        # bindgen.h: composer-owned shell, but its include list is a
        # manual-edit block — preserve its ordering and membership.
        bh = root / "bindgen.h"
        emit(bh, _bindgen_h(lp, bh.read_text() if bh.exists() else None,
                            reset=reset), overwrite=True)
        emit(root / "src" / "lib.rs", _lib_rs(lp), overwrite=True)
        # Shims live in bindgen.h's own crustify:shims block (see _bindgen_h) —
        # no separate bindgen_macros.{h,c}, and no bindgen_extra.h: a fix-up
        # #include goes in the crustify:includes block like any other, so there
        # is only one include list to read.

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
                    help="Restrict to these crates.json libraries.")
    ap.add_argument("--repo-root", type=Path, default=None,
                    help="Repo root; resolves build.json include_dirs into "
                         "absolute bindgen -I clang args.")
    ap.add_argument("--reset", action="store_true",
                    help="Recompute the composer-owned state from scratch: the "
                         "build.rs ALLOWED_*/BLOCKLIST_FOREIGN arrays stop "
                         "being a cross-target union, and bindgen.h's include "
                         "block is re-seeded. Never touches the agent block or "
                         "the shims.")
    args = ap.parse_args()

    spec = FilterSpec(scope_json_path=args.scope_json)
    plan = compose(args.t1, args.t2, args.analysis_root, spec,
                   lib_filter=args.libs, repo_root=args.repo_root)
    stats = write_plan(plan, args.rust_root, reset=args.reset)
    for lib, lp in sorted(plan.libs.items()):
        print(f"  {_sys_crate(lib):16} types={len(lp.allow_types):4} "
              f"funcs={len(lp.allow_funcs):4} "
              f"macros={len(lp.allow_macros):4} "
              f"vars={len(lp.allow_vars):4} "
              f"callbacks={len(lp.allow_callbacks):4} "
              f"deps={sorted(lp.foreign_libs)}")
    print(f"bindgen: {stats.libs} -sys crate(s), {stats.files_written} file(s) "
          f"written, {stats.skipped_existing} preserved → {args.rust_root}")


if __name__ == "__main__":
    main()
