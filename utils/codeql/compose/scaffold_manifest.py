"""Deterministically scaffold the Rust crate skeleton from the analysis tree.

This is the **scaffold** stage, recast as a pure composer (no LLM agent).
It mirrors the **vanilla C source directory layout** into a Cargo workspace
under `<target>/rust/crates/`.

Scope comes from the **target's** `scope.json`, never from the on-disk
analysis tree. The repo-root analysis tree (`<repo_root>/.crustify/analysis`)
is cumulative across targets — it accumulates entries from every `analyze`
run for any target — so walking it blindly would scaffold the union of all
targets. Instead this composer re-runs the same `types`/`syms` composers the
analyze pipeline uses, with a `FilterSpec(scope_json_path=<target scope>)`,
which returns exactly this target's in-scope universe (port files ∪
wrap-reachable closure). Same inputs (T1/T2 CodeQL CSVs + scope.json), same
scope semantics, fully per-target.

Path rule — one Rust module per C source file (file-grained)
------------------------------------------------------------
A manifest dir on disk is `<srcdir>/<stem>` (the composer's stem-grouping,
see `path_partition.manifest_dir_for`). The Rust location drops the stem
component: a file's module lives in the folder of its *source directory*,
flat, exactly as the `.c`/`.h` files sit in the C tree. There is one `.rs`
per source file; a type is NOT given its own file — it is an item anchor
homed in the `.rs` of the source file it *lives* in (`defined_in` for port
elements, the declaring import header for wrap elements), so multiple types
share a file (see the file-grained R2 note further down).

    analysis/ssl/ssl_local/types.json
        srcdir = ssl, stem = ssl_local
        -> crates/ssl/src/ssl_local.rs          (file module; its types +
                                                 syms anchor here, no
                                                 per-type `.rs`)

    analysis/ssl/statem/statem_clnt/types.json
        srcdir = ssl/statem, stem = statem_clnt
        -> crates/ssl/src/statem/statem_clnt.rs

Module plumbing
---------------
  - crate  = first path component of the source dir (`ssl`, `crypto`,
    `include`, `system`, ...). One crate per top-level dir; nothing dropped.
  - crate root dir       -> `src/lib.rs`
  - every C subdirectory -> `src/<rel>/mod.rs`   (the module file lives
    INSIDE the dir, 2015-style, so no `<dir>.rs` ever sits outside a
    directory that exists in the vanilla layout)
  - every C file stem    -> `src/<rel>/<stem>.rs`

`lib.rs` / `mod.rs` carry nothing but a managed `pub mod` list inside
`// crustify:modules:start` / `// crustify:modules:end` markers — a flat
list of the immediate child dirs + file stems in that dir. Re-runs
reconcile the block additively (new modules appended, existing lines and
any non-stub `.rs` body left untouched), the same contract the bindgen
stage uses for `bindgen.h`.

Within a single dir, a file stem that collides with a child dir name is
folded into that dir's `mod.rs` (the dir doubles as the file-stem module).

The bindgen wiring (`<top>-sys/` crates) is intentionally NOT done here —
that is the separate `CrustifyBindingsScaffolder` LLM agent's job.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple

from . import scope
from .filter_spec import FilterSpec
from .path_partition import manifest_dir_for
from .syms_manifest import compose as syms_compose
from .types_manifest import compose as types_compose

# Markers for the managed module-list block in lib.rs / mod.rs. Same
# load-bearing convention as the bindgen.h / wrap-modules blocks.
_BLOCK_START = "// crustify:modules:start"
_BLOCK_END = "// crustify:modules:end"

# Top-level dirs that never become a Rust crate. `system/` IS kept — we may
# need wrappers for libc/system types — so nothing is excluded by default.
_SKIP_CRATES: frozenset[str] = frozenset()

# Every type the scaffolder homes comes from the fresh (T1, T2) `types`
# composer — there are no agent-synthesized type kinds to pull back in from the
# on-disk tree.

# --- per-symbol anchor model (scheduler idempotency / --reset targets) --------
# Every emitted symbol gets a stable, scaffolded location anchor + a
# ``// crustify:todo`` placeholder; the scheduler treats todo-absent as "done"
# and ``--reset`` resets the anchor's region. Anchors are laid for the symbol
# subkinds the wrap/port stages actually emit; the rest are filtered out.
_TODO = "// crustify:todo"
_MANAGED = "//! crustify:managed"

# Classification is by kind *prefix*: `function_*` / `global_*` get a
# re-export / safe-view `// Replaces:` anchor. `macro*` gets NONE — macros are
# neither ported nor wrapped, bindgen owns their whole surface. `Mirrors` is
# retained on the anchor grammar for the port stage's own use. These never
# anchor (no manifest entry exists to place):
_SKIP_KINDS: frozenset[str] = frozenset({"external", "builtin"})


class SymAnchor(NamedTuple):
    name: str
    file: str | None     # defining file (for the `(<file>)` annotation)
    verb: str            # "Replaces" | "Mirrors"

# Rust 2018 reserved words that can't be bare module names.
_RUST_KEYWORDS = frozenset({
    "as", "break", "const", "continue", "crate", "dyn", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop",
    "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
    "static", "struct", "super", "trait", "true", "type", "unsafe", "use",
    "where", "while", "async", "await", "abstract", "become", "box", "do",
    "final", "macro", "override", "priv", "typeof", "unsized", "virtual",
    "yield", "try", "union",
})


# --------------------------------------------------------------------- naming

def _snake(name: str) -> str:
    """Lower-snake-case a C identifier into a valid Rust module name.

    Handles CamelCase boundaries, strips pointer/qualifier punctuation, and
    collapses any run of non-alphanumerics into a single underscore. Returns
    "" when nothing usable remains (caller skips such entries).
    """
    import re
    s = name.strip()
    # Insert a boundary at lower|digit -> Upper transitions (CamelCase).
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _strip_st(name: str) -> str:
    """Drop a trailing `_st` / `_state`-style struct-tag suffix, casefold-safe."""
    for suffix in ("_st", "_state_st"):
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _module_name(raw: str) -> str:
    """Sanitize a snake stem into a legal, non-keyword module name."""
    stem = _snake(raw)
    if not stem:
        return ""
    if stem[0].isdigit():
        stem = "_" + stem
    if stem in _RUST_KEYWORDS:
        stem = stem + "_"
    return stem


def _type_stem(entry: dict[str, Any]) -> str:
    """Derive the snake-case anchor stem for one types.json entry — the
    type's name *within* its file-grained `.rs`, NOT a separate file.

    Prefer the public typedef (`SSL_SESSION` -> `ssl_session`); fall back to
    the struct tag with `_st` stripped (`ssl_session_st` -> `ssl_session`).
    """
    typedefs = entry.get("typedef") or []
    chosen = typedefs[0] if typedefs else (entry.get("name") or entry.get("type") or "")
    return _module_name(_strip_st(chosen or ""))


# ---------------------------------------------------------------------- plan

class TypeMod(NamedTuple):
    stem: str            # snake module name (pre-collision-resolution)
    tag: str             # C type tag (entry["name"])
    typedef: str | None  # public typedef, if any
    scope: str | None    # "port" / "wrap" / None (unknown)
    fields: tuple = ()   # field names (each gets a `// Field:` accessor anchor)


# ======================================================================
# File-grained scaffold (R2): one Rust module per C source file.
#
# Every element is anchored where it *lives* — port elements by ``defined_in``,
# wrap elements by their **import header** (the precise ``scope.json.wrap``
# surface). A type is just an item anchor among its file's other items, so
# multiple types share one file and a wrap type's port-scope ops cannot be
# misplaced into the wrong crate (the failure mode of the earlier, now-removed
# per-``<type>.rs`` model).
# ======================================================================

class FileStub:
    """All anchors homing in one source file's ``.rs`` module.

    ``src_label`` is the **home** (where the wrapper is placed — a wrap type's
    declaring header, or a port symbol's ``defined_in``). ``source_files`` is the set of
    every C source path that *contributes* an anchor here (each element's
    ``defined_in`` ∪ ``declared_in``). The ``--file`` / ``--dir`` selectors match
    against ``source_files`` ∪ ``src_label``, so a focused scaffold can name the
    natural source file (``odb.h``) even when the wrapper homes at a different
    header (``include/git2/odb.h``)."""
    __slots__ = ("src_label", "types", "syms", "source_files")

    def __init__(self, src_label: str) -> None:
        self.src_label = src_label
        self.types: list[TypeMod] = []
        self.syms: list[SymAnchor] = []
        self.source_files: set[str] = set()


def _decls(v: Any) -> list[str]:
    if isinstance(v, list):
        return [d for d in v if d]
    return [v] if v else []


def _load_wrap_routing(
    scope_json_path: Path | None,
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """``(name, defined_in) -> import header`` and ``tag -> import header`` from
    the derived ``wrap`` section of scope.json. The home is the first (sorted)
    ``declared_in`` header; any extra headers are re-export sites (R2.4)."""
    sym_via: dict[tuple[str, str], str] = {}
    type_via: dict[str, str] = {}
    if scope_json_path is None:
        return sym_via, type_via
    try:
        doc = json.loads(Path(scope_json_path).read_text())
    except (OSError, ValueError):
        return sym_via, type_via
    w = doc.get("wrap") or {}
    for bucket in ("functions", "globals", "macros"):
        for r in w.get(bucket, []):
            via = r.get("declared_in") or []
            if via:
                sym_via[(r.get("name"), r.get("defined_in") or "")] = via[0]
    for r in w.get("types", []):
        via = r.get("declared_in") or []
        if via:
            type_via[r.get("name") or r.get("type")] = via[0]
    return sym_via, type_via


def _wrap_home_header(decls: list[str], defined_in: str) -> str:
    """Fallback import header for a wrap element absent from scope.wrap (e.g. a
    its canonical declaration header, else ``defined_in``."""
    return scope.canonical_decl(decls) or defined_in


def _classify_symbols(syms_by_dir, port_files, want):
    """Yield ``(name, defined_in, decls, verb, scope_label)`` for every
    anchorable symbol, routed file-grained. Classification is (``function_*``/``global_*`` →
    ``Replaces``; macros skipped entirely — bindgen owns them); routing is
    the caller's job."""
    for mdir, entries in syms_by_dir.items():
        parts = Path(mdir).parts
        crate = parts[0] if parts else None
        if crate is None or crate in _SKIP_CRATES or (
                want is not None and crate not in want):
            continue
        for e in entries:
            name, kind = e.get("name"), e.get("kind") or ""
            if not name or kind in _SKIP_KINDS:
                continue
            df = e.get("defined_in") or ""
            decls = _decls(e.get("declared_in"))
            if kind.startswith("macro"):
                # Macros get NO anchor: they are the one kind that is neither
                # ported (port.py: "macros … are bindgen's") nor wrapped
                # (wrap.py excludes macro_* from selection) — bindgen owns their
                # whole surface, a `crustify_<NAME>` shim or a `pub const`. They
                # still appear in crates.json `members` so bindgen can resolve
                # which library owns them, but nothing anchors them to a .rs.
                continue
            if kind.startswith(("function_", "global_")):
                yield name, df, decls, "Replaces", scope.classify(df, decls, port_files)


def _crate_by_mdir(analysis_root: Path | None) -> dict[str, str]:
    """Map each manifest dir to its owning crate. Always ``{}``.

    This used to read a per-entry ``linked_in`` library off the analysis tree.
    That field is emitted by nothing and present on no entry (0 of 437 types,
    0 of 7926 symbols on the OpenSSL tree), so the lookup could only ever miss;
    reading it back was dead code that read as live attribution.

    Crate attribution is ``crates.json`` now, consumed by ``crustify.scaffold``
    — which is the ``scaffold`` command. The ``python -m scaffold_manifest``
    entry point below does not use it, so it has had no crate attribution since
    ``linked_in`` was retired. Kept as a seam rather than threaded through, so
    that gap stays visible instead of being spelled `{}` at the call site.
    """
    return {}


def compose_files(
    csv_dir_t1: Path,
    csv_dir_t2: Path,
    filter_spec: FilterSpec | None = None,
    *,
    crate_filter: list[str] | None = None,
    analysis_root: Path | None = None,
) -> dict[str, FileStub]:
    """File-grained scaffold plan: ``manifest_dir (posix) -> FileStub``.

    Routes every in-scope element to the source file it belongs in — port by
    ``defined_in``, wrap by its ``scope.json.wrap`` import header — and groups
    all of that file's anchors into one stub. The module tree + on-disk emission
    are :func:`write_plan_files`."""
    if filter_spec is None:
        filter_spec = FilterSpec()
    scope_json = filter_spec.scope_json_path
    port_files = scope.load_port_paths(scope_json) if scope_json else set()
    sym_via, type_via = _load_wrap_routing(scope_json)
    want = set(crate_filter) if crate_filter else None

    types_by_dir, _, _ = types_compose(csv_dir_t1, csv_dir_t2, filter_spec)
    syms_by_dir, _, _ = syms_compose(csv_dir_t1, csv_dir_t2, filter_spec)
    manifest_dirs = set(types_by_dir) | set(syms_by_dir)

    # Crate = the owning link unit. The module tree inside a crate mirrors the
    # full C source path, so a type (in `include/…`) and its ops (in `src/…`)
    # co-locate in their library's crate. NO in-tree/system discrimination —
    # any library with a source location (in-tree, a vendored lib like libz, or
    # a system header carrying a type/constant) belongs in its own crate. Pure
    # external symbols with no source file are unscaffoldable anyway (see
    # `_SKIP_KINDS`) and stay FFI-only via bindgen.
    # `_crate_by_mdir` is empty — see its docstring: this path has had no crate
    # attribution since `linked_in` was retired, and `crates.json` (via
    # `crustify.scaffold`) is the live one.
    crate_by_mdir = _crate_by_mdir(analysis_root)

    def _crate_for(md_posix: str) -> str | None:
        return crate_by_mdir.get(md_posix) or None

    stubs: dict[str, FileStub] = {}

    def _stub_at(key: str, src_label: str) -> FileStub | None:
        parts = Path(key).parts
        crate = parts[0] if parts else None
        if crate is None or crate in _SKIP_CRATES or (
                want is not None and crate not in want):
            return None
        st = stubs.get(key)
        if st is None:
            st = stubs[key] = FileStub(src_label)
        return st

    def home(target_file: str) -> FileStub | None:
        md = manifest_dir_for(target_file)
        if md is None or not md.parts:
            return None
        crate = _crate_for(md.as_posix())
        if crate is None:
            return None
        return _stub_at(f"{crate}/{md.as_posix()}", target_file)

    def add_type(entry: dict, scope_label: str) -> None:
        tag = entry.get("name") or entry.get("type")
        if not tag:
            return
        if scope_label == "wrap":
            st = home(type_via.get(tag) or _wrap_home_header(
                _decls(entry.get("declared_in")), entry.get("defined_in") or ""))
        else:
            st = home(entry.get("defined_in") or "")
        if st is None:
            return
        for s in (entry.get("defined_in"), *_decls(entry.get("declared_in"))):
            if s:
                st.source_files.add(s)
        typedefs = entry.get("typedef") or []
        st.types.append(TypeMod(
            stem=_type_stem(entry) or _module_name(tag) or tag, tag=tag,
            typedef=typedefs[0] if typedefs else None, scope=scope_label,
            fields=_field_names(entry)))

    for mdir, entries in types_by_dir.items():
        parts = Path(mdir).parts
        crate = parts[0] if parts else None
        if crate in _SKIP_CRATES or (want is not None and crate not in want):
            continue
        for entry in entries:
            add_type(entry, _entry_scope(entry, port_files) if port_files else "wrap")

    for name, df, decls, verb, scope_label in _classify_symbols(
            syms_by_dir, port_files, want):
        if scope_label == "wrap":
            tf = sym_via.get((name, df)) or _wrap_home_header(decls, df)
        else:
            tf = df
        st = home(tf)
        if st is not None:
            st.syms.append(SymAnchor(name, df, verb))
            for s in (df, *decls):
                if s:
                    st.source_files.add(s)

    return stubs


def _type_block(tm: "TypeMod") -> str:
    """One type's anchor block: the ``// Replaces:`` item anchor followed by its
    ``// Field:`` accessor anchors. Shared by the fresh-stub and reconcile paths."""
    typedef = f"  [typedef: {tm.typedef}]" if tm.typedef else ""
    sc = f"  ({tm.scope})" if tm.scope else ""
    block = f"// Replaces: {tm.tag}{typedef}{sc}\n{_TODO}"
    for f in tm.fields:
        block += f"\n// Field: {f}\n{_TODO}"
    return block


def _sym_block(a: "SymAnchor") -> str:
    loc = f" ({Path(a.file).name})" if a.file else ""
    return f"// {a.verb}: {a.name}{loc}\n{_TODO}"


def _file_stub(st: FileStub) -> str:
    """Render one source file's module: a managed header, then every type (as a
    ``// Replaces:`` *item* anchor — files hold many types now — each followed by
    its ``// Field:`` accessor anchors), then the file's free functions / mirrored
    macros. All ``//`` line comments so the unfilled stub compiles."""
    head = (
        f"//! `{st.src_label}` — generated module (file-grained).\n"
        f"{_MANAGED}\n"
        f"//!\n"
        f"//! One Rust module per C source file: every type (with its field\n"
        f"//! accessors), function, and mirrored macro that lives in this file\n"
        f"//! is anchored below; the wrap/port stage fills each in place.\n"
    )
    blocks: list[str] = [_type_block(tm) for tm in sorted(st.types, key=lambda t: t.tag)]
    blocks += [_sym_block(a) for a in sorted(st.syms)]
    return head + ("\n" + "\n\n".join(blocks) + "\n" if blocks else "")


# Matches an emitted item anchor of either flavour: ``// Replaces: <name>`` /
# ``/// Replaces: <name>`` / ``// Mirrors: <name>``. The captured name is the
# bare tag (anything up to the first whitespace / ``[typedef`` / ``(`` suffix),
# so a filled or unfilled anchor reads identically. Leading whitespace is
# allowed: a *filled* anchor is routinely an indented ``///`` doc comment inside
# an ``impl`` / ``define_type!`` block, not a column-0 line — missing those would
# make the reconcile pass re-append a duplicate stub for already-done work.
_ANCHOR_RE = re.compile(r"^\s*/{2,3}\s*(?:Replaces|Mirrors):\s*([^\s\[(]+)", re.M)


def _existing_anchor_names(text: str) -> set[str]:
    return set(_ANCHOR_RE.findall(text))


def _reconcile_anchors(dest: Path, st: FileStub) -> int:
    """Back-fill anchors the composer now plans but an *existing* stub lacks.

    The stub file is written once at creation; later analysis improvements
    (e.g. an anonymous-typedef type that only gained a ``def_file`` after a
    query fix, or a newly in-scope free symbol) would otherwise never get an
    anchor — re-scaffold skips any file that exists. This mirrors the additive
    ``// crustify:modules`` block contract at the *anchor* granularity: append
    every planned ``// Replaces:`` type / symbol anchor whose name is absent,
    preserving all existing (filled or unfilled) content untouched. Returns the
    number of anchors appended.

    Scoped to whole-item (type/symbol) addition — field-accessor back-fill into
    an already-present type block is intentionally left out (it would risk
    editing filled work; the agent reads fields from ``types.json`` regardless).
    """
    text = dest.read_text()
    have = _existing_anchor_names(text)
    add_types = [tm for tm in sorted(st.types, key=lambda t: t.tag)
                 if tm.tag not in have]
    add_syms = [a for a in sorted(st.syms) if a.name not in have]
    if not add_types and not add_syms:
        return 0
    blocks = [_type_block(tm) for tm in add_types] + [_sym_block(a) for a in add_syms]
    addition = (_RECONCILE_NOTE + "\n"
                + "\n\n".join(blocks) + "\n")
    dest.write_text(text.rstrip("\n") + "\n\n" + addition)
    return len(blocks)


_RECONCILE_NOTE = (
    "// crustify:reconciled — anchors back-filled by a later scaffold pass "
    "(see scaffold_manifest._reconcile_anchors).")


def write_plan_files(stubs: dict[str, FileStub], rust_root: Path,
                     *, all_keys=None) -> Stats:
    """Realize the file-grained plan: one ``.rs`` per source file + the module
    tree. Idempotent (stub files written only when absent; module blocks merge).

    Folding: when a file stem equals a sibling sub*directory* (e.g. ``hash.c``
    beside ``hash/``), Rust can't name both ``hash`` — so the file's anchors are
    written into that directory's ``mod.rs`` (which doubles as the file module)
    rather than a clashing ``<stem>.rs``. Folding depends on the *full* plan, so
    ``all_keys`` (every key from the unfiltered plan) keeps a narrowed write's
    paths identical to a full ``--all`` write; it defaults to ``stubs``' keys."""
    crates_root = rust_root
    parsed = {k: (p[0], p[1:-1], p[-1])
              for k in stubs for p in [Path(k).parts]}
    # A stem folds iff (its dir + stem) is itself the home dir of some stub.
    home_dirs = _fold_home_dirs(all_keys if all_keys is not None else stubs)

    # Module tree: (crate, reldir tuple) -> children + optional folded file.
    tree: dict[tuple, dict] = {}

    def node(crate: str, tup: tuple) -> dict:
        n = tree.get((crate, tup))
        if n is None:
            n = tree[(crate, tup)] = {"subdirs": set(), "stems": set(),
                                      "folded": None}
        return n

    def ensure_chain(crate: str, tup: tuple) -> None:
        node(crate, tup)
        for i in range(len(tup)):
            node(crate, tup[:i])["subdirs"].add(tup[i])

    for key, (crate, rd, stem_raw) in parsed.items():
        ensure_chain(crate, rd)
        if (crate, rd + (stem_raw,)) in home_dirs:        # stem clashes with a dir
            ensure_chain(crate, rd + (stem_raw,))
            node(crate, rd + (stem_raw,))["folded"] = key
        else:
            node(crate, rd)["stems"].add((_module_name(stem_raw) or stem_raw, key))

    n_files = n_mod = n_skip = n_recon = 0
    for crate in {c for (c, _) in tree}:
        cargo = crates_root / crate / "Cargo.toml"
        if not cargo.exists():
            cargo.parent.mkdir(parents=True, exist_ok=True)
            cargo.write_text(_port_crate_cargo_toml(crate, crates_root))

    for (crate, rd), n in tree.items():
        folder = crates_root / crate / "src"
        if rd:
            folder = folder / Path(*rd)
        # leaf file stubs in this dir
        for stem, key in n["stems"]:
            dest = folder / f"{stem}.rs"
            if dest.exists():
                n_skip += 1
                n_recon += _reconcile_anchors(dest, stubs[key])
                continue
            folder.mkdir(parents=True, exist_ok=True)
            dest.write_text(_file_stub(stubs[key]))
            n_files += 1
        # this dir's module file (lib.rs at the crate root, else mod.rs) —
        # carries the pub-mod block, plus the folded file's anchors if any.
        modfile = folder / ("lib.rs" if not rd else "mod.rs")
        entries = [
            _module_decl(_module_name(d) or d, d if (_module_name(d) or d) != d else None)
            for d in sorted(n["subdirs"])
        ] + [_module_decl(stem, None) for stem, _ in sorted(n["stems"])]
        header = (
            # crate-root lib.rs carries crate-level lints:
            #  - allow(module_inception): file-grained mirroring of the C tree
            #    maps a `foo.c` inside `foo/` to `foo::foo`. Harmless.
            #  - warn(undocumented_unsafe_blocks): make the wrap/port merge's
            #    `cargo clippy` gate enforce the DISCIPLINE `// SAFETY:` rule —
            #    every `unsafe {}` / `unsafe impl` needs a SAFETY comment
            #    immediately above it (catches positional/keyword drift the
            #    audit's raw unsafe count can't). Only the agent-written wrapper
            #    crates get it; the bindgen `-sys` crates are emitted elsewhere.
            f"//! `{crate}` crate root (generated).\n"
            f"#![allow(clippy::module_inception)]\n"
            f"#![warn(clippy::undocumented_unsafe_blocks)]"
            if not rd
            else f"//! `{crate}/{'/'.join(rd)}` module tree (generated).")
        if n["folded"] is not None and not modfile.exists():
            folder.mkdir(parents=True, exist_ok=True)
            block = (_BLOCK_START + "\n"
                     + "\n".join(sorted(entries, key=_entry_key))
                     + "\n" + _BLOCK_END + "\n")
            modfile.write_text(_file_stub(stubs[n["folded"]]).rstrip() + "\n\n" + block)
            n_mod += 1
            n_files += 1
        else:
            if _merge_module_block(modfile, header, entries):
                n_mod += 1
            # A folded `.c` bakes its anchors into this existing mod.rs — back-fill
            # any the composer now plans but the file lacks (same contract as the
            # leaf-stub reconcile above).
            if n["folded"] is not None and modfile.exists():
                n_recon += _reconcile_anchors(modfile, stubs[n["folded"]])

    sync_workspace(rust_root)
    return Stats(crates=len({c for (c, _) in tree}), module_files=n_mod,
                 stem_stubs=n_files, type_stubs=0, skipped_existing=n_skip,
                 reconciled=n_recon)


def _op_ownership(
    analysis_root: Path | None, manifest_dirs: set[str],
) -> dict[str, str]:
    """``op-name -> owning type tag`` from the **agent-annotated** on-disk
    ``types.json`` (the fresh T1/T2 composer leaves ``ops`` empty — it is an
    analyzer-filled field). Scoped to the in-scope manifest dirs. A name owned
    by several types is assigned to
    the lexicographically smallest tag, so its anchor lands in one file only
    (the other owners merely call it). Name-keyed: a same-named free static in
    another file can be mis-pulled (rare); the (name, defined_in) DAG keying is
    the precise resolver downstream.
    """
    owner: dict[str, str] = {}
    if analysis_root is None:
        return owner
    for mdir in sorted(manifest_dirs):
        try:
            doc = json.loads((analysis_root / mdir / "types.json").read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("types", []):
            tag = e.get("name") or e.get("type")
            if not tag or str(tag).startswith(("_", "(")):
                continue
            for op in scope.type_method_syms(e):
                if op and (op not in owner or tag < owner[op]):
                    owner[op] = tag
    return owner


def _entry_scope(entry: dict[str, Any], port_files: set[str]) -> str:
    """port iff the type's defining file (or first declaring header) is in
    the port-scope set; else wrap."""
    df = entry.get("defined_in")
    if not df:
        decls = entry.get("declared_in")
        if isinstance(decls, list):
            df = decls[0] if decls else None
        elif isinstance(decls, str):
            df = decls
    return "port" if df in port_files else "wrap"


# -------------------------------------------------------------------- writing

class Stats(NamedTuple):
    crates: int
    module_files: int
    stem_stubs: int
    type_stubs: int
    skipped_existing: int
    reconciled: int = 0  # anchors back-filled into existing stubs (F7)


def _fold_home_dirs(keys) -> set:
    """``{(crate, reldir-tuple)}`` — the set of home dirs across ``keys``. A
    file stem folds (homes in ``<stem>/mod.rs`` rather than ``<stem>.rs``) iff
    its own ``dir + stem`` path is the home dir of some other stub."""
    return {(p[0], p[1:-1]) for k in keys for p in [Path(k).parts]}


def plan_paths(stubs: dict[str, "FileStub"], rust_root: Path,
               all_keys=None) -> dict[str, Path]:
    """Deterministic ``stub key -> homed .rs path`` — the same path rule
    :func:`write_plan_files` writes to, computed **without touching disk**. The
    oracle behind ``scaffold`` query mode. Folding depends on the *full* plan,
    so pass ``all_keys`` (every key from the unfiltered plan) when resolving a
    narrowed selection; it defaults to ``stubs``' own keys."""
    home_dirs = _fold_home_dirs(all_keys if all_keys is not None else stubs)
    out: dict[str, Path] = {}
    for k in stubs:
        crate, rd, stem_raw = (lambda p: (p[0], p[1:-1], p[-1]))(Path(k).parts)
        folder = rust_root / crate / "src"
        if rd:
            folder = folder / Path(*rd)
        if (crate, rd + (stem_raw,)) in home_dirs:           # stem clashes w/ dir
            out[k] = folder / stem_raw / "mod.rs"            # folded
        else:
            out[k] = folder / f"{_module_name(stem_raw) or stem_raw}.rs"
    return out


def _module_decl(name: str, raw_dir: str | None) -> str:
    """One `pub mod` declaration. When ``raw_dir`` is set (the on-disk folder
    name differs from the sanitized module ``name``), prepend a ``#[path]``
    bridge so the module still resolves to the real, e.g. hyphenated, folder."""
    if raw_dir is not None and raw_dir != name:
        return f'#[path = "{raw_dir}/mod.rs"]\npub mod {name};'
    return f"pub mod {name};"


def _entry_key(entry: str) -> str:
    """Identity of a module declaration — its `pub mod X;` line, ignoring any
    `#[path]` attribute above it. Used to dedup/merge across re-runs."""
    for ln in entry.splitlines():
        s = ln.strip()
        if s.startswith("pub mod "):
            return s
    return entry.strip()


def _parse_module_entries(body: str) -> dict[str, str]:
    """Parse a managed module block into ``{pub-mod line -> full entry}``. A
    `#[path = "..."]` line binds to the `pub mod` line immediately below it, so
    multi-line entries round-trip intact (idempotent across re-runs)."""
    out: dict[str, str] = {}
    pending: str | None = None
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("#[path"):
            pending = s
        elif s.startswith("pub mod "):
            out[s] = (pending + "\n" + s) if pending else s
            pending = None
        else:
            pending = None
    return out


def _merge_module_block(path: Path, header: str, entries: list[str]) -> bool:
    """Write/merge the managed `pub mod` block in a lib.rs/mod.rs.

    ``entries`` are full declarations — each a `pub mod X;`, optionally preceded
    by a `#[path = "..."]` line for a sanitized-dir bridge. Returns True when
    the file was created or its block changed. Additive: the union of existing +
    new entries (keyed by the `pub mod` line), sorted; existing declarations are
    never dropped and content outside the markers is kept.
    """
    desired = {_entry_key(e): e.strip() for e in entries}

    if path.exists():
        text = path.read_text()
        if _BLOCK_START in text and _BLOCK_END in text:
            pre, rest = text.split(_BLOCK_START, 1)
            body, post = rest.split(_BLOCK_END, 1)
            merged = dict(_parse_module_entries(body))
            changed = False
            for k, e in desired.items():
                if merged.get(k) != e:
                    merged[k] = e
                    changed = True
            if not changed:
                return False  # nothing new
            block = (_BLOCK_START + "\n"
                     + "\n".join(merged[k] for k in sorted(merged))
                     + "\n" + _BLOCK_END)
            path.write_text(pre + block + post)
            return True
        # File exists without markers — leave it alone (hand-edited).
        return False

    block = (_BLOCK_START + "\n"
             + "\n".join(desired[k] for k in sorted(desired))
             + "\n" + _BLOCK_END)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n\n" + block + "\n")
    return True


def _field_names(entry: dict[str, Any]) -> tuple[str, ...]:
    """Field names of a type entry — each gets a `// Field:` accessor anchor so
    the per-field workload is budget-split (``--max-fields``) and tracked like
    ops. Fields come from the structural composer, so this is available without
    an on-disk read (unlike ops)."""
    return tuple(f["name"] for f in (entry.get("fields") or []) if f.get("name"))


def _port_crate_cargo_toml(crate: str, rust_root: Path) -> str:
    """Cargo.toml for a per-library port crate. It is the **staticlib**
    the C build links (replacing the old central ``ffi-exports`` crate, since
    the ``#[no_mangle]`` re-exports now co-locate with their ops inside this
    crate). Carries an empty ``[features]`` table (the port stage appends the
    ``CRUSTIFY_<FILE>`` flags) and deps on ``crustify`` + every ``-sys`` FFI
    crate present in the workspace."""
    sys_deps = sorted(
        p.name for p in rust_root.iterdir()
        if p.is_dir() and p.name.endswith("-sys") and (p / "Cargo.toml").exists()
    )
    lib_name = crate.replace("-", "_")
    deps = ['crustify-prim = { path = "../../../../crustify-prim" }', 'paste = "1"']
    deps += [f'{d} = {{ path = "../{d}" }}' for d in sys_deps]
    return (
        "[package]\n"
        f'name = "{crate}"\n'
        'version = "0.0.0"\n'
        'edition = "2024"\n'
        "\n"
        "[lib]\n"
        f'name = "{lib_name}"\n'
        'crate-type = ["staticlib", "rlib"]\n'
        "\n"
        "[features]\n"
        "default = []\n"
        "\n"
        "[dependencies]\n"
        + "\n".join(deps) + "\n"
    )


def sync_workspace(rust_root: Path) -> None:
    """(Re)write the shared repo-root ``rust/`` workspace manifest with an
    explicit, additive member list discovered from disk.

    Crates live directly under ``rust/`` (``rust/ssl``, ``rust/crypto``,
    ``rust/libssl-sys``, …). Because the source crates (scaffold) and the
    ``-sys`` crates (bindgen) are written by separate composer passes — and
    the tree is shared/grown across targets — the member list is derived from
    whatever crate dirs exist on disk, so no pass clobbers another's
    registration."""
    rust_root.mkdir(parents=True, exist_ok=True)
    members = sorted(
        p.name for p in rust_root.iterdir()
        if p.is_dir() and (p / "Cargo.toml").exists()
    )
    body = "".join(f'    "{m}",\n' for m in members)
    # `[workspace.lints]` must exist for any member carrying `[lints] workspace =
    # true` to load at all — without it cargo rejects the WHOLE workspace, not
    # just that crate. Emitted empty: which lints to set is a policy call for the
    # crate author, and an empty table inherits nothing while still resolving.
    (rust_root / "Cargo.toml").write_text(
        "[workspace]\n"
        'resolver = "2"\n'
        f"members = [\n{body}]\n"
        "\n"
        "[workspace.lints]\n"
    )


# --------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scaffold the Rust crate skeleton for one target, scoped "
                    "by its scope.json (file-grained)."
    )
    ap.add_argument("--t1", type=Path, required=True,
                    help="<repo_root>/.crustify/codeql/t1")
    ap.add_argument("--t2", type=Path, required=True,
                    help="<repo_root>/.crustify/codeql/t2")
    ap.add_argument("--scope-json", type=Path, required=True,
                    help="<target>/.crustify/scope.json")
    ap.add_argument("--rust-root", type=Path, required=True,
                    help="<target>/rust")
    ap.add_argument("--crates", nargs="+", default=None, metavar="DIR",
                    help="Restrict to these top-level crate dirs (post-scope).")
    ap.add_argument("--analysis-root", type=Path, default=None,
                    help="<repo_root>/.crustify/analysis. Optional; enables "
                         "crate routing.")
    args = ap.parse_args()

    spec = FilterSpec(scope_json_path=args.scope_json)
    stubs = compose_files(args.t1, args.t2, spec, crate_filter=args.crates,
                          analysis_root=args.analysis_root)
    stats = write_plan_files(stubs, args.rust_root)
    print(
        f"scaffold: {stats.crates} crate(s), {stats.module_files} module "
        f"file(s) touched, {stats.stem_stubs} file stub(s), "
        f"{stats.skipped_existing} existing file(s) preserved, "
        f"{stats.reconciled} anchor(s) reconciled → {args.rust_root}"
    )


if __name__ == "__main__":
    main()
