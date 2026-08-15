"""Deterministic audit scanner (no LLM) — **entity-seeded, global search**.

Seeds are **types and symbols** (like wrap/port ``--name``); the scan is always
global. For each seed the audit reports:

  - **`own`** — the entity's *own implementation* surface: ``unsafe_pub_fn`` /
    ``unsafe_blocks`` / ``raw_ptr_ret`` (signature return) / ``raw_ptr_arg``
    (signature args) / ``raw_ptr_body`` (every other raw ptr `*mut`/`*const` in
    the body — casts, ``let x: *T``, turbofish, field/`static` decls),
    ``wrapper_ref`` (items taking the wrapper by reference of either kind — a §8
    smell, since access goes through the borrowed handles),
    ``ffi_self`` (raw ``ffi::T`` use *inside* its own impl),
    ``ffi_self_smell`` (the subset of ``ffi_self`` that is NEITHER the
    ``define_*ctype!`` binding NOR inside an FFI-seam routine — with ``self`` in
    scope there is no reason to name ``ffi::T``), ``raw_ptr_void`` (the
    ``*mut/const c_void`` subset of the raw-ptr counts), and ``raw_ptr_seam``
    (raw ptrs inside seam routines — expected plumbing, subtract to isolate
    smells), and ``raw_ptr_body_smell`` (body raw ptrs that ARE a smell — **port
    symbols only**, gated by the ``stage`` inferred from the `#[no_mangle]`
    re-export; a wrap body legitimately uses raw ptrs at the FFI seam),
    measured over its impl region **minus** its `mod ffi_export` C-ABI shim
    (whose raw ptrs / `ffi::` are legitimate marshalling, not a smell):
      * **type** ``T`` (wrapper ``W``) — its ``define_*ctype!(…, ffi::T)`` plus
        every ``impl W`` / ``impl … for W`` / ``impl_*!(W, …)`` block;
      * **symbol** ``S`` — the ``/// Replaces: S`` idiomatic fn plus its
        ``mod ffi_export`` re-export.
  - **`naked`** — every ``ffi::T`` use / ``ffi::S(`` call **outside** that impl
    region **and outside any ``mod ffi_export`` gateway** (DISCIPLINE §1.4 — the
    sanctioned raw C-ABI re-export region, where ``ffi::`` use is expected),
    anywhere in the tree (the wrapper being bypassed). With per-site file + line
    list.

A seed with no wrapper (no ``define_*ctype!`` / ``/// Replaces:``) is reported
with ``wrapper: null`` and just its naked footprint, so unwrapped-but-used
entities surface.

Alongside the per-seed entries, a seed-independent **`global`** section
(``_global_scan``) reports tree-wide smells the seed model can't: raw-ptr
signatures **outside** every impl/``ffi_export``/seam region (``outside_impl``),
the ``ffi::Ident`` type surface partitioned into ``wrapped_bypass`` (a
``define_*ctype!`` exists and was bypassed) vs ``unwrapped`` (``ffi_type_surface``),
a ``*c_void`` filter split into sanctioned vs smell (``void_ptr``), and a
**raw-field-projection** filter (``raw_field_proj``) — ``(*x.as_ptr()).field`` /
``addr_of!((*p).field)`` sites split into sanctioned (accessor definitions, in an
``impl``/``trait`` body or seam) vs smell (a port body bypassing the accessor).

Selectors resolve to the seed set (``compose.audit_manifest.resolve_seeds``):
``--name`` (explicit), ``--file`` / ``--dir`` (entities homed there),
``--crate`` (entities in that crate), ``--all`` (every wrapped type ∪ symbol).
All regex-driven over comment/string-stripped text.
"""

from __future__ import annotations

import re
from pathlib import Path

