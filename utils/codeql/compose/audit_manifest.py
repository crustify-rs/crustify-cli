"""Deterministic audit scanner (no LLM) — **entity-seeded, global search**.

Seeds are **types and symbols** (like wrap/port ``--name``); the scan is always
global. For each seed the audit reports:

  - **`own`** — the entity's *own implementation* surface: ``unsafe_pub_fn`` /
    ``unsafe_blocks`` / ``raw_ptr_ret`` (signature return) / ``raw_ptr_arg``
    (signature args) / ``raw_ptr_body`` (every other raw ptr `*mut`/`*const` in
    the body — casts, ``let x: *T``, turbofish, field/`static` decls),
    ``borrow_mut`` (items taking the wrapper as ``&mut self`` / ``&mut <Wrapper>``
    — a §8 smell, since the `UnsafeCell`-backed wrapper interior-mutates through
    ``&self``), and ``ffi_self`` (raw ``ffi::`` use *inside* its own impl, which
    is sanctioned),
    measured over its impl region:
      * **type** ``T`` (wrapper ``W``) — its ``define_type!(…, ffi::T)`` plus
        every ``impl W`` / ``impl … for W`` / ``impl_*!(W, …)`` block;
      * **symbol** ``S`` — the ``/// Replaces: S`` idiomatic fn plus its
        ``mod ffi_export`` re-export.
  - **`naked`** — every ``ffi::T`` use / ``ffi::S(`` call **outside** that impl
    region **and outside any ``mod ffi_export`` gateway** (DISCIPLINE §1.4 — the
    sanctioned raw C-ABI re-export region, where ``ffi::`` use is expected),
    anywhere in the tree (the wrapper being bypassed). With per-site file + line
    list.

A seed with no wrapper (no ``define_type!`` / ``/// Replaces:``) is reported
with ``wrapper: null`` and just its naked footprint, so unwrapped-but-used
entities surface.

Selectors resolve to the seed set (``compose.audit_manifest.resolve_seeds``):
``--name`` (explicit), ``--file`` / ``--dir`` (entities homed there),
``--crate`` (entities in that crate), ``--all`` (every wrapped type ∪ symbol).
All regex-driven over comment/string-stripped text.
"""

from __future__ import annotations

import re
from pathlib import Path

# Example output (`crustify <target> audit --name git_oid git_oid_cpy`):
#
#   {
#     "seed": "--name git_oid git_oid_cpy",
#     "resolved": {"types": 1, "symbols": 1, "unwrapped": 0},
#     "entries": [
#       {
#         "name": "git_oid", "kind": "type", "wrapper": "GitOid",
#         "home": "libgit2/src/include/git2/oid.rs",
#         "own": {"unsafe_pub_fn": 0, "unsafe_blocks": 1, "raw_ptr_ret": 0,
#                 "raw_ptr_arg": 0, "raw_ptr_body": 0, "borrow_mut": 0, "ffi_self": 1},
#         "naked": 15,
#         "naked_sites": [
#           {"file": "libgit2/src/src/libgit2/odb.rs", "count": 10, "lines": [134, 148, 153]},
#           {"file": "libgit2/src/src/util/alloc.rs",  "count": 2,  "lines": [154, 160]}
#         ]
#       },
#       {
#         "name": "git_oid_cpy", "kind": "symbol", "wrapper": "git_oid_cpy",
#         "home": "libgit2/src/src/libgit2/oid.rs",
#         "own": {"unsafe_pub_fn": 0, "unsafe_blocks": 1, "raw_ptr_ret": 0,
#                 "raw_ptr_arg": 0, "raw_ptr_body": 0, "borrow_mut": 0, "ffi_self": 0},
#         "naked": 0, "naked_sites": []
#       }
#       # an unwrapped seed (no define_type!/Replaces found) reports just its
#       # footprint: {"wrapper": null, "home": null, "own": null, "naked": N, ...}
#     ],
#     "totals": {"naked_ffi": 15, "own_unsafe_blocks": 2, "own_raw_ptr": 0,
#                "own_borrow_mut": 0, "unwrapped": 0}
#   }

