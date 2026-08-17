"""Orchestration for the ``scaffold`` command — the crates.json-driven ``.rs``
oracle.

``crates.json`` maps every in-scope C symbol/type to the unique Rust ``.rs``
that homes it. It is authored OUTSIDE this stage — by hand or by an
orchestrator — and ``scaffold`` never writes it. This command is purely
mechanical:

  - **query** (default) — resolve the selection (``--name`` / ``--file`` /
    ``--dir`` / ``--all``) to its ``.rs`` path(s) and print them. A lookup MISS
    is a hard error naming the unplaced entities: placement is an authoring
    decision, so the stage reports the gap rather than guessing at one.
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

# The composer package lives at ``utils/codeql/compose/`` in the crustify
# checkout, not as an installed package. Mirrors wrap.py / port.py.
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))

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
        _validate(layout, target)
        return

    doc = crates.load(layout)

    # --- resolve the entries to act on; an unplaced selection is a hard error
    if all:
        if not (doc.get("crates")):
            raise SystemExit(
                f"scaffold: crates.json is empty ({layout.crates_json}). It is the "
                f"placement oracle and is authored outside this stage — populate it "
                f"(see specs/crates.json for the schema) before scaffolding.")
        entries = _all_entries(doc)
    elif name:
        _require_one_home(doc, name, file)
        misses = [n for n in name if not crates.lookup_all(doc, n, file=file)]
        if misses:
            # Split the miss two ways so the message is actionable: a name that is
            # not in scope at all is a typo / wrong target, while an in-scope name
            # is simply not placed yet in crates.json.
            universe = _in_scope_names(layout, target)
            unknown = [n for n in misses if n not in universe] if universe else []
            unplaced = [n for n in misses if n not in unknown]
            msg = []
            if unknown:
                msg.append("not in scope (unknown symbol/type): "
                           + ", ".join(repr(n) for n in unknown))
            if unplaced:
                msg.append("in scope but not placed in crates.json: "
                           + ", ".join(repr(n) for n in unplaced)
                           + " — add them to the oracle, then re-run.")
            raise SystemExit("scaffold: " + "; ".join(msg))
        entries, missing = _entries_for_names(doc, name, file)
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
        stats = _materialize(layout, entries,
                             _scope_map(layout, target), _field_map(layout, target))
        mstats = _materialize_manifests(layout, doc)
        print(f"[crustify-cli scaffold --create] {stats}{mstats} → {layout.rust}")
    else:
        seen: set[str] = set()
        for e in entries:
            p = _full_rs(layout, e["crate_path"], e["rs"])
            if p not in seen:
                seen.add(p)
                print(p)


def _in_scope_names(layout, target: Path) -> set[str]:
    """Every in-scope symbol/type name the scaffolder is allowed to place —
    the authoritative ``scope.json`` universe (port ∪ wrap). Functions, globals
    and macros key on ``name``; types key on ``name`` (port) or ``type`` (wrap).
    Empty set when scope cannot be composed (callers gate on emptiness)."""
    from crustify import scope as _scope_mod
    try:
        doc = _scope_mod.build(layout, target, stage="scaffold")
    except SystemExit:
        return set()
    names: set[str] = set()
    for section in (doc.get("target") or {}), (doc.get("import") or {}):
        for group in ("functions", "globals", "macros", "types"):
            for e in section.get(group) or []:
                for key in ("name", "type"):
                    if e.get(key):
                        names.add(e[key])
    return names


def _scope_map(layout, target: Path) -> dict[str, str]:
    """``name -> scope.TARGET | scope.IMPORT`` from scope.json — which section an
    item sits in. Types key on ``name``, with a ``type`` fallback for un-migrated
    records; functions/globals on ``name``. TARGET is applied second so it wins
    on the (rare) overlap. Empty when scope cannot be composed."""
    from crustify import scope as _scope_mod
    try:
        doc = _scope_mod.build(layout, target, stage="scaffold")
    except SystemExit:
        return {}
    out: dict[str, str] = {}
    for sec in ("import", "target"):   # target second -> overrides on overlap
        section = doc.get(sec) or {}
        for group in ("functions", "globals", "types"):
            for e in section.get(group) or []:
                for key in ("name", "type"):
                    if e.get(key):
                        out[e[key]] = sec
    return out


def _port_touched(layout, target) -> dict[str, set] | None:
    """``type tag -> {field names touched by PORT-scope code}``, or None when
    scope cannot be resolved (then every field is anchored, as before).

    Delegates to the oracle rather than re-deriving from the CSVs: the same
    answer `query types --fields --target-only` gives, so an anchor set and the
    field workset an agent is handed can never disagree. `scope_touched_index`
    is one pass over both access edges, cached — the per-type
    `_scope_touched_fields` would rescan them once per type.
    """
    from crustify.query import scope_touched_index

    # Only a genuinely ABSENT input returns None (a tree scaffolded before
    # `analyze scope` / `extract-ql` has run). Anything else is allowed to
    # raise: None means "anchor every field", so swallowing an error here would
    # quietly restore the over-anchoring this function exists to prevent, and
    # look like it worked.
    from crustify import scope as _scope_mod
    try:
        _scope_mod.build(layout, target, stage="scaffold")
    except SystemExit:
        return None
    idx = scope_touched_index(layout, target, "target")
    return {tag: {f for s in by_file.values() for f in s}
            for tag, by_file in idx.items()} or None


def _field_map(layout, target=None) -> dict[str, list[str]]:
    """``type tag -> [field names]`` from the analysis tree's ``types.json`` — the
    source for a type's field accessor anchors (crates.json / scope.json carry no
    field lists). Empty when the analysis tree is absent.

    Narrowed to the fields TARGET-section code actually touches, because an
    anchor is a request for an ACCESSOR and only the target side consumes one. The
    manifest's ``fields`` is the full declared layout for every type -- this
    function used to take it verbatim on the belief that the type composer had
    already scope-shaped it, which it never did. The cost of that was concrete:
    ``bio_st`` carried 16 anchors against 0 target-touched fields, ``ossl_provider_st``
    30 against 0, and agents filled them, so two thirds of the accessors emitted
    tree-wide serve only code inside the type's own module.

    Layout compatibility is unaffected -- a field still has to EXIST in the Rust
    struct, which the type's own definition anchor covers. This governs only
    which fields are owed a public accessor.

    The narrowing is near-total for import-section types (53 of 1,223 fields
    target-touched) and near-nil for target ones (2,158 of 2,214): a ported
    type is translated wholesale, so its own ported code touches its fields.
    """
    out: dict[str, list[str]] = {}
    if target is None:
        return out
    from crustify import manifests as _manifests
    touched = _port_touched(layout, target)
    if True:
        for e in _manifests.entries(layout, target, "types", stage="scaffold"):
            tag = e.get("name") or e.get("type")
            if not tag:
                continue
            names = [f["name"] for f in (e.get("fields") or [])
                     if isinstance(f, dict) and f.get("name")]
            if touched is not None:
                keep = touched.get(tag, set())
                names = [n for n in names if n in keep]
            out[tag] = names
    return out


def _validate(layout, target=None) -> None:
    from crustify import crates
    doc = crates.load(layout)
    errs = crates.validate(doc)
    # `depends_on` vs placement + the composed records' by-value type
    # references. Needs a target (records are composed per target, since scope
    # narrows them), so `--validate` without one checks only the pure
    # crates.json properties. bindgen derives the -sys blocklist and the
    # `pub use <dep>_sys::*` imports from `depends_on` alone, and a missing
    # edge there is silent until `cargo check` much later.
    if target is not None:
        from crustify import manifests as _manifests
        errs += crates.validate_depends_on(
            doc, _manifests.entries(layout, target, "types", stage="scaffold"))
    if errs:
        for e in errs:
            print(f"scaffold: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"[crustify-cli scaffold --validate] crates.json OK ({layout.crates_json})")


# --------------------------------------------------------------- entry resolution

def _entry(cname: str, c: dict, rs: str, rr: dict) -> dict:
    return {"crate": cname, "crate_path": c.get("crate_path"), "rs": rs,
            "members": rr.get("members") or {}, "tu": rr.get("tu"),
            "headers": rr.get("headers") or []}


def _all_entries(doc: dict) -> list[dict]:
    out = []
    for cname, c in (doc.get("crates") or {}).items():
        for m in (c.get("modules") or {}).values():
            for rs, rr in (m.get("rs") or {}).items():
                out.append(_entry(cname, c, rs, rr))
    return out


def _require_one_home(doc: dict, names: list[str], file: str | None) -> None:
    """Refuse a ``--name`` with several homes unless ``--file`` picks one.

    A name with several homes is a real placement fact (one per ``tu``, see
    :func:`crates.lookup_all`) — but they are DIFFERENT entities, not one entity
    in two places: `ossl_record_layer_st` is the TLS record layer in
    `recmethod_local.h` and a private QUIC struct in `quic_tls.c`. Scaffolding
    both because the caller typed one tag lays anchors in a module they never
    meant to touch. `--file` is the qualifier, matching either the ``tu`` or a
    header (same rule as :func:`crates.lookup_all`)."""
    from crustify import crates
    bad = []
    for n in dict.fromkeys(names):
        hits = crates.lookup_all(doc, n, file=file)
        if len(hits) > 1:
            bad.append((n, hits))
    if not bad:
        return
    lines = []
    for n, hits in bad:
        lines.append(f"  - {n}  ({len(hits)} homes)")
        for h in hits:
            # A header-only `.rs` has no `tu`; `lookup_all` matches headers too,
            # so quote one of those instead of printing an unusable placeholder.
            qual = h.get("tu") or (h.get("headers") or [None])[0]
            lines.append(f"      --file {qual or '?'}   → {h['crate']}/{h['rs']}")
    narrowed = " (already narrowed by --file)" if file else ""
    raise SystemExit(
        f"scaffold: {len(bad)} name(s) are placed in more than one module"
        f"{narrowed} — pass --file to pick one:\n" + "\n".join(lines))


def _entries_for_names(doc: dict, names: list[str],
                       file: str | None = None) -> tuple[list[dict], list[str]]:
    """The home of every name, narrowed by ``file`` when given. Ambiguity is
    already refused by :func:`_require_one_home`, so exactly one home rides per
    name; the dedup on ``(crate, rs)`` stays because two names in one call may
    share a module."""
    from crustify import crates
    entries, missing, seen = [], [], set()
    for n in names:
        hits = crates.lookup_all(doc, n, file=file)
        if not hits:
            missing.append(n)
            continue
        for hit in hits:
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
                files = [f for f in [rr.get("tu"), *(rr.get("headers") or [])] if f]
                if file is not None:
                    hit = any(f == want for f in files)
                else:
                    hit = any(f == want or f.startswith(want + "/") for f in files)
                if hit:
                    out.append(_entry(cname, c, rs, rr))
    return out


def _path_filter(layout, target, sel: str) -> str:
    import posixpath
    rel = layout.rel_target(target)
    base = "" if rel in ("", ".") else rel
    return posixpath.normpath(posixpath.join(base, sel)).lstrip("/")


# ------------------------------------------------------------------- materialize

def _materialize(layout, entries: list[dict],
                 scope_map: dict[str, str] | None = None,
                 field_map: dict[str, list[str]] | None = None) -> str:
    scope_map = scope_map or {}
    # Template generators: the one macro kind that carries an anchor.
    try:
        from compose import macro_families as _mf
        generators = set(_mf.load(layout.codeql))
    except Exception:
        generators = set()
    field_map = field_map or {}
    created = preserved = links = 0
    for e in entries:
        crate_dir = layout.repo_root / e["crate_path"]
        rs = _safe_rs(e["rs"])
        rs_path = crate_dir / rs
        if not rs_path.exists():
            rs_path.parent.mkdir(parents=True, exist_ok=True)
            rs_path.write_text(_stub(e, scope_map, field_map, generators))
            created += 1
        else:
            # Already materialized. Nothing to top up: members are anchored by
            # the SCHEDULER, in the worktree of the agent that owes them, so a
            # newly-homed member needs no scaffold rerun to become reachable.
            preserved += 1
        links += _wire(crate_dir, rs)
    return (f"{created} stub(s) created, {preserved} preserved, "
            f"{links} module link(s)")


def _ffibox(layout) -> Path:
    """Absolute path to the ffibox checkout, from ``deps.ffibox``
    in ``cli-config.json``.

    That entry is the only source — the same one
    :meth:`crustify.agents.base.CrustifyAgent._dep` resolves, so one config line
    places the crate for both the generated manifests and the agent prompts.
    Absent or unreadable is a hard error: the path is written into every
    generated ``Cargo.toml``, so guessing produces a tree that scaffolds
    cleanly and fails at the first ``cargo`` invocation, pointing at a location
    nobody chose.

    Absolute, because the path lands in a ``Cargo.toml`` under ``rust/`` and a
    relative one is depth-sensitive — it resolves differently inside a git
    worktree, which sits two levels deeper under
    ``crustify/.worktrees/<slug>/``.
    """
    p = layout.repo_config
    try:
        dep = (json.loads(p.read_text()).get("deps") or {}).get("ffibox")
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"scaffold: cannot read {p}: {exc}") from exc
    if not dep:
        raise SystemExit(
            f"scaffold: no `deps.ffibox` in {p}. Every generated "
            f"Cargo.toml needs the wrap-primitive crate's absolute path; set "
            f"it from specs/cli-config.json.")
    return Path(dep)


def _materialize_manifests(layout, doc: dict) -> str:
    """Write each in-tree wrapper crate's ``Cargo.toml`` + register it as a
    workspace member, idempotently — so the wrap/port output is a real compilable
    package (``cargo check``-able), not a manifest-less module tree the agent has
    to invent. A wrapper crate depends on its ``-sys`` FFI crate, the ``crustify``
    support crate (the ``define_ctype!`` / ``C*`` API), and the wrapper crates of
    its ``crates.json`` ``depends_on``. The ``// SAFETY:`` discipline is enforced
    by denying ``clippy::undocumented_unsafe_blocks`` workspace-wide, inherited
    via ``[lints] workspace = true`` (the ``-sys`` crates don't opt in, keeping
    their generated-code ``allow``)."""
    ffibox = _ffibox(layout)
    crates = doc.get("crates") or {}
    # A wrapper crate becomes "real" once it has a scaffolded lib.rs (members were
    # placed there); skip empty ones (e.g. a foreign lib with no wrappers). NOTE:
    # `in_tree` is provenance, never a gate: an out-of-tree dependency (libc,
    # libpthread) gets a wrapper crate like any other, holding safe Rust that
    # must compile and obey the SAFETY discipline just like the project crate.
    real = {n: c for n, c in crates.items()
            if (layout.repo_root / c["crate_path"] / "src" / "lib.rs").exists()}
    ws_toml = layout.rust / "Cargo.toml"
    written = members = 0
    for name, c in real.items():
        crate_dir = layout.repo_root / c["crate_path"]
        manifest = crate_dir / "Cargo.toml"
        if not manifest.exists():
            manifest.write_text(
                _crate_manifest(name, c, crate_dir, layout, ffibox, real))
            written += 1
        members += _add_workspace_member(
            ws_toml, os.path.relpath(crate_dir, layout.rust).replace(os.sep, "/"))
    if real:
        _ensure_workspace_lints(ws_toml)
    return f", {written} manifest(s), {members} member(s)" if (written or members) else ""


def _crate_manifest(name: str, c: dict, crate_dir: Path, layout,
                    ffibox: Path, real: dict) -> str:
    def rel(p: Path) -> str:
        return os.path.relpath(p, crate_dir).replace(os.sep, "/")
    # Absolute, per `_ffibox`: the crate lives outside `rust/`, so a
    # relative path resolves differently from a worktree than from the main
    # checkout.
    deps = [f'ffibox = {{ path = "{ffibox}" }}']
    # The crate's -sys FFI dep: explicit `sys_crate`, else the `<name>-sys`
    # convention. Every library with bound entities has one, in-tree or not
    # (e.g. libpthread → libpthread-sys).
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



_TODO = "// crustify:todo"  # matches _schedule._TODO; a surviving one = pending

#: The unfilled item anchor. ONE line naming the item, with no verb: it is laid
#: by the SCHEDULER, inside the worktree of the agent that owes it, and even
#: there the verb is unknown -- that is `--objective`, and the orchestrator
#: picks it per wave. The agent promotes this to `/// Wraps: <item>` or `/// Replaces: <item>`
#: according to what it actually did, which is the only moment the verb is
#: knowable. The item is OWNER-QUALIFIED for a field (`Type.field`): a
#: file-grained module holds many types, and two of them with a `data` field
#: would otherwise collide on one line. A C identifier cannot contain a dot, so
#: the dot also discriminates a field anchor from a symbol one.
def _todo_anchor(item: str) -> str:
    return f"{_TODO}: {item}"


#: Matches an item anchor in EITHER form — the neutral todo scaffold now emits,
#: or a `Wraps:`/`Replaces:` line at any comment depth (`//` unfilled from an
#: older scaffold, `///` once an agent has filled it). Readers must accept both:
#: a tree scaffolded before the neutral form carries thousands of the old shape,
#: and re-emitting anchors over filled work would be destructive.
#:
#: The name is terminated explicitly rather than with `\b`, for the reason
#: :func:`_has_field_anchor` documents one level down: a filled ACCESSOR anchor
#: is `<tag>.<field>`, and `\b` is satisfied by the `.`, so `Wraps: ssl_st.sess`
#: would read as an anchor for `ssl_st` and the type's own placeholder would
#: never be laid. A trailing gloss is still allowed — it is separated by space.
def _anchor_re(nm: str) -> "re.Pattern[str]":
    q = re.escape(nm)
    return re.compile(
        rf"(?m)^\s*(?://+\s*(?:Replaces|Wraps):\s*{q}(?:\s|$)"
        rf"|{re.escape(_TODO)}:\s*{q}\s*$)")

def _has_field_anchor(text: str, tag: str, fld: str) -> bool:
    """Is ``<tag>.<fld>`` already anchored in ``text``, filled or not?

    One exact match, because the anchor names its own owner
    (``prompts/principles.md``: ``// crustify:todo: <C_ITEM>.<field>``). The unqualified form
    this replaced could only be attributed by POSITION, which needed a walk that
    tracked the enclosing item anchor and had two failure modes -- a sibling
    type in the same module with the same field name, and a symbol's anchor
    sitting between a type and its accessors.

    The name is terminated explicitly rather than with ``\\b``: a field path is
    itself dotted for a flattened anonymous member, and ``\\b`` is satisfied by a
    ``.``, so ``ssl_session_st.ext`` would match ``ssl_session_st.ext.hostname``
    and a genuinely missing anchor would be skipped.
    """
    q = rf"{re.escape(tag)}\.{re.escape(fld)}"
    return re.search(
        rf"(?m)^\s*(?://+\s*(?:Field|Wraps|Replaces):\s*{q}(?:\s|$)"
        rf"|{re.escape(_TODO)}:\s*{q}\s*$)",
        text) is not None


def _stub(e: dict, scope_map: dict[str, str] | None = None,
          field_map: dict[str, list[str]] | None = None,
          generators: set[str] | None = None) -> str:
    # Each member is laid as one neutral `// crustify:todo: <item>` line. The
    # verb is not knowable here — scaffold runs before translate, and whether an
    # item is wrapped or replaced is `--objective`, chosen per wave by the
    # orchestrator. The agent promotes the line to `/// Wraps:` or
    # `/// Replaces:` according to what it did.
    # Macros are NOT anchored: bindgen owns their `ffi::` bindings /
    # `crustify_<NAME>` shims and the C `#define` stays, so the port/wrap stages
    # never fill a macro -- with one exception. A TEMPLATE GENERATOR expands to a
    # whole aggregate, and the generic its instances alias is Rust this stage
    # writes, so it gets an anchor like any other item. `generators` is the set
    # `compose.macro_families` recognises; the same carve-out exists in
    # `wrap._is_macro` and in the import closure's admission gate. A type additionally gets one accessor
    # anchor per field, laid right after the item anchor so it binds to it as
    # the owner. The wrap/port AGENT locates each by its anchor,
    # fills it, and promotes `//` -> `///` while dropping the todo. This is the
    # agent's fill contract — the scheduler schedules blindly and no longer reads
    # these markers, so the verb + todo are load-bearing for the agent, not the
    # scheduler.
    scope_map = scope_map or {}
    field_map = field_map or {}
    generators = generators or set()
    src = e.get("tu") or (
        "(no TU — placed by headers: "
        + ", ".join(e.get("headers") or ["?"]) + ")")
    lines = ["//! crustify:managed — generated module skeleton.",
             "//!",
             f"//! C source: {src}",
             "//! Each item below is a scaffolded anchor the translate stage fills.",
             ""]
    m = e["members"]
    any_member = False
    for kind in ("types", "functions", "callbacks", "globals", "macros"):
        for nm in m.get(kind) or []:
            if kind == "macros" and nm not in generators:
                continue
            lines += [_todo_anchor(nm), ""]
            if kind == "types":
                for fld in field_map.get(nm, ()):
                    lines += [_todo_anchor(f"{nm}.{fld}"), ""]
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
    Node→module resolution keys on the anchors, not file names,
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


def place_anchors(layout, target: Path, names: list[str], *,
                  fields: dict[str, list[str]] | None = None,
                  emit: bool = True) -> tuple[int, list[str]]:
    """Insert a `// crustify:todo: <item>` line for every `names` entry that has
    no anchor yet, in the `.rs` `crates.json` homes it at.

    This is the LAZY replacement for the scaffolder's up-front anchor pass. The
    scaffolder still materializes crates, empty modules and the `mod` tree —
    one-time, single-threaded work whose absence would break compilation — but
    it no longer writes an anchor per in-scope entity. On a wrap campaign over
    libcrypto that was ~25k placeholders, almost all of them for items a given
    wave never touches, and it forced an idempotent re-reconcile of a tree
    agents had already edited on every rerun.

    Called by the scheduler AFTER a batch's worktree exists and against that
    worktree's `Layout`, so an agent sees exactly the anchors for its own units
    and none of its siblings'. Two agents therefore cannot both be looking at a
    placeholder only one of them owes, and the anchor lands on the branch that
    fills it.

    `emit=False` reports without writing — the review path, which has nothing to
    do for an item nobody has wrapped yet.

    Returns `(inserted, unanchored)`: how many lines were written, and the names
    that have no anchor and did not get one.
    """
    from crustify import crates as _crates
    doc = _crates.load(layout)
    fields = fields or {}
    def _rs_path(e: dict) -> Path:
        # `crate_path` is repo-root-relative (`crustify/rust/<crate>`), not
        # relative to `layout.rust` — joining it there double-prefixes.
        cp = Path(e["crate_path"] or "")
        return (cp if cp.is_absolute() else layout.repo_root / cp) / e["rs"]

    homes: dict[Path, list[str]] = {}
    unanchored: list[str] = []
    for nm in names:
        entries, missing = _entries_for_names(doc, [nm])
        if missing:
            unanchored.append(nm)
            continue
        for e in entries:
            homes.setdefault(_rs_path(e), []).append(nm)
    inserted = 0
    for rs_path, items in homes.items():
        if not rs_path.exists():
            unanchored += items
            continue
        text = rs_path.read_text()
        add: list[str] = []
        for nm in items:
            # An ACCESSOR is tested with `_has_field_anchor`, not `_anchor_re`:
            # it is the one that also reads the pre-neutral `// Field: T.f`
            # shape, and a tree carries thousands of those, most already FILLED.
            # Testing the accessor with the item matcher lays a fresh
            # placeholder over a finished accessor — `ssl_session_st` alone had
            # 41 of them, and the agent would re-emit every one.
            wanted = [(nm, None)] + [(f"{nm}.{f}", f) for f in fields.get(nm, ())]
            for item, fld in wanted:
                anchored = (_has_field_anchor(text, nm, fld) if fld
                            else _anchor_re(item).search(text))
                if anchored or any(l == _todo_anchor(item) for l in add):
                    continue
                if not emit:
                    unanchored.append(item)
                    continue
                add += [_todo_anchor(item), ""]
        if add:
            sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            rs_path.write_text(text + sep + "\n".join(add) + "\n")
            inserted += sum(1 for l in add if l)
    return inserted, unanchored