# Example output (`crustify <repo> <target> audit --name git_oid git_oid_cpy`):
#
#   {
#     "seed": "--name git_oid git_oid_cpy",
#     "resolved": {"types": 1, "symbols": 1, "unwrapped": 0},
#     "entries": [
#       {
#         "name": "git_oid", "kind": "type", "wrapper": "GitOid",
#         "home": "libgit2/src/odb/oid_api.rs",
#         "own": {"unsafe_pub_fn": 0, "unsafe_blocks": 1, "raw_ptr_ret": 0,
#                 "raw_ptr_arg": 0, "raw_ptr_body": 0, "raw_ptr_body_smell": 0,
#                 "raw_ptr_void": 0, "raw_ptr_seam": 2, "wrapper_ref": 0,
#                 "ffi_self": 1, "ffi_self_smell": 0},
#         "naked": 6,
#         "naked_sites": [
#           {"file": "libgit2/src/odb/hashmap_oid.rs", "count": 2, "lines": [118, 140]},
#           {"file": "libgit2/src/odb/oidarray_h.rs",  "count": 1, "lines": [54]}
#         ]
#       },
#       {
#         "name": "git_oid_cpy", "kind": "symbol", "stage": "port",
#         "wrapper": "git_oid_cpy", "home": "libgit2/src/odb/oid.rs",
#         "own": {"unsafe_pub_fn": 0, "unsafe_blocks": 1, "raw_ptr_ret": 0,
#                 "raw_ptr_arg": 0, "raw_ptr_body": 0, "raw_ptr_body_smell": 0,
#                 "raw_ptr_void": 0, "raw_ptr_seam": 0, "wrapper_ref": 0,
#                 "ffi_self": 0, "ffi_self_smell": 0},
#         # stage (port|wrap) inferred from a bare `#[no_mangle]` re-export;
#         # raw_ptr_body_smell / ffi_self_smell fire only for port symbols.
#         "naked": 0, "naked_sites": []
#       }
#       # an unwrapped seed (no define_*ctype!/Replaces found) reports just its
#       # footprint: {"wrapper": null, "home": null, "own": null, "naked": N, ...}
#     ],
#     # seed-independent, tree-wide — always present, identical for any selector:
#     "global": {
#       # raw-ptr signatures OUTSIDE every impl / ffi_export / seam routine:
#       "outside_impl": {"raw_ptr_ret": 5, "raw_ptr_arg": 63, "sites": [
#         {"file": "libgit2/src/pack/delta.rs", "count": 4, "lines": [65, 73, 309, 341]}]},
#       # raw ptrs INSIDE seam routines (as_ptr/from_raw/…) — expected plumbing:
#       "seam": {"raw_ptr_ret": 4, "raw_ptr_arg": 0},
#       # naked `ffi::Ident` type uses, partitioned (bypass = wrapper exists):
#       "ffi_type_surface": {"naked_total": 310, "wrapped_bypass": 57,
#                            "unwrapped": 142, "scalar": 101,
#                            "unwrapped_tags": {"git_object_t": 8, "pthread_key_t": 6}},
#       # `*mut/const c_void`: sanctioned (ffi_export/seam) vs smell (elsewhere):
#       "void_ptr": {"total": 65, "sanctioned": 23, "smell": 42, "smell_sites": [
#         {"file": "libgit2/src/pack/pack_objects_h.rs", "count": 3, "lines": [489, 502, 530]}]},
#       # `(*x.as_ptr()).field` / `addr_of!((*p).field)`: sanctioned (accessor
#       # definition in an impl/trait body or seam) vs smell (port body bypass):
#       "raw_field_proj": {"total": 14, "sanctioned": 11, "smell": 3, "smell_sites": [
#         {"file": "libgit2/src/pack/mwindow.rs", "count": 3, "lines": [702, 806, 1012]}]}
#     },
#     "totals": {"naked_ffi": 6, "naked_type": 6, "naked_call": 0,
#                "own_unsafe_blocks": 2, "own_raw_ptr": 0, "own_raw_ptr_void": 0,
#                "own_raw_ptr_body_smell": 0, "own_ffi_self_smell": 0,
#                "own_wrapper_ref": 0, "global_void_ptr_smell": 42,
#                "global_raw_field_proj_smell": 3, "unwrapped": 0}
#   }