_RE_UNSAFE_PUB_FN = re.compile(r"\bpub(?:\([^)]*\))?\s+unsafe\s+fn\b")
_RE_UNSAFE_BLOCK = re.compile(r"\bunsafe\s*\{")
_RE_RAW_PTR_RET = re.compile(r"->\s*\*\s*(?:const|mut)\b")
_RE_FN_SIG = re.compile(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\(")
_RE_RAW_PTR = re.compile(r"\*\s*(?:const|mut)\b")
_RE_BORROW_MUT_SELF = re.compile(r"&\s*(?:'\w+\s+)?mut\s+self\b")

_RE_DEFINE_TYPE = re.compile(r"define_type!\s*[({](.*?)[)}]", re.DOTALL)
_RE_DT_NAMES = re.compile(r"(\w+)\s*,\s*ffi::(\w+)")
_RE_CTYPE = re.compile(r"\bCType\s*<\s*ffi::(\w+)")
_RE_REPLACES = re.compile(r"//+\s*Replaces:\s*(\w+)")
_RE_MOD_FFI_EXPORT = re.compile(r"\bmod\s+ffi_export\b")

_SKIP_CRATE_SUFFIX = "-sys"


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
        elif c in "\"'":
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
    """``types[c_tag] = (home_rel, wrapper_name)`` from ``define_type!`` /
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
    ``define_type!`` / ``impl … <wrapper> … {}`` / ``impl_*!(… <wrapper> …)``."""
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


def _sym_impl_region(raw: str, clean: str, c_fn: str) -> list[tuple[int, int]]:
    """Spans of a symbol's own implementation: the ``/// Replaces: c_fn``
    idiomatic fn + its ``mod ffi_export`` re-export ``extern "C" fn c_fn``."""
    spans: list[tuple[int, int]] = []
    F = re.escape(c_fn)
    for m in re.finditer(rf"//+\s*Replaces:\s*{F}\b", raw):
        nf = re.search(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\([^;{]*\{", clean[m.end():])
        if nf:
            s = m.end() + nf.start()
            spans.append((s, _match_brace(clean, m.end() + nf.end() - 1)))
    for m in re.finditer(rf'\bextern\s+"C"\s+fn\s+{F}\s*\([^;{{]*\{{', clean):
        spans.append((m.start(), _match_brace(clean, m.end() - 1)))
    return spans


def _count_raw_ptr_args(text: str, lo: int, hi: int) -> int:
    total = 0
    for m in _RE_FN_SIG.finditer(text, lo, hi):
        b = m.end() - 1
        depth, j = 0, b
        while j < hi:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        total += len(_RE_RAW_PTR.findall(text[b:j]))
    return total


def _own_counts(clean: str, spans: list[tuple[int, int]], c_tag: str,
                wrapper: str | None = None) -> dict:
    """The surface counts + ``ffi_self`` over the union of impl spans.

    ``borrow_mut`` — items that take the wrapper as ``&mut`` (``&mut self`` or
    ``&mut <Wrapper>``). Wrappers are `UnsafeCell`-backed (interior mutability),
    so methods should take ``&self``; a ``&mut`` borrow is a §8 discipline smell."""
    def in_region(pos):
        return _in_spans(pos, spans)
    ffi_self = sum(1 for m in re.finditer(rf"\bffi::{re.escape(c_tag)}\b", clean)
                   if in_region(m.start()))
    borrow_mut = sum(1 for m in _RE_BORROW_MUT_SELF.finditer(clean) if in_region(m.start()))
    if wrapper:
        borrow_mut += sum(1 for m in re.finditer(
            rf"&\s*(?:'\w+\s+)?mut\s+{re.escape(wrapper)}\b", clean) if in_region(m.start()))
    # Signature raw-ptr surface (return + args); everything else is body:
    # `as *T` casts, `let x: *T`, turbofish `::<*T>`, struct/field decls, etc.
    raw_ret = sum(1 for m in _RE_RAW_PTR_RET.finditer(clean) if in_region(m.start()))
    raw_arg = sum(_count_raw_ptr_args(clean, a, b) for a, b in spans)
    raw_total = sum(1 for m in _RE_RAW_PTR.finditer(clean) if in_region(m.start()))
    return {
        "unsafe_pub_fn": sum(1 for m in _RE_UNSAFE_PUB_FN.finditer(clean) if in_region(m.start())),
        "unsafe_blocks": sum(1 for m in _RE_UNSAFE_BLOCK.finditer(clean) if in_region(m.start())),
        "raw_ptr_ret": raw_ret,
        "raw_ptr_arg": raw_arg,
        "raw_ptr_body": max(0, raw_total - raw_ret - raw_arg),
        "borrow_mut": borrow_mut,
        "ffi_self": ffi_self,
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
        own = _own_counts(clean[home], spans, tag, wrapper) if home else None
        if home is None:
            unwrapped += 1
        nk_total, nk_sites = _footprint(
            re.compile(rf"\bffi::{re.escape(tag)}\b"), clean, home, spans, ffx_spans)
        entries.append({
            "name": tag, "kind": "type", "wrapper": wrapper, "home": home,
            "own": own, "naked": nk_total, "naked_sites": nk_sites,
        })
    for fn in sym_names:
        home = syms_idx.get(fn)
        spans = _sym_impl_region(raw[home], clean[home], fn) if home else []
        own = _own_counts(clean[home], spans, fn) if home else None
        if home is None:
            unwrapped += 1
        nk_total, nk_sites = _footprint(
            re.compile(rf"\bffi::{re.escape(fn)}\s*\("), clean, home, spans, ffx_spans)
        entries.append({
            "name": fn, "kind": "symbol", "wrapper": fn if home else None,
            "home": home, "own": own, "naked": nk_total, "naked_sites": nk_sites,
        })

    totals = {
        "naked_ffi": sum(e["naked"] for e in entries),
        "own_unsafe_blocks": sum((e["own"] or {}).get("unsafe_blocks", 0) for e in entries),
        "own_raw_ptr": sum(
            sum((e["own"] or {}).get(k, 0) for k in ("raw_ptr_ret", "raw_ptr_arg", "raw_ptr_body"))
            for e in entries),
        "own_borrow_mut": sum((e["own"] or {}).get("borrow_mut", 0) for e in entries),
        "unwrapped": unwrapped,
    }
    return {
        "resolved": {"types": len(type_tags), "symbols": len(sym_names),
                     "unwrapped": unwrapped},
        "entries": entries,
        "totals": totals,
    }
