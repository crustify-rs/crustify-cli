"""Orchestration for the ``scaffold`` command — the crates.json-driven ``.rs``
oracle.

``crates.json`` (authored by ``CrustifyScaffolder``) maps every in-scope C
symbol/type to the unique Rust ``.rs`` that homes it. ``scaffold`` is the
mechanical front door:

  - **query** (default) — resolve the selection (``--name`` / ``--file`` /
    ``--dir`` / ``--all``) to its ``.rs`` path(s) and print them. On a ``--name``
    lookup MISS, spawn ``CrustifyScaffolder`` to place the missing seeds, then
    re-resolve.
  - **``--create``** — materialize the resolved ``.rs`` on disk (stub + module
    tree), lazily and idempotently. ``rust/`` grows as users ask for it.
  - **``--validate``** — the crates.json consistency gate.

Placement comes from ``crates.json``, not the former file-grained composer.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_BLOCK_START = "// crustify:modules:start"
_BLOCK_END = "// crustify:modules:end"

# Rust strict + reserved keywords — illegal as a bare `mod` identifier. Raw-able
# ones (all but crate/self/super/Self) are emitted as `r#<kw>` (resolves to the
# unprefixed `<kw>.rs`); the four non-raw-able ones get a trailing `_` instead
# (both file and decl, via `_safe_rs`), since `r#` can't wrap them.
_NON_RAW_KEYWORDS = frozenset({"crate", "self", "super", "Self"})
_RUST_KEYWORDS = frozenset(
    "as break const continue crate else enum extern false fn for if impl in let "
    "loop match mod move mut pub ref return self Self static struct super trait "
    "true type unsafe use where while async await dyn abstract become box do "
    "final macro override priv typeof unsized virtual yield try gen".split())


def scaffold(
    target: Path,
    *,
    all: bool = False,
    dir: str | None = None,
    file: str | None = None,
    name: list[str] | None = None,
    create: bool = False,
    validate: bool = False,
) -> None:
    from crustify import crates
    from crustify.layout import Layout

    layout = Layout.discover(target)

    if validate:
        _validate(layout)
        return

    doc = crates.load(layout)

    # --- resolve the entries to act on, spawning the agent for --name/--all misses
    if all:
        if not (doc.get("crates")):
            _spawn(target)
            doc = crates.load(layout)
        entries = _all_entries(doc)
    elif name:
        misses = [n for n in name if crates.lookup(doc, n) is None]
        if misses:
            # Validate unplaced seeds against the in-scope universe BEFORE spawning
            # the LLM scaffolder — a typo'd / out-of-scope name is otherwise placed
            # as a phantom seed (an agent run authoring crates.json for a symbol that
            # does not exist), instead of failing fast like `query syms --name` does.
            universe = _in_scope_names(layout, target)
            unknown = [n for n in misses if n not in universe] if universe else []
            if unknown:
                raise SystemExit(
                    "scaffold: not in scope (unknown symbol/type): "
                    + ", ".join(repr(n) for n in unknown))
            _spawn(target, seeds=misses)
            doc = crates.load(layout)
        entries, missing = _entries_for_names(doc, name)
        for n in missing:
            print(f"scaffold: {n}: not placed", file=sys.stderr)
        if missing and not entries:
            raise SystemExit(1)
    elif file is not None or dir is not None:
        entries = _entries_for_path(doc, layout, target, file, dir)
        if not entries:
            raise SystemExit(
                f"scaffold: nothing in crates.json under "
                f"{file if file is not None else dir!r}. Run `scaffold --all` first.")
    else:
        raise SystemExit("scaffold: pass one of --all / --name / --file / --dir / --validate.")

    # --- act
    if create:
        stats = _materialize(layout, entries, _elem_aliases(layout),
                             _scope_map(layout, target), _field_map(layout))
        mstats = _materialize_manifests(layout, doc)
        print(f"[crustify scaffold --create] {stats}{mstats} → {layout.rust}")
    else:
        seen: set[str] = set()
        for e in entries:
            p = _full_rs(layout, e["crate_path"], e["rs"])
            if p not in seen:
                seen.add(p)
                print(p)


def _spawn(target: Path, seeds: list[str] | None = None) -> None:
    from crustify.agents.scaffolder import CrustifyScaffolder
    CrustifyScaffolder(target, seeds=seeds).run()


def _in_scope_names(layout, target: Path) -> set[str]:
    """Every in-scope symbol/type name the scaffolder is allowed to place —
    the authoritative ``scope.json`` universe (port ∪ wrap). Functions, globals
    and macros key on ``name``; types key on ``name`` (port) or ``type`` (wrap).
    Empty set if ``scope.json`` is absent/unreadable (callers gate on emptiness)."""
    scope_path = layout.scope(target)
    try:
        doc = json.loads(scope_path.read_text())
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for section in (doc.get("port") or {}), (doc.get("wrap") or {}):
        for group in ("functions", "globals", "macros", "types"):
            for e in section.get(group) or []:
                for key in ("name", "type"):
                    if e.get(key):
                        names.add(e[key])
    return names


def _scope_map(layout, target: Path) -> dict[str, str]:
    """``name -> "port" | "wrap"`` from scope.json — the anchor-verb selector: a
    wrap-scope item anchors as ``// Wraps:``, a port-scope one as ``// Replaces:``.
    Types key on ``name`` (port) / ``type`` (wrap); functions/globals on ``name``.
    Port is applied second so it wins on the (rare) overlap. Empty when scope.json
    is absent -> everything falls back to ``Replaces``."""
    try:
        doc = json.loads(layout.scope(target).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for sec in ("wrap", "port"):   # port second -> overrides on overlap
        section = doc.get(sec) or {}
        for group in ("functions", "globals", "types"):
            for e in section.get(group) or []:
                for key in ("name", "type"):
                    if e.get(key):
                        out[e[key]] = sec
    return out


def _field_map(layout) -> dict[str, list[str]]:
    """``type tag -> [field names]`` from the analysis tree's ``types.json`` — the
    source for a type's ``// Field:`` accessor anchors (crates.json / scope.json
    carry no field lists). The field set is already scope-shaped by the type
    composer (wrap = port-touched subset, port = full layout). Empty when the
    analysis tree is absent."""
    out: dict[str, list[str]] = {}
    tree = layout.analysis
    if not tree.exists():
        return out
    for p in tree.rglob("types.json"):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        for e in doc.get("types", []):
            tag = e.get("type")
            if tag:
                out[tag] = [f["name"] for f in (e.get("fields") or [])
                            if isinstance(f, dict) and f.get("name")]
    return out


def _validate(layout) -> None:
    from crustify import crates
    errs = crates.validate(crates.load(layout))
    if errs:
        for e in errs:
            print(f"scaffold: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[crustify scaffold --validate] crates.json OK ({layout.crates_json})")


# --------------------------------------------------------------- entry resolution

def _entry(cname: str, c: dict, rs: str, rr: dict) -> dict:
    return {"crate": cname, "crate_path": c.get("crate_path"), "rs": rs,
            "members": rr.get("members") or {}, "def_file": rr.get("def_file")}


def _all_entries(doc: dict) -> list[dict]:
    out = []
    for cname, c in (doc.get("crates") or {}).items():
        for m in (c.get("modules") or {}).values():
            for rs, rr in (m.get("rs") or {}).items():
                out.append(_entry(cname, c, rs, rr))
    return out


def _entries_for_names(doc: dict, names: list[str]) -> tuple[list[dict], list[str]]:
    from crustify import crates
    entries, missing, seen = [], [], set()
    for n in names:
        hit = crates.lookup(doc, n)
        if hit is None:
            missing.append(n)
            continue
        key = (hit["crate"], hit["rs"])
        if key in seen:
            continue
        seen.add(key)
        c = doc["crates"][hit["crate"]]
        rr = c["modules"][hit["module"]]["rs"][hit["rs"]]
        entries.append(_entry(hit["crate"], c, hit["rs"], rr))
    return entries, missing


def _entries_for_path(doc, layout, target, file, dir) -> list[dict]:
    want = _path_filter(layout, target, file if file is not None else dir)
    out = []
    for cname, c in (doc.get("crates") or {}).items():
        for m in (c.get("modules") or {}).values():
            for rs, rr in (m.get("rs") or {}).items():
                files = [f for f in [rr.get("def_file"), *(rr.get("decl_files") or [])] if f]
                if file is not None:
                    hit = any(f == want for f in files)
                else:
                    hit = any(f == want or f.startswith(want + "/") for f in files)
                if hit:
                    out.append(_entry(cname, c, rs, rr))
    return out


def _path_filter(layout, target, sel: str) -> str:
    import posixpath
    from crustify.layout import ROOT_TARGET
    rel = layout.rel_target(target)
    base = "" if rel == ROOT_TARGET else rel
    return posixpath.normpath(posixpath.join(base, sel)).lstrip("/")


# ------------------------------------------------------------------- materialize

def _elem_aliases(layout) -> dict[str, list[str]]:
    """`element-type tag -> [array-cluster tag, …]` from the deps-dag.

    An array cluster is a foundational leaf: the generic `CVec<T, S>` is
    T-agnostic and a synthetic tag has no dependents, so it wraps FIRST. Its
    typed `CVec<T>` aliases reference element wrappers that wrap LATER — the dag
    records those as the cluster's `fallback` (the cluster renders the elem raw
    `ffi::T`, back-filled once the elem lands). So the alias is scaffolded in the
    ELEMENT's module (owner = the elem) as `// Alias: <cluster>` — the back-fill
    site — not in the cluster's. The dag has already resolved elem strings to
    canonical tags and dropped scalars (no wrapper); missing dag → empty."""
    out: dict[str, list[str]] = {}
    root = getattr(layout, "analysis", None)
    if root is None:
        return out
    try:
        doc = json.loads((root / "deps-dag.json").read_text())
    except (OSError, ValueError):
        return out
    for layer in doc.get("layers", []):
        for n in layer:
            if not isinstance(n, dict) or n.get("subkind") != "array":
                continue
            for et in (n.get("fallback") or {}).get("types", []):
                out.setdefault(et, []).append(n["id"])
    for et in out:
        out[et].sort()
    return out


def _materialize(layout, entries: list[dict],
                 alias_map: dict[str, list[str]] | None = None,
                 scope_map: dict[str, str] | None = None,
                 field_map: dict[str, list[str]] | None = None) -> str:
    alias_map = alias_map or {}
    scope_map = scope_map or {}
    field_map = field_map or {}
    created = updated = preserved = links = 0
    for e in entries:
        crate_dir = layout.repo_root / e["crate_path"]
        rs = _safe_rs(e["rs"])
        rs_path = crate_dir / rs
        if not rs_path.exists():
            rs_path.parent.mkdir(parents=True, exist_ok=True)
            rs_path.write_text(_stub(e, alias_map, scope_map, field_map))
            created += 1
        elif _merge_anchors(rs_path, e, alias_map, scope_map, field_map):
            # File already there but newly-homed members (or a cluster's element
            # aliases) lack an anchor — `--create` is additive/idempotent (the
            # docstring contract), so add the missing ones rather than leaving
            # them unscaffolded.
            updated += 1
        else:
            preserved += 1
        links += _wire(crate_dir, rs)
    return (f"{created} stub(s) created, {updated} updated, "
            f"{preserved} preserved, {links} module link(s)")


def _materialize_manifests(layout, doc: dict) -> str:
    """Write each in-tree wrapper crate's ``Cargo.toml`` + register it as a
    workspace member, idempotently — so the wrap/port output is a real compilable
    package (``cargo check``-able), not a manifest-less module tree the agent has
    to invent. A wrapper crate depends on its ``-sys`` FFI crate, the ``crustify``
    support crate (the ``define_type!`` / ``C*`` API), and the wrapper crates of
    its ``crates.json`` ``depends_on``. The ``// SAFETY:`` discipline is enforced
    by denying ``clippy::undocumented_unsafe_blocks`` workspace-wide, inherited
    via ``[lints] workspace = true`` (the ``-sys`` crates don't opt in, keeping
    their generated-code ``allow``)."""
    crustify_crate = Path(__file__).resolve().parents[3] / "crustify-crate"
    crates = doc.get("crates") or {}
    # A wrapper crate becomes "real" once it has a scaffolded lib.rs (members were
    # placed there); skip empty ones (e.g. a foreign lib with no wrappers). NOTE:
    # `in_tree` is NOT the gate — foreign-lib wrapper crates (libc, libpthread)
    # are `in_tree=False` yet hold wrapper code (e.g. the pthread_mutex_t wrapper)
    # that must compile and obey the SAFETY discipline just like the project crate.
    real = {n: c for n, c in crates.items()
            if (layout.repo_root / c["crate_path"] / "src" / "lib.rs").exists()}
    ws_toml = layout.rust / "Cargo.toml"
    written = members = 0
    for name, c in real.items():
        crate_dir = layout.repo_root / c["crate_path"]
        manifest = crate_dir / "Cargo.toml"
        if not manifest.exists():
            manifest.write_text(
                _crate_manifest(name, c, crate_dir, layout, crustify_crate, real))
            written += 1
        members += _add_workspace_member(
            ws_toml, os.path.relpath(crate_dir, layout.rust).replace(os.sep, "/"))
    if real:
        _ensure_workspace_lints(ws_toml)
    return f", {written} manifest(s), {members} member(s)" if (written or members) else ""


def _crate_manifest(name: str, c: dict, crate_dir: Path, layout,
                    crustify_crate: Path, real: dict) -> str:
    def rel(p: Path) -> str:
        return os.path.relpath(p, crate_dir).replace(os.sep, "/")
    # `crustify-crate` lives OUTSIDE `rust/` (up at the git root), so a relative
    # path is depth-sensitive and breaks inside a git worktree (which sits two
    # levels deeper under `crustify/.worktrees/<slug>/`). Use an absolute path so
    # it resolves identically from the main checkout and from any worktree.
    deps = [f'crustify = {{ path = "{crustify_crate}" }}']
    # The crate's -sys FFI dep: explicit `sys_crate`, else the `<name>-sys`
    # convention (foreign wrapper crates carry no `sys_crate` but still reference
    # `ffi::` from their bindgen crate, e.g. libpthread → libpthread-sys).
    sysdir = (layout.repo_root / c["sys_crate"]) if c.get("sys_crate") else None
    if sysdir is None and (layout.rust / f"{name}-sys").exists():
        sysdir = layout.rust / f"{name}-sys"
    if sysdir is not None:
        deps.append(f'{sysdir.name} = {{ path = "{rel(sysdir)}" }}')
    for dep in c.get("depends_on") or []:
        dc = real.get(dep)                       # depend only on wrapper crates that exist
        if dc:
            deps.append(
                f'{dep} = {{ path = "{rel(layout.repo_root / dc["crate_path"])}" }}')
    return ("[package]\n"
            f'name = "{name}"\n'
            'version = "0.0.0"\n'
            'edition = "2024"\n\n'
            "[dependencies]\n" + "\n".join(deps) + "\n\n"
            "[lints]\nworkspace = true\n")


def _add_workspace_member(ws_toml: Path, member: str) -> int:
    """Insert ``member`` into the workspace ``members`` array, idempotent."""
    if not ws_toml.exists():
        return 0
    text = ws_toml.read_text()
    if re.search(rf'(?m)^\s*"{re.escape(member)}"\s*,', text):
        return 0
    new = re.sub(r'(?ms)(members\s*=\s*\[.*?\n)(\s*\])',
                 lambda m: m.group(1) + f'    "{member}",\n' + m.group(2),
                 text, count=1)
    if new == text:
        return 0
    ws_toml.write_text(new)
    return 1


def _ensure_workspace_lints(ws_toml: Path) -> None:
    """Deny ``clippy::undocumented_unsafe_blocks`` workspace-wide (every ``unsafe``
    block must carry a ``// SAFETY:`` comment). Idempotent."""
    if not ws_toml.exists():
        return
    text = ws_toml.read_text()
    if "undocumented_unsafe_blocks" in text:
        return
    if not text.endswith("\n"):
        text += "\n"
    ws_toml.write_text(
        text + "\n[workspace.lints.clippy]\n"
        "# Every `unsafe` block must carry a `// SAFETY:` justification.\n"
        'undocumented_unsafe_blocks = "deny"\n'
        "# One Rust module per C source file homed in a same-named dir is the\n"
        "# scaffolder's layout, not a smell (e.g. `odb::odb`).\n"
        'module_inception = "allow"\n')


def _merge_anchors(rs_path: Path, e: dict,
                   alias_map: dict[str, list[str]] | None = None,
                   scope_map: dict[str, str] | None = None,
                   field_map: dict[str, list[str]] | None = None) -> int:
    """Add anchors missing from the existing managed `.rs`: for each member, its
    item anchor by scope (`// Wraps:` wrap / `// Replaces:` port) + todo (macros
    are never anchored), a type's `// Field:` accessor anchors, and — for a type
    that is an array cluster's element — a `// Alias: <cluster>` + todo per
    arraying cluster, inserted right after that ELEMENT's item-anchor line so it
    lands in the element's region (its owner; the back-fill site for the cluster's
    raw alias). Idempotent (skips anchors already present, filled `///` or not).
    Returns the number of anchors added."""
    alias_map = alias_map or {}
    scope_map = scope_map or {}
    field_map = field_map or {}
    text = rs_path.read_text()
    added = 0
    new: list[str] = []
    for kind in ("types", "functions", "globals"):
        for nm in e["members"].get(kind) or []:
            # Verb-agnostic match (won't duplicate a fresh-composed anchor of
            # either verb).
            if not re.search(
                    rf"(?m)^\s*//+\s*(?:Replaces|Wraps):\s*{re.escape(nm)}\b", text):
                verb = "Wraps" if scope_map.get(nm) == "wrap" else "Replaces"
                new += [f"// {verb}: {nm}", _TODO, ""]
                if kind == "types":
                    for fld in field_map.get(nm, ()):
                        new += [f"// Field: {fld}", _TODO, ""]
                added += 1
    if new:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n".join(new) + "\n"
    # element aliases — one `// Alias: <cluster>` per arraying cluster, inserted
    # into THAT element's region (so an element arrayed by two clusters gets a
    # sub-anchor under each — they're distinct CVec<Elem,S> aliases over distinct
    # strategies). The presence check is region-scoped.
    for nm in e["members"].get("types") or []:
        clusters = alias_map.get(nm)
        if not clusters:
            continue
        mrep = re.search(rf"(?m)^[ \t]*//+\s*(?:Replaces|Wraps):\s*{re.escape(nm)}\b.*\n", text)
        if not mrep:
            continue
        region_start = mrep.end()
        nxt = re.search(r"(?m)^[ \t]*//+\s*(?:Replaces|Wraps):", text[region_start:])
        region = text[region_start:region_start + (nxt.start() if nxt else len(text))]
        ins: list[str] = []
        for ct in clusters:
            if re.search(rf"(?m)^\s*//+\s*Alias:\s*{re.escape(ct)}\b", region):
                continue
            ins += [f"// Alias: {ct}", _TODO, ""]
        if ins:
            text = text[:region_start] + "\n".join(ins) + "\n" + text[region_start:]
            added += len(ins) // 3
    if added:
        rs_path.write_text(text)
    return added


_TODO = "// crustify:todo"  # matches _schedule._TODO; a surviving one = pending


def _stub(e: dict, alias_map: dict[str, list[str]] | None = None,
          scope_map: dict[str, str] | None = None,
          field_map: dict[str, list[str]] | None = None) -> str:
    # Each member is laid as an item anchor whose verb is its scope — `// Wraps:`
    # for a wrap-scope item, `// Replaces:` for a port-scope one (a native Rust
    # item: type / function / global) — followed by a `crustify:todo` placeholder.
    # Macros are NOT anchored: bindgen owns their `ffi::` bindings /
    # `crustify_<NAME>` shims and the C `#define` stays, so the port/wrap stages
    # never fill a macro. A type additionally gets one `// Field: <name>` accessor
    # anchor per field, and — if it is an array cluster's element — one
    # `// Alias: <cluster>` sub-anchor per arraying cluster (its typed
    # `CVec<Self, ClusterStrategy>` alias), all laid right after the item anchor so
    # they bind to it as the owner. The wrap/port AGENT locates each by its anchor,
    # fills it, and promotes `//` -> `///` while dropping the todo. This is the
    # agent's fill contract — the scheduler schedules blindly and no longer reads
    # these markers, so the verb + todo are load-bearing for the agent, not the
    # scheduler.
    alias_map = alias_map or {}
    scope_map = scope_map or {}
    field_map = field_map or {}
    src = e.get("def_file") or "(external — no in-tree source)"
    lines = ["//! crustify:managed — generated module skeleton.",
             "//!",
             f"//! C source: {src}",
             "//! Each item below is a scaffolded anchor the wrap/port stage fills.",
             ""]
    m = e["members"]
    any_member = False
    for kind in ("types", "functions", "globals"):
        for nm in m.get(kind) or []:
            verb = "Wraps" if scope_map.get(nm) == "wrap" else "Replaces"
            lines += [f"// {verb}: {nm}", _TODO, ""]
            if kind == "types":
                for fld in field_map.get(nm, ()):
                    lines += [f"// Field: {fld}", _TODO, ""]
            for ct in alias_map.get(nm, ()):
                lines += [f"// Alias: {ct}", _TODO, ""]
            any_member = True
    if not any_member:
        lines.append("// (no members homed here yet)")
    return "\n".join(lines) + "\n"


def _wire(crate_dir: Path, rs: str) -> int:
    """Declare the module path of `rs` up the lib.rs / mod.rs tree, idempotent."""
    parts = Path(rs).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    n = 0
    modfile = crate_dir / "src" / "lib.rs"
    accum = crate_dir / "src"
    for i, p in enumerate(parts):
        n += _add_pub_mod(modfile, p)
        if i < len(parts) - 1:
            accum = accum / p
            modfile = accum / "mod.rs"
    return n


def _add_pub_mod(modfile: Path, name: str) -> int:
    """Add `pub mod <name>;` to modfile's managed block (creating the file if
    absent), idempotent. Returns 1 if a declaration was added."""
    # `in.rs` etc. are valid files but `pub mod in;` is illegal — use a raw
    # identifier (`pub mod r#in;`, which still resolves to `in.rs`).
    tok = f"r#{name}" if name in _RUST_KEYWORDS and name not in _NON_RAW_KEYWORDS else name
    entry = f"pub mod {tok};"
    if modfile.exists():
        text = modfile.read_text()
        if entry in text:
            return 0
        if _BLOCK_END in text:
            text = text.replace(_BLOCK_END, f"{entry}\n{_BLOCK_END}", 1)
        else:
            text += f"\n{_BLOCK_START}\n{entry}\n{_BLOCK_END}\n"
    else:
        modfile.parent.mkdir(parents=True, exist_ok=True)
        hdr = ("//! crate root (generated).\n" if modfile.name == "lib.rs"
               else "//! module tree (generated).\n")
        text = f"{hdr}\n{_BLOCK_START}\n{entry}\n{_BLOCK_END}\n"
    modfile.write_text(text)
    return 1


def _safe_rs(rs: str) -> str:
    """Sanitize a ``crates.json`` ``rs`` path into valid Rust module/file names.

    The oracle mirrors C file names into ``rs`` (e.g. ``pack-objects.c`` →
    ``pack-objects.rs``), but a Rust module — and therefore its ``.rs`` file —
    must be an identifier: every path segment's stem gets non-``[A-Za-z0-9_]``
    runs collapsed to ``_`` (``pack-objects.rs`` → ``pack_objects.rs``). Applied
    at the filesystem boundary so ``crates.json`` stays C-faithful while the
    emitted tree (files, ``pub mod`` declarations, query output) is valid Rust.
    Node→module resolution keys on the ``// Replaces:`` anchors, not file names,
    so renaming is transparent to the scheduler."""
    out = []
    for seg in Path(rs).parts:
        stem, dot, ext = seg.partition(".")
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem)
        if stem in _NON_RAW_KEYWORDS:        # can't be raw-id → disambiguate file too
            stem += "_"
        out.append(stem + dot + ext)
    return str(Path(*out)) if out else rs


def _full_rs(layout, crate_path: str, rs: str) -> str:
    return str(layout.repo_root / crate_path / _safe_rs(rs))