_RE_UNSAFE_PUB_FN = re.compile(r"\bpub(?:\([^)]*\))?\s+unsafe\s+fn\b")
_RE_UNSAFE_BLOCK = re.compile(r"\bunsafe\s*\{")
_RE_RAW_PTR_RET = re.compile(r"->\s*\*\s*(?:const|mut)\b")
_RE_FN_SIG = re.compile(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\(")
_RE_RAW_PTR = re.compile(r"\*\s*(?:const|mut)\b")
_RE_BORROW_MUT_SELF = re.compile(r"&\s*(?:'\w+\s+)?mut\s+self\b")
# Raw FIELD projection off a WRAPPER's pointer — the `*mut`/`*const` type literal
# that `_RE_RAW_PTR` matches misses these *value-position* projections entirely:
#   `(*x.as_ptr()).field` / `(*x.as_mut_ptr()).field`        — deref a wrapper's
#       own raw pointer and reach into a field, bypassing its safe accessor;
#   `addr_of!((*x.as_ptr()).field)` / `addr_of_mut!(…)`      — the macro form.
# Rooted at `.as_ptr()`/`.as_mut_ptr()` ON PURPOSE: that marks the pointer as a
# wrapper's own (a safe accessor exists or should), the case the user flagged.
# A bare `addr_of!((*p).field)` on a raw FFI-struct pointer with NO wrapper
# (e.g. a ported C-array header) is legitimate raw porting, not this smell, so it
# is deliberately excluded. A projection inside an `impl`/`trait` body (an
# accessor *definition*), a seam routine, or a `mod ffi_export` shim is
# sanctioned; one in a free function (a port body) is the smell.
_RE_RAW_FIELD_PROJ = re.compile(
    r"\baddr_of(?:_mut)?!\s*\(\s*\(\s*\*\s*[\w.]+\.as_(?:mut_)?ptr\s*\(\s*\)\s*\)"  # addr_of!((*x.as_ptr())…)
    # `(*x.as_ptr()).field` — but NOT `addr_of!(*x.as_ptr()).method()`, where the
    # `(` is addr_of!'s call paren wrapping a place expr (a whole-pointee read,
    # not a field projection); the lookbehinds drop that single-paren form.
    r"|(?<!addr_of!)(?<!addr_of_mut!)\(\s*\*\s*[\w.]+\.as_(?:mut_)?ptr\s*\(\s*\)\s*\)\s*\.\w")
_RE_TRAIT_HEAD = re.compile(r"\btrait\s+\w+[^{;]*\{")

_RE_DEFINE_TYPE = re.compile(r"define_ctype!\s*[({](.*?)[)}]", re.DOTALL)
# Any wrapper-binding macro — `define_*ctype!` plus the lifecycle binders
# (`impl_dropped!` / `impl_cloned!` / `impl_cvalued!` / …),
# each of which takes `ffi::T` as a binding argument. Those `ffi::T` mentions are bindings,
# not business-logic smells, so they are excluded from `ffi_self_smell`.
_RE_BIND_MACRO = re.compile(
    r"\b(?:define_ctype|impl_[a-z_]+)!\s*[({](.*?)[)}]", re.DOTALL)
_RE_DT_NAMES = re.compile(
    r"\A(?:\s*(?://[^\n]*|\#\[[^\]]*\])\n?)*\s*(\w+)\s*,.*?\bffi::(\w+)",
    re.DOTALL)
# The layout newtype a wrapper is `#[repr(transparent)]` over.
_RE_CTYPE = re.compile(r"\bCType\s*<\s*ffi::(\w+)")
_RE_REPLACES = re.compile(r"//+\s*(?:Replaces|Wraps):\s*(\w+)")
_RE_MOD_FFI_EXPORT = re.compile(r"\bmod\s+ffi_export\b")

_SKIP_CRATE_SUFFIX = "-sys"

# FFI-seam conversion routines — the sanctioned home of raw `*ffi::T` / `*c_void`
# plumbing (the moral equivalent of a `mod ffi_export` gateway, but for pointer
# conversions). A raw pointer or `ffi::T` *inside* one of these is expected; the
# same token in an ordinary business method is a smell (self / a safe wrapper
# should have been used instead).
_SEAM_FN_NAMES = (
    "as_ptr", "as_mut_ptr", "as_c_ptr", "as_raw",
    "from_ptr", "from_raw", "to_ptr", "to_raw", "into_raw",
)
_RE_SEAM_FN = re.compile(
    r"\bfn\s+(?:" + "|".join(_SEAM_FN_NAMES) + r")\b\s*(?:<[^>]*>)?\s*\([^;{]*\{")
_RE_IMPL_HEAD = re.compile(r"\bimpl\b[^{;]*\{")
# A raw pointer to the type-erased `c_void` (`*mut c_void` / `*const c_void`,
# with or without a `core::ffi::` / `std::ffi::` path qualifier).
_RE_RAW_PTR_VOID = re.compile(
    r"\*\s*(?:const|mut)\s+(?:(?:::)?(?:core|std)::ffi::)?c_void\b")
# `ffi::Ident` used in *bare type* position — NOT a `ffi::fn(` call and NOT a
# `ffi::T::assoc` path qualifier (`ffi::T::bitfield_raw(self.as_ptr())` is a
# sanctioned bitfield accessor, not a naked type use of `T`).
_RE_FFI_TYPE_USE = re.compile(r"\bffi::(\w+)\b(?!\s*\()(?!::)")


def _re_ffi_bare_type(tag: str) -> re.Pattern:
    """`ffi::<tag>` as a bare type — excludes the `ffi::<tag>(` call form and the
    `ffi::<tag>::assoc` path (bitfield accessors / associated consts)."""
    return re.compile(rf"\bffi::{re.escape(tag)}\b(?!\s*\()(?!::)")

# Scalar / primitive FFI aliases that are NOT wrappable types — a naked
# `ffi::c_int` is not a bypassed wrapper, it is just a primitive. Listed
# **explicitly by name** (no `c_*` / `*_t` wildcard) so genuine wrappable C
# types that merely share a prefix (e.g. `git_object_t`, `git_oid_t`) are never
# misclassified as scalars. `ffi_type_surface` buckets these separately from the
# actionable `unwrapped` (real C types still lacking a `define_*ctype!`).
_FFI_SCALAR_ALIASES = frozenset({
    # core::ffi primitives
    "c_void", "c_char", "c_schar", "c_uchar", "c_short", "c_ushort",
    "c_int", "c_uint", "c_long", "c_ulong", "c_longlong", "c_ulonglong",
    "c_float", "c_double", "c_size_t", "c_ssize_t", "c_ptrdiff_t",
    # std C-string views
    "CStr", "CString",
    # common libc width/offset aliases seen in the bindgen surface
    "off_t", "off64_t", "size_t", "ssize_t", "ptrdiff_t", "intptr_t",
    "uintptr_t", "time_t", "mode_t", "pid_t", "uid_t", "gid_t", "dev_t",
    "ino_t", "nlink_t", "blksize_t", "blkcnt_t", "suseconds_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
})


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def _strip_noise(text: str) -> str:
    """Blank line/block comments + string/char literals, preserving length."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == '"':
            q, j = c, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    j += 1
                    break
                j += 1
            for k in range(i, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "'":
            # Disambiguate a Rust char literal (`'x'`, `'\n'`, `b'\0'`) from a
            # lifetime (`'a`, `'static`, `'this`): a char literal is an escape
            # (`'\…'`) or a single char closed by `'`; a lifetime is `'` + ident
            # with NO closing quote. Blanking a lifetime as if it were a literal
            # eats the `(` / `{` / `)` up to the next `'` and truncates every
            # brace span downstream (e.g. an `impl` body with a `<'a>` method).
            if i + 1 < n and text[i + 1] == "\\":
                j = i + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == "'":
                        j += 1
                        break
                    j += 1
                for k in range(i, min(j, n)):
                    if out[k] != "\n":
                        out[k] = " "
                i = j
            elif i + 2 < n and text[i + 2] == "'":
                for k in (i, i + 1, i + 2):
                    if out[k] != "\n":
                        out[k] = " "
                i += 3
            else:
                i += 1  # Rust lifetime — not a literal, leave intact
        else:
            i += 1
    return "".join(out)


def _match_brace(text: str, open_at: int) -> int:
    """Index just past the ``}`` matching the ``{`` at/after ``open_at``."""
    b = text.find("{", open_at)
    if b == -1:
        return open_at
    depth, j = 0, b
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(text)


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _ffi_export_spans(clean: str) -> list[tuple[int, int]]:
    """Byte ranges of every ``mod ffi_export { … }`` body — the sanctioned raw
    C-ABI gateway (DISCIPLINE §1.4), where `ffi::` use is *expected*, not naked."""
    spans: list[tuple[int, int]] = []
    for m in _RE_MOD_FFI_EXPORT.finditer(clean):
        spans.append((m.start(), _match_brace(clean, m.end())))
    return spans


def _seam_spans(clean: str) -> list[tuple[int, int]]:
    """Byte ranges of every FFI-seam conversion routine body
    (`fn as_ptr`/`from_raw`/`as_c_ptr`/… — see ``_SEAM_FN_NAMES``), where raw
    pointer / `ffi::T` traffic is expected rather than a smell."""
    return [(m.start(), _match_brace(clean, m.end() - 1))
            for m in _RE_SEAM_FN.finditer(clean)]


def _all_impl_spans(clean: str) -> list[tuple[int, int]]:
    """Byte ranges of every ``impl … { … }`` block body (any impl, not just a
    wrapper's). Used to decide "outside any impl block" for the global scan.
    `\\bimpl\\b` excludes `impl_*!` macros (the `_` defeats the word boundary)."""
    return [(m.start(), _match_brace(clean, m.end() - 1))
            for m in _RE_IMPL_HEAD.finditer(clean)]


def _all_trait_spans(clean: str) -> list[tuple[int, int]]:
    """Byte ranges of every ``trait … { … }`` block body. A trait's default
    methods are accessor *definitions* (the sanctioned home for raw field
    projection), so they are excluded from the global raw-field-projection smell
    just like ``impl`` bodies are."""
    return [(m.start(), _match_brace(clean, m.end() - 1))
            for m in _RE_TRAIT_HEAD.finditer(clean)]


def _raw_ptr_args_where(text: str, want) -> tuple[int, list[int]]:
    """Count raw-ptr args across every ``fn …(…)`` whose signature start
    satisfies ``want(pos)``. Returns ``(count, [line, …])``."""
    total, lines = 0, []
    for m in _RE_FN_SIG.finditer(text):
        if not want(m.start()):
            continue
        b = m.end() - 1
        depth, j = 0, b
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        n = len(_RE_RAW_PTR.findall(text[b:j]))
        if n:
            total += n
            lines.append(_line_of(text, m.start()))
    return total, lines


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def _port_crates(rust_root: Path) -> list[Path]:
    return sorted(
        d for d in rust_root.iterdir()
        if d.is_dir() and (d / "Cargo.toml").exists()
        and not d.name.endswith(_SKIP_CRATE_SUFFIX) and d.name != "target"
    )


def _sub_of(rel: str) -> tuple[str, str]:
    """``libgit2/src/include/git2/oid.rs`` → (``libgit2``, ``include/git2/oid.rs``)."""
    parts = rel.split("/")
    crate = parts[0]
    sub = "/".join(parts[parts.index("src") + 1:]) if "src" in parts else "/".join(parts[1:])
    return crate, sub


# --------------------------------------------------------------------------- #
# tree read + wrapper index
# --------------------------------------------------------------------------- #
def load_tree(rust_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(raw, clean)`` file maps keyed by rust-root-relative path."""
    raw: dict[str, str] = {}
    clean: dict[str, str] = {}
    for crate in _port_crates(rust_root):
        src = crate / "src"
        if not src.is_dir():
            continue
        for f in src.rglob("*.rs"):
            if f.name in ("lib.rs", "mod.rs"):
                continue
            try:
                t = f.read_text()
            except OSError:
                continue
            rel = _rel(f, rust_root)
            raw[rel] = t
            clean[rel] = _strip_noise(t)
    return raw, clean


def build_index(raw: dict[str, str], clean: dict[str, str]):
    """``types[c_tag] = (home_rel, wrapper_name)`` from ``define_*ctype!`` /
    ``CType<ffi::T>``; ``syms[c_fn] = home_rel`` from ``/// Replaces:``."""
    types: dict[str, tuple[str, str]] = {}
    syms: dict[str, str] = {}
    for rel, c in clean.items():
        for m in _RE_DEFINE_TYPE.finditer(c):
            nm = _RE_DT_NAMES.search(m.group(1))
            if nm:
                types.setdefault(nm.group(2), (rel, nm.group(1)))
        for m in _RE_CTYPE.finditer(c):
            types.setdefault(m.group(1), (rel, None))
    for rel, r in raw.items():
        for m in _RE_REPLACES.finditer(r):
            # `// Replaces:` anchors both types and symbols; a tag already in the
            # type index is a type, not a symbol.
            name = m.group(1)
            if name not in types:
                syms.setdefault(name, rel)
    return types, syms


# --------------------------------------------------------------------------- #
# impl-region detection + own-surface counts
# --------------------------------------------------------------------------- #
def _type_impl_region(clean: str, wrapper: str | None) -> list[tuple[int, int]]:
    """Spans of a type's own implementation in its home file: every
    ``define_*ctype!`` / ``impl … <wrapper> … {}`` / ``impl_*!(… <wrapper> …)``."""
    spans: list[tuple[int, int]] = []
    for m in _RE_DEFINE_TYPE.finditer(clean):
        spans.append((m.start(), m.end()))
    if not wrapper:
        return spans
    W = re.escape(wrapper)
    for m in re.finditer(rf"\bimpl\b[^{{;]*\b{W}\b[^{{;]*\{{", clean):
        spans.append((m.start(), _match_brace(clean, m.end() - 1)))
    for m in re.finditer(rf"\bimpl_\w+!\s*[({{][^)}}]*\b{W}\b[^)}}]*[)}}]", clean):
        spans.append((m.start(), m.end()))
    return spans


_RE_NO_MANGLE = re.compile(r"#\s*\[\s*(?:unsafe\s*\(\s*)?no_mangle")


def _is_port_symbol(clean: str, c_fn: str) -> bool:
    """Infer wrap vs port for a symbol from the tree alone (no scope.json): a
    symbol is *ported* iff its home file carries a `#[no_mangle]` /
    `#[unsafe(no_mangle)]` `extern "C" fn <c_fn>` re-export — the port stage's
    C-replacing shim. A wrap-stage symbol has no such re-export (symbol_wrapper
    Sec 2: 'no #[no_mangle] re-export ... that is the port stage's job'). Used to
    gate the stage-dependent smells (a PORT body must not use raw ptrs / call
    `ffi::S`; a WRAP body legitimately does both at the FFI seam).

    The re-export comes in two forms, BOTH of which mean "port" (a wrap never
    re-exports the symbol — it is additive):
      - bare  `fn <c_fn>(`                     — exported C symbol, replaced directly;
      - prefixed `fn crustify_<path>__<c_fn>(` — a TU-local / inline-static symbol
        whose bare name would clash with the unfenced macro family, so the C side
        `#define`-redirects callers to the collision-safe shim. Without matching
        this form the khash `*_oidmap__resize` / `*_packmap__resize` etc. were
        mislabeled `wrap`, hiding their native-body raw pointers.

    NB: anchored on `#[no_mangle]` + `fn …`, NOT on `extern "C"` — comment/string
    stripping blanks the `"C"` literal, so it is absent in ``clean``."""
    F = re.escape(c_fn)
    # `fn <c_fn>(` or `fn crustify_<anything>__<c_fn>(` after the attribute.
    pat = re.compile(rf"\bfn\s+(?:crustify_\w*?__)?{F}\s*\(")
    for m in _RE_NO_MANGLE.finditer(clean):
        # the re-export follows the attribute within a line or two (the prefixed
        # form is long: `pub unsafe extern "C" fn crustify_src_…__<c_fn>(`).
        if pat.search(clean[m.end():m.end() + 240]):
            return True
    return False


def _sym_impl_region(raw: str, clean: str, c_fn: str) -> list[tuple[int, int]]:
    """Spans of a symbol's own implementation: the ``/// Replaces: c_fn``
    idiomatic fn + its ``mod ffi_export`` re-export ``extern "C" fn c_fn``."""
    spans: list[tuple[int, int]] = []
    F = re.escape(c_fn)
    for m in re.finditer(rf"//+\s*(?:Replaces|Wraps):\s*{F}\b", raw):
        nf = re.search(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\([^;{]*\{", clean[m.end():])
        if nf:
            s = m.end() + nf.start()
            spans.append((s, _match_brace(clean, m.end() + nf.end() - 1)))
    for m in re.finditer(rf'\bextern\s+"C"\s+fn\s+{F}\s*\([^;{{]*\{{', clean):
        spans.append((m.start(), _match_brace(clean, m.end() - 1)))
    return spans


def _own_counts(clean: str, spans: list[tuple[int, int]], c_tag: str,
                wrapper: str | None = None, is_type: bool = True,
                ffx: list[tuple[int, int]] | None = None,
                is_port: bool = False) -> dict:
    """The surface counts + ``ffi_self`` over the impl spans, **excluding the
    `mod ffi_export` C-ABI shim** (``ffx``) — its raw ptrs / `ffi::` are
    legitimate C-ABI marshalling in any stage, so counting them would be a false
    positive. The reported surface is thus the *idiomatic* wrapper/port body.

    Stage-dependent smells (symbols only, gated by ``is_port`` inferred from the
    `#[no_mangle]` re-export): a PORT body must not use raw ptrs in its body nor
    call ``ffi::S`` (it claims to replace C); a WRAP body legitimately does both.

    ``wrapper_ref`` — items that take the wrapper by reference (``&self`` /
    ``&mut self`` / ``&<Wrapper>`` / ``&mut <Wrapper>``). A reference of either
    kind over a wrapped C object asserts something about memory C may write
    through a pointer it retains; access goes through the borrowed handles,
    which hold the pointer by value. This textual pass matches on the wrapper
    NAME; the resolution-aware ``utils/unsafe_metrics`` keys on the seam trait
    and is authoritative."""
    ffx = ffx or []

    def smell(pos):
        # the entity's own impl MINUS the sanctioned `mod ffi_export` C-ABI shim.
        return _in_spans(pos, spans) and not _in_spans(pos, ffx)

    seam = _seam_spans(clean)
    # binding-macro spans (define_*ctype! + impl_*! lifecycle binders): the `ffi::T`
    # they name is the wrapper binding, not a smell.
    decl = [(m.start(), m.end()) for m in _RE_BIND_MACRO.finditer(clean)]
    # `ffi_self` — raw `ffi::<tag>` inside the entity's own idiomatic impl. For a
    # TYPE this is a bare type use (`ffi::T::assoc` bitfield accessors sanctioned);
    # for a SYMBOL it is the `ffi::fn(` call.
    ffi_re = _re_ffi_bare_type(c_tag) if is_type else re.compile(
        rf"\bffi::{re.escape(c_tag)}\b")
    ffi_self = sum(1 for m in ffi_re.finditer(clean) if smell(m.start()))
    if is_type:
        # ffi_self_smell — `ffi::T` in a TYPE's own impl that is NEITHER the
        # `define_*ctype!`/`impl_*!` binding NOR inside an FFI-seam routine: with
        # `self` (→ `self.as_ptr()`) in scope there is no reason to name `ffi::T`.
        ffi_self_smell = sum(
            1 for m in ffi_re.finditer(clean)
            if smell(m.start())
            and not _in_spans(m.start(), seam)
            and not _in_spans(m.start(), decl))
    else:
        # SYMBOL: a WRAP fn's `ffi::S(` call is the expected bridge (no smell); a
        # PORT fn that still calls `ffi::S` in its native body is not truly ported.
        ffi_self_smell = ffi_self if is_port else 0
    wrapper_ref = sum(1 for m in _RE_BORROW_MUT_SELF.finditer(clean) if smell(m.start()))
    if wrapper:
        wrapper_ref += sum(1 for m in re.finditer(
            rf"&\s*(?:'\w+\s+)?mut\s+{re.escape(wrapper)}\b", clean) if smell(m.start()))
    # Signature raw-ptr surface (return + args); everything else is body:
    # `as *T` casts, `let x: *T`, turbofish `::<*T>`, struct/field decls, etc.
    raw_ret = sum(1 for m in _RE_RAW_PTR_RET.finditer(clean) if smell(m.start()))
    raw_arg, _ = _raw_ptr_args_where(clean, smell)
    raw_total = sum(1 for m in _RE_RAW_PTR.finditer(clean) if smell(m.start()))
    raw_body = max(0, raw_total - raw_ret - raw_arg)
    # raw_ptr_void — the type-erased subset (`*mut/const c_void`) of the above.
    # raw_ptr_seam — raw ptrs that sit inside a seam routine (expected); subtract
    # to isolate the raw-ptr smells in ordinary methods.
    raw_void = sum(1 for m in _RE_RAW_PTR_VOID.finditer(clean) if smell(m.start()))
    raw_seam = sum(1 for m in _RE_RAW_PTR.finditer(clean)
                   if smell(m.start()) and _in_spans(m.start(), seam))
    return {
        "unsafe_pub_fn": sum(1 for m in _RE_UNSAFE_PUB_FN.finditer(clean) if smell(m.start())),
        "unsafe_blocks": sum(1 for m in _RE_UNSAFE_BLOCK.finditer(clean) if smell(m.start())),
        "raw_ptr_ret": raw_ret,
        "raw_ptr_arg": raw_arg,
        "raw_ptr_body": raw_body,
        # raw_ptr_body_smell — body raw ptrs that ARE a smell: a PORT fn's native
        # body should not need them (a WRAP fn's FFI-seam body legitimately does;
        # a TYPE's field-accessor body uses sanctioned addr_of/cast plumbing).
        "raw_ptr_body_smell": raw_body if (not is_type and is_port) else 0,
        "raw_ptr_void": raw_void,
        "raw_ptr_seam": raw_seam,
        "wrapper_ref": wrapper_ref,
        "ffi_self": ffi_self,
        "ffi_self_smell": ffi_self_smell,
    }


# --------------------------------------------------------------------------- #
# seed resolution + per-entity audit
# --------------------------------------------------------------------------- #
def resolve_seeds(rust_root: Path, types_idx, syms_idx, *, all=False,
                  names=None, crate=None, mod=None, file=None):
    """Resolve a selector to a ``(type_tags, sym_names)`` seed set."""
    if all:
        return sorted(types_idx), sorted(syms_idx)
    if names:
        ts = [n for n in names if n in types_idx]
        ss = [n for n in names if n in syms_idx]
        # unknown names: try as a bare type tag (may be used-but-unwrapped)
        ts += [n for n in names if n not in types_idx and n not in syms_idx]
        return ts, ss

    def home_match(rel: str) -> bool:
        c, sub = _sub_of(rel)
        if crate is not None:
            return c == crate
        if mod is not None:
            m = mod.strip("/")
            return sub == m or sub.startswith(m + "/")
        if file is not None:
            fn = file.lstrip("./")
            return (sub == fn or sub.endswith("/" + fn)
                    or f"{c}/{sub}" == fn or sub.rsplit("/", 1)[-1] == fn)
        return False

    ts = sorted(t for t, (home, _) in types_idx.items() if home_match(home))
    ss = sorted(s for s, home in syms_idx.items() if home_match(home))
    return ts, ss


def _footprint(pattern: re.Pattern, clean_files, home_rel, home_spans, ffx_spans):
    """All matches of ``pattern`` across the tree that are NOT in the entity's
    own impl region (home file) **and NOT in any `mod ffi_export` gateway**
    (sanctioned raw C-ABI region, anywhere) → ``(total, [{file,count,lines}])``."""
    sites = []
    total = 0
    for rel, c in clean_files.items():
        hits = []
        for m in pattern.finditer(c):
            pos = m.start()
            if _in_spans(pos, ffx_spans.get(rel, [])):
                continue  # inside a mod ffi_export re-export — expected, not naked
            if rel == home_rel and _in_spans(pos, home_spans):
                continue  # the entity's own impl — counted as ffi_self
            hits.append(_line_of(c, pos))
        if hits:
            total += len(hits)
            sites.append({"file": rel, "count": len(hits), "lines": hits})
    sites.sort(key=lambda s: -s["count"])
    return total, sites


def _global_scan(clean_files, types_idx, ffx_spans) -> dict:
    """Seed-independent, tree-wide scan (always runs, regardless of selector):

      - **outside_impl** — raw-ptr returns/args in signatures that sit OUTSIDE
        every ``impl`` block, every ``mod ffi_export`` gateway, and every seam
        routine (the complement of the per-seed ``own.raw_ptr_*``, which only
        covers a seed's own impl). With per-site file+line list.
      - **seam** — raw-ptr returns/args inside seam routines (``as_ptr`` etc.);
        the *expected* pointer plumbing, reported for reference / subtraction.
      - **ffi_type_surface** — every ``ffi::Ident`` in type position that is
        naked (outside any impl / ``define_*ctype!`` / ``ffi_export``), partitioned
        by whether ``Ident`` has a ``define_*ctype!`` wrapper (``wrapped_bypass`` —
        the wrapper exists and was bypassed), is a named scalar/primitive alias
        (``scalar`` — ``_FFI_SCALAR_ALIASES``, not wrappable), or is a real C type
        still lacking a wrapper (``unwrapped``; ``unwrapped_tags`` by frequency).
      - **void_ptr** — ``*mut/const c_void`` tree-wide, split into ``sanctioned``
        (inside ``ffi_export`` / a seam routine) and ``smell`` (everywhere else),
        with smell sites.
      - **raw_field_proj** — raw field projections (``(*x.as_ptr()).field`` /
        ``addr_of!((*p).field)``) tree-wide, split into ``sanctioned`` (an
        accessor *definition* — inside an ``impl``/``trait`` body, a seam routine,
        or ``ffi_export``) and ``smell`` (a free-function/port body that should
        call the accessor instead), with smell sites. This catches the
        value-position projections that the ``*mut``/``*const`` type-literal
        ``raw_ptr_*`` filters structurally miss."""
    wrapped = set(types_idx)
    g = {
        "outside_impl": {"raw_ptr_ret": 0, "raw_ptr_arg": 0, "sites": []},
        "seam": {"raw_ptr_ret": 0, "raw_ptr_arg": 0},
        "ffi_type_surface": {"naked_total": 0, "wrapped_bypass": 0,
                             "unwrapped": 0, "scalar": 0, "unwrapped_tags": {}},
        "void_ptr": {"total": 0, "sanctioned": 0, "smell": 0, "smell_sites": []},
        "raw_field_proj": {"total": 0, "sanctioned": 0, "smell": 0, "smell_sites": []},
    }
    fts, vp, rfp = g["ffi_type_surface"], g["void_ptr"], g["raw_field_proj"]
    for rel, c in clean_files.items():
        impl_spans = _all_impl_spans(c)
        trait_spans = _all_trait_spans(c)
        seam_spans = _seam_spans(c)
        ffx = ffx_spans.get(rel, [])
        decl_spans = [(m.start(), m.end()) for m in _RE_DEFINE_TYPE.finditer(c)]

        def outside(pos):
            return not (_in_spans(pos, impl_spans) or _in_spans(pos, ffx)
                        or _in_spans(pos, seam_spans))

        # raw-ptr returns: seam (expected) vs outside-impl (smell)
        ret_lines = []
        for m in _RE_RAW_PTR_RET.finditer(c):
            pos = m.start()
            if _in_spans(pos, seam_spans):
                g["seam"]["raw_ptr_ret"] += 1
            elif outside(pos):
                g["outside_impl"]["raw_ptr_ret"] += 1
                ret_lines.append(_line_of(c, pos))
        # raw-ptr args
        out_arg, arg_lines = _raw_ptr_args_where(c, outside)
        seam_arg, _ = _raw_ptr_args_where(c, lambda p: _in_spans(p, seam_spans))
        g["outside_impl"]["raw_ptr_arg"] += out_arg
        g["seam"]["raw_ptr_arg"] += seam_arg
        lines = sorted(set(ret_lines + arg_lines))
        if lines:
            g["outside_impl"]["sites"].append(
                {"file": rel, "count": len(lines), "lines": lines})

        # naked ffi::Ident type uses, partitioned by has-define_*ctype!
        for m in _RE_FFI_TYPE_USE.finditer(c):
            pos = m.start()
            if (_in_spans(pos, impl_spans) or _in_spans(pos, decl_spans)
                    or _in_spans(pos, ffx)):
                continue
            tag = m.group(1)
            fts["naked_total"] += 1
            if tag in wrapped:
                fts["wrapped_bypass"] += 1
            elif tag in _FFI_SCALAR_ALIASES:
                fts["scalar"] += 1
            else:
                fts["unwrapped"] += 1
                fts["unwrapped_tags"][tag] = fts["unwrapped_tags"].get(tag, 0) + 1

        # void pointers: sanctioned (ffi_export / seam) vs smell
        vlines = []
        for m in _RE_RAW_PTR_VOID.finditer(c):
            pos = m.start()
            vp["total"] += 1
            if _in_spans(pos, ffx) or _in_spans(pos, seam_spans):
                vp["sanctioned"] += 1
            else:
                vp["smell"] += 1
                vlines.append(_line_of(c, pos))
        if vlines:
            vp["smell_sites"].append({"file": rel, "count": len(vlines), "lines": vlines})

        # raw field projections: sanctioned (impl/trait accessor body, seam,
        # ffi_export) vs smell (a free-function/port body bypassing the accessor)
        plines = []
        for m in _RE_RAW_FIELD_PROJ.finditer(c):
            pos = m.start()
            rfp["total"] += 1
            if (_in_spans(pos, impl_spans) or _in_spans(pos, trait_spans)
                    or _in_spans(pos, seam_spans) or _in_spans(pos, ffx)):
                rfp["sanctioned"] += 1
            else:
                rfp["smell"] += 1
                plines.append(_line_of(c, pos))
        if plines:
            rfp["smell_sites"].append({"file": rel, "count": len(plines), "lines": plines})

    g["outside_impl"]["sites"].sort(key=lambda s: -s["count"])
    vp["smell_sites"].sort(key=lambda s: -s["count"])
    rfp["smell_sites"].sort(key=lambda s: -s["count"])
    fts["unwrapped_tags"] = dict(sorted(fts["unwrapped_tags"].items(),
                                        key=lambda kv: -kv[1]))
    return g


def audit(rust_root: Path, *, all=False, names=None, crate=None, mod=None,
          file=None) -> dict:
    rust_root = Path(rust_root)
    raw, clean = load_tree(rust_root)
    types_idx, syms_idx = build_index(raw, clean)
    # mod ffi_export gateway spans per file — excluded from naked everywhere.
    ffx_spans = {rel: _ffi_export_spans(c) for rel, c in clean.items()}
    type_tags, sym_names = resolve_seeds(
        rust_root, types_idx, syms_idx,
        all=all, names=names, crate=crate, mod=mod, file=file)

    entries = []
    unwrapped = 0
    for tag in type_tags:
        home, wrapper = types_idx.get(tag, (None, None))
        spans = _type_impl_region(clean[home], wrapper) if home else []
        own = _own_counts(clean[home], spans, tag, wrapper,
                          ffx=ffx_spans.get(home, [])) if home else None
        if home is None:
            unwrapped += 1
        nk_total, nk_sites = _footprint(
            _re_ffi_bare_type(tag), clean, home, spans, ffx_spans)
        entries.append({
            "name": tag, "kind": "type", "wrapper": wrapper, "home": home,
            "own": own, "naked": nk_total, "naked_sites": nk_sites,
        })
    for fn in sym_names:
        home = syms_idx.get(fn)
        spans = _sym_impl_region(raw[home], clean[home], fn) if home else []
        is_port = _is_port_symbol(clean[home], fn) if home else False
        own = _own_counts(clean[home], spans, fn, is_type=False,
                          ffx=ffx_spans.get(home, []), is_port=is_port) if home else None
        if home is None:
            unwrapped += 1
        nk_total, nk_sites = _footprint(
            re.compile(rf"\bffi::{re.escape(fn)}\s*\("), clean, home, spans, ffx_spans)
        entries.append({
            "name": fn, "kind": "symbol", "wrapper": fn if home else None,
            "stage": ("port" if is_port else "wrap") if home else None,
            "home": home, "own": own, "naked": nk_total, "naked_sites": nk_sites,
        })

    glob = _global_scan(clean, types_idx, ffx_spans)

    totals = {
        "naked_ffi": sum(e["naked"] for e in entries),
        "naked_type": sum(e["naked"] for e in entries if e["kind"] == "type"),
        "naked_call": sum(e["naked"] for e in entries if e["kind"] == "symbol"),
        "own_unsafe_blocks": sum((e["own"] or {}).get("unsafe_blocks", 0) for e in entries),
        "own_raw_ptr": sum(
            sum((e["own"] or {}).get(k, 0) for k in ("raw_ptr_ret", "raw_ptr_arg", "raw_ptr_body"))
            for e in entries),
        "own_raw_ptr_void": sum((e["own"] or {}).get("raw_ptr_void", 0) for e in entries),
        "own_raw_ptr_body_smell": sum((e["own"] or {}).get("raw_ptr_body_smell", 0) for e in entries),
        "own_ffi_self_smell": sum((e["own"] or {}).get("ffi_self_smell", 0) for e in entries),
        "own_wrapper_ref": sum((e["own"] or {}).get("wrapper_ref", 0) for e in entries),
        "global_void_ptr_smell": glob["void_ptr"]["smell"],
        "global_raw_field_proj_smell": glob["raw_field_proj"]["smell"],
        "unwrapped": unwrapped,
    }
    return {
        "resolved": {"types": len(type_tags), "symbols": len(sym_names),
                     "unwrapped": unwrapped},
        "entries": entries,
        "global": glob,
        "totals": totals,
    }
