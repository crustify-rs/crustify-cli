"""Access layer for ``crates.json`` — the whole-repo crate/module decomposition.

``crates.json`` is the placement oracle: it maps every in-scope C symbol/type
to the unique Rust ``.rs`` that homes it. It is authored outside crustify — by
hand, or by an orchestrator — against the layout in
``specs/crates.json``; this module is the consumer-side read / lookup /
validate API the ``scaffold`` command uses against it. Schema
authority: ``specs/crates.json``.

Shape (eliding ``_comment`` keys)::

    crates.<crate> = {
      kind, in_tree, crate_path, sys_crate?, depends_on: [crate, ...],
      modules.<module> = {
        rust_path,
        rs.<path> = {                 # single source of truth; the module's
          tu: str | None,             # source + header surface derive from these
          headers: [str, ...],
          members: {functions: [], types: [], callbacks: [],
                    macros: [], globals: []},
        },
      },
    }

``tu`` is the ONE translation unit the ``.rs`` mirrors (null when there is no
in-tree definition site — a callback, an extern, an opaque struct, or any
entity of an out-of-tree library); ``headers`` are the headers its members are
declared or defined through. Scalar ``tu`` + plural ``headers`` is what lets a
same-stem ``foo.c``/``foo.h`` pair be ONE Rust module instead of colliding.

Field meaning: ``docs/schemas/crates.md``. Layout: ``specs/crates.json``.
"""

from __future__ import annotations

import json
import re
from typing import Optional

# Member buckets. `macros` is homed for library attribution only — bindgen
# owns a macro's whole surface, so nothing anchors or wraps one.
_KINDS = ("functions", "types", "callbacks", "macros", "globals")


# --------------------------------------------------------------- load / save

def load(layout) -> dict:
    """Load ``crates.json`` (the repo-root artifact). An absent file yields an
    empty ``{"crates": {}}`` shell; a malformed file raises."""
    path = layout.crates_json
    if not path.exists():
        return {"crates": {}}
    return json.loads(path.read_text())


def save(layout, doc: dict) -> None:
    layout.crates_json.write_text(json.dumps(doc, indent=2) + "\n")


# ------------------------------------------------------------------- lookup

def lookup_all(doc: dict, name: str, *, file: str | None = None) -> list[dict]:
    """Every home of an entity, in crates.json order. Each is
    ``{crate, module, rs, crate_path, tu}``.

    Matches a member ``name`` in some ``.rs``; ``file`` narrows by provenance,
    matching EITHER the ``tu`` or any of the ``headers``. One qualifier rather
    than two because an entity is reached by whichever file the caller happens
    to hold — a header-defined struct is looked up by its header, a TU function
    by its ``.c``, and both may name the same ``.rs``.

    More than one home is legitimate, not a corruption: crates.json's key is
    ``(kind, name, tu)``, so a tag defined once per TU homes once per TU —
    ``struct ossl_record_layer_st`` is defined for TLS in
    ``ssl/record/methods/recmethod_local.h`` and again, privately, in
    ``ssl/quic/quic_tls.c``. File-local statics collide the same way. Callers
    that need exactly one (the wrap/port schedulers) pass ``file`` and take
    :func:`lookup`; callers reporting to a human show them all."""
    out: list[dict] = []
    for crate, c in (doc.get("crates") or {}).items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                members = r.get("members") or {}
                if not any(name in (members.get(k) or []) for k in _KINDS):
                    continue
                if file is not None and file != r.get("tu") and file not in (
                        r.get("headers") or []):
                    continue
                out.append({"crate": crate, "module": mod, "rs": rs,
                            "crate_path": c.get("crate_path"),
                            "tu": r.get("tu"),
                            "headers": list(r.get("headers") or [])})
    return out


def lookup(doc: dict, name: str, *, file: str | None = None) -> Optional[dict]:
    """First home of an entity, or ``None`` on a miss — :func:`lookup_all` for
    all of them. Ambiguity is resolved by ``file``, not by this function."""
    hits = lookup_all(doc, name, file=file)
    return hits[0] if hits else None


# ----------------------------------------------------------------- validate

