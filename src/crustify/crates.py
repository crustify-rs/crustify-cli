"""Read-only access and CLI operations for ``crates.json``.

``crates.json`` is the placement oracle: it maps every in-scope C symbol/type
to the unique Rust ``.rs`` that homes it. It is authored outside crustify — by
hand, or by an orchestrator — against the layout in
``specs/crates.json``; this module is the consumer-side locate / lookup /
validate API. It never materializes Rust source or Cargo files. Schema authority:
``specs/crates.json``.

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
import sys
from pathlib import Path
from typing import Optional

# Member buckets. `macros` is homed for library attribution only — the
# orchestrator-authored FFI crate owns its surface, so nothing anchors it.
_KINDS = ("functions", "types", "callbacks", "macros", "globals")
_NON_RAW_KEYWORDS = frozenset({"crate", "self", "super", "Self"})


# ---------------------------------------------------------------------- load

def load(layout) -> dict:
    """Load ``crates.json`` (the repo-root artifact). An absent file yields an
    empty ``{"crates": {}}`` shell; a malformed file raises."""
    path = layout.crates_json
    if not path.exists():
        return {"crates": {}}
    return json.loads(path.read_text())

# --------------------------------------------------------------- CLI operations

def locate(
    target: Path,
    *,
    all: bool = False,
    dir: str | None = None,
    file: str | None = None,
    name: list[str] | None = None,
) -> None:
    """Print the ``crates.json`` home of a read-only selection."""
    from crustify.layout import Layout

    layout = Layout.discover(target)
    doc = load(layout)

    if all:
        if not doc.get("crates"):
            raise SystemExit(
                f"crates locate: crates.json is empty ({layout.crates_json}). "
                "Populate it from specs/crates.json before locating items.")
        entries = _all_entries(doc)
    elif name:
        _require_one_home(doc, name, file)
        misses = [n for n in name if not lookup_all(doc, n, file=file)]
        if misses:
            raise SystemExit(
                "crates locate: not placed in crates.json: "
                + ", ".join(repr(n) for n in misses))
        entries, missing = entries_for_names(doc, name, file)
        for n in missing:
            print(f"crates locate: {n}: not placed", file=sys.stderr)
        if missing and not entries:
            raise SystemExit(1)
    elif file is not None or dir is not None:
        entries = _entries_for_path(doc, layout, target, file, dir)
        if not entries:
            raise SystemExit(
                "crates locate: nothing in crates.json under "
                f"{file if file is not None else dir!r}.")
    else:
        raise SystemExit(
            "crates locate: pass one of --all / --name / --file / --dir.")

    seen: set[Path] = set()
    for entry in entries:
        path = full_rs(layout, entry["crate_path"], entry["rs"])
        if path not in seen:
            seen.add(path)
            print(path)


def validate_command(target: Path) -> None:
    """Run the existing ``crates.json`` consistency checks and print a gate."""
    from crustify.layout import Layout

    layout = Layout.discover(target)
    doc = load(layout)
    errors = validate(doc)

    if errors:
        for error in errors:
            print(f"crates validate: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[crustify crates validate] crates.json OK ({layout.crates_json})")


def _entry(cname: str, crate: dict, rs: str, record: dict) -> dict:
    return {"crate": cname, "crate_path": crate.get("crate_path"), "rs": rs,
            "members": record.get("members") or {}, "tu": record.get("tu"),
            "headers": record.get("headers") or []}


def _all_entries(doc: dict) -> list[dict]:
    out = []
    for cname, crate in (doc.get("crates") or {}).items():
        for module in (crate.get("modules") or {}).values():
            for rs, record in (module.get("rs") or {}).items():
                out.append(_entry(cname, crate, rs, record))
    return out


def _require_one_home(doc: dict, names: list[str], file: str | None) -> None:
    """Refuse a name with several homes unless ``file`` disambiguates it."""
    bad = []
    for name in dict.fromkeys(names):
        hits = lookup_all(doc, name, file=file)
        if len(hits) > 1:
            bad.append((name, hits))
    if not bad:
        return
    lines = []
    for name, hits in bad:
        lines.append(f"  - {name}  ({len(hits)} homes)")
        for hit in hits:
            qualifier = hit.get("tu") or (hit.get("headers") or [None])[0]
            lines.append(
                f"      --file {qualifier or '?'}   → {hit['crate']}/{hit['rs']}")
    narrowed = " (already narrowed by --file)" if file else ""
    raise SystemExit(
        f"crates locate: {len(bad)} name(s) are placed in more than one module"
        f"{narrowed} — pass --file to pick one:\n" + "\n".join(lines))


def entries_for_names(
    doc: dict,
    names: list[str],
    file: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Return the unique ``crates.json`` homes for ``names``."""
    entries, missing, seen = [], [], set()
    for name in names:
        hits = lookup_all(doc, name, file=file)
        if not hits:
            missing.append(name)
            continue
        for hit in hits:
            key = (hit["crate"], hit["rs"])
            if key in seen:
                continue
            seen.add(key)
            crate = doc["crates"][hit["crate"]]
            record = crate["modules"][hit["module"]]["rs"][hit["rs"]]
            entries.append(_entry(hit["crate"], crate, hit["rs"], record))
    return entries, missing


def _entries_for_path(doc, layout, target, file, dir) -> list[dict]:
    wanted = _path_filter(layout, target, file if file is not None else dir)
    out = []
    for cname, crate in (doc.get("crates") or {}).items():
        for module in (crate.get("modules") or {}).values():
            for rs, record in (module.get("rs") or {}).items():
                files = [f for f in [record.get("tu"),
                                     *(record.get("headers") or [])] if f]
                hit = (any(f == wanted for f in files) if file is not None else
                       any(f == wanted or f.startswith(wanted + "/") for f in files))
                if hit:
                    out.append(_entry(cname, crate, rs, record))
    return out


def _path_filter(layout, target, selection: str) -> str:
    import posixpath

    rel = layout.rel_target(target)
    base = "" if rel in ("", ".") else rel
    return posixpath.normpath(posixpath.join(base, selection)).lstrip("/")


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


def _safe_rs(rs: str) -> str:
    """Map a C-faithful ``crates.json`` path to its physical Rust path."""
    out = []
    for segment in Path(rs).parts:
        stem, dot, extension = segment.partition(".")
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem)
        if stem in _NON_RAW_KEYWORDS:
            stem += "_"
        out.append(stem + dot + extension)
    return str(Path(*out)) if out else rs


def full_rs(layout, crate_path: str, rs: str) -> Path:
    """Return the physical Rust path for a ``crates.json`` home."""
    path = Path(crate_path)
    crate_dir = path if path.is_absolute() else layout.repo_root / path
    return crate_dir / _safe_rs(rs)


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
        module ``x``, never both (E0761), and the authored tree needs a
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