def validate(doc: dict) -> list[str]:
    """Return a list of error strings (``[]`` = valid):

      - **uniqueness** — each ``(kind, name, tu)`` homes in exactly one ``.rs``
        (two ``.rs`` claiming it = a duplicate Rust definition). The ``tu``
        component keeps same-named file-local statics in different TUs apart.
      - **deps DAG** — ``depends_on`` has no cycle (Rust forbids crate cycles).
      - **well-formedness** — every ``depends_on`` names a defined crate.
      - **module-path collision** — no ``src/x.rs`` beside a ``src/x/`` claimed
        by another entry. Rust accepts ``x.rs`` OR ``x/mod.rs`` as the body of
        module ``x``, never both (E0761), and the scaffolder materializes a
        ``mod.rs`` for every directory it has to wire. The convention is
        ``src/x/x.rs`` (module ``x::x``), which the workspace's
        ``clippy::module_inception = "allow"`` exists for.
    """
    errors: list[str] = []
    crates = doc.get("crates") or {}

    seen: dict[tuple, str] = {}
    for crate, c in crates.items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                tu = r.get("tu")
                for kind, names in (r.get("members") or {}).items():
                    for nm in names or []:
                        key = (kind, nm, tu)
                        if key in seen:
                            errors.append(
                                f"duplicate: {kind} {nm!r} (tu={tu}) in "
                                f"{seen[key]} AND {crate}/{mod}/{rs}")
                        else:
                            seen[key] = f"{crate}/{mod}/{rs}"

    # `src/x.rs` + `src/x/…` -> two bodies for module `x`. Caught here because
    # it is a pure property of the authored paths: it needs no tree, and it
    # otherwise surfaces as a rustc E0761 that blocks `cargo check --workspace`
    # for every agent, far from its cause.
    for crate, c in crates.items():
        rs_paths = {rs for m in (c.get("modules") or {}).values()
                    for rs in (m.get("rs") or {})}
        dirs = {rs.rsplit("/", 1)[0] for rs in rs_paths if "/" in rs}
        for rs in sorted(rs_paths):
            stem = rs[:-3] if rs.endswith(".rs") else rs
            if stem in dirs:
                leaf = stem.rsplit("/", 1)[-1]
                errors.append(
                    f"module-path collision: {crate}/{rs} is the body of module "
                    f"`{leaf}`, but `{stem}/` also holds modules — rustc accepts "
                    f"only one (E0761). Home it at {stem}/{leaf}.rs instead.")

    names = set(crates)
    adj: dict[str, list[str]] = {}
    for crate, c in crates.items():
        deps = c.get("depends_on") or []
        for d in deps:
            if d not in names:
                errors.append(f"{crate}.depends_on names undefined crate {d!r}")
        adj[crate] = [d for d in deps if d in names]
    cyc = _find_cycle(adj)
    if cyc:
        errors.append("dependency cycle: " + " -> ".join(cyc))

    return errors


# C primitives / qualifiers that are never a user-type tag.
_NON_TAG = frozenset({
    "const", "volatile", "struct", "union", "enum", "unsigned", "signed",
    "void", "char", "short", "int", "long", "float", "double", "_Bool",
    "size_t", "ssize_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "intptr_t", "uintptr_t",
})


def _ref_tags(type_str: str | None) -> list[str]:
    """Candidate user-type tags in a C type string (a field or arg type)."""
    if not type_str:
        return []
    s = re.sub(r"[*\[\]()]", " ", type_str)
    return [t for t in s.split()
            if re.match(r"^[A-Za-z_]\w*$", t) and t not in _NON_TAG]


def validate_depends_on(doc: dict, type_entries) -> list[str]:
    """Cross-check ``depends_on`` against member placement and the composed
    records' BY-VALUE type references. Returns error strings (``[]`` = consistent).

    A struct homed to crate A that embeds **by value** an entity homed to crate B
    needs B's layout, so ``A.depends_on`` must contain B. A missing edge is
    silent downstream: bindgen derives the ``-sys`` blocklist and the
    ``pub use <dep>_sys::*`` imports from ``depends_on`` alone, so it mints its
    own copy of B's type rather than importing B's — two distinct Rust types for
    one C type, surfacing much later as a mismatch at ``cargo check``.

    Deliberately restricted to ``ref == "value"`` fields. A **pointer** to a
    foreign type needs no layout (an incomplete type binds fine), so a missing
    edge there is not a defect — and demanding one would report OpenSSL's real
    C-level circularity (``libcrypto``'s ``BIO_POLL_DESCRIPTOR.value.ssl`` is an
    ``SSL *``) as an authoring error, when the only fix would be a crate cycle
    Rust forbids. Pointer args/returns are excluded for the same reason, so
    syms.json is not read at all.

    Placement itself is NOT checked here (this reads it as ground truth); a
    misplaced member moves the reference and its owner together.
    """
    from pathlib import Path

    crates = doc.get("crates") or {}
    owner: dict[str, str] = {}       # member name -> owning crate
    for crate, c in crates.items():
        for m in (c.get("modules") or {}).values():
            for r in (m.get("rs") or {}).values():
                for names in (r.get("members") or {}).values():
                    for nm in names or []:
                        owner.setdefault(nm, crate)
    if not owner:
        return []

    declared = {c: set(v.get("depends_on") or []) for c, v in crates.items()}
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()   # (crate, dep) — one error per edge

    for t in type_entries:
        if True:
            tag = t.get("name") or t.get("type")
            home = owner.get(tag)
            if not home:
                continue
            for f in t.get("fields") or []:
                if f.get("ref") != "value":
                    continue
                for ref in _ref_tags(f.get("type")):
                    dep = owner.get(ref)
                    if (not dep or dep == home
                            or dep in declared.get(home, ())
                            or (home, dep) in seen):
                        continue
                    seen.add((home, dep))
                    errors.append(
                        f"{home}.depends_on is missing {dep!r}: "
                        f"{tag}.{f.get('name')} embeds {ref!r} by value, and "
                        f"{ref!r} is homed to {dep}")
    return errors


def _find_cycle(adj: dict[str, list[str]]) -> list[str] | None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = GRAY
        stack.append(n)
        for m in adj.get(n, []):
            if color.get(m, WHITE) == GRAY:
                return stack[stack.index(m):] + [m]
            if color.get(m, WHITE) == WHITE:
                r = dfs(m)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None

    for n in adj:
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return None
