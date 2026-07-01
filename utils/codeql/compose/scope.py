"""Scope classification — the load-bearing composer every other
composer imports.

The classification rule is the definition-anchored rule documented
in `utils/codeql/README.md`:

  An entity is port-scope iff EITHER:
    1. (Primary path) It has a definition in the CodeQL database AND
       that definition's file is in `scope.json`'s `port.files`, OR
    2. (Fallback for entities with no definition in the DB) It has
       no definition in the database AND all its declarations live
       in port-scope files.

  Otherwise the entity is wrap-scope.

For typedefs an additional rule applies: a typedef inherits the
scope of its terminal underlying user type, walking the `aliases`
chain emitted by `entities/types.ql`. This avoids the typedef ↔
struct scope-split that would otherwise classify e.g.
`SSL_CONNECTION` (typedef in port header) and `ssl_connection_st`
(struct in wrap header) on opposite sides of the boundary even
though they represent the same conceptual type.

This module exports the rule as a single classifier function plus
a typedef-resolution helper. Higher-level orchestration —
loading the T1 CSVs, building the type index, producing per-scope
splits — happens in the manifest composers (`types_manifest.py`,
`syms_manifest.py`, etc.) that import from here.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


# ---------------------------------------------------------------- core rule

def classify(def_file: str, decl_files: list[str], port_paths: set[str]) -> str:
    """Apply the definition-anchored rule.

    Args:
      def_file: repository-relative path of the entity's definition
        site. Empty string when no definition is in the CodeQL DB.
      decl_files: list of repository-relative paths of all
        declaration entries (typically the headers carrying
        `extern` / forward declarations). May be empty.
      port_paths: the port-scope path set (from `scope.json`'s `port.files`).

    Returns "port" or "wrap".
    """
    if def_file:
        return "port" if def_file in port_paths else "wrap"
    # Fallback: no def in DB, classify by declarations.
    if not decl_files:
        # No def AND no decls — treat as wrap (unknown origin, safer
        # to surface as boundary entity than to silently include in
        # port).
        return "wrap"
    return "port" if all(d in port_paths for d in decl_files) else "wrap"


_C_TU_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".cu")


def entry_scope(
    kind: str,
    def_file: str,
    decl_files: list[str],
    port_paths: set[str],
) -> str:
    """Per-entry port/wrap classification (the rule consumers apply).

    Identical to `classify` for ordinary entities, with one carve-out
    for macros: a `#define` cannot be re-exported Rust->C, so a macro
    whose home is a HEADER is *always* wrap (kept as a C define and
    read through bindgen), regardless of the per-dir scope tag that
    `analyze` was routed with. Only a TU-local macro defined in a `.c`
    that is itself in port scope is a port entry (it gets inlined when
    that translation unit is ported).

    This is the single source of truth shared by the composer's macro
    `is_port`, bindgen's surface filter, and the deps-DAG scope query,
    so the per-dir analysis tag never has to encode it.
    """
    if (kind or "").startswith("macro"):
        home = def_file or (decl_files[0] if decl_files else "")
        if home.endswith(_C_TU_SUFFIXES) and home in port_paths:
            return "port"
        return "wrap"
    return classify(def_file, decl_files, port_paths)


def parse_decl_files(decl_files_pipe: str) -> list[str]:
    """Parse the pipe-separated `decl_files` column emitted by T1
    queries. Returns the list of non-empty path strings.
    """
    return [d for d in decl_files_pipe.split("|") if d]


def canonical_decl(decls: list[str]) -> str | None:
    """Pick the canonical declaration file from a list, by priority rather
    than position.

    The T1 `decl_files` list is alphabetical (`order by getRelativePath()`),
    so a bare `decls[0]` is systematically biased toward `.c` over `.h`
    (`c` < `h`) and `build/` generated artifacts over `src/` — the wrong
    file for a "declared_in" / include-site purpose. Prefer instead:
    in-repo header > in-repo source > external (absolute) header, with
    generated `build/` artifacts deprioritized. Falls back to alphabetical
    only as the final tiebreak.
    """
    if not decls:
        return None

    def rank(d: str) -> tuple:
        external = d.startswith("/")            # absolute → out-of-repo
        header = d.endswith((".h", ".hpp", ".hh"))
        generated = d.startswith("build/")
        return (external, not header, generated, d)

    return min(decls, key=rank)


# ---------------------------------------------------------------- type index + resolution

def build_types_index(rows: list[dict]) -> dict[str, dict]:
    """Build a name → row index for typedef alias resolution.

    Anonymous-tag rows (`(unnamed enum)`,
    `(unnamed class/struct/union)`) are EXCLUDED — they share the
    same synthetic name across hundreds of distinct rows, so a single
    name → row dict would arbitrarily pick one and propagate its
    scope to every same-name lookup (the bug we hit during T1
    validation). Typedef-alias resolution only ever chases named
    aliases (anonymous bases are surfaced as `aliases=""` by
    `entities/types.ql`), so excluding anonymous rows from the index
    is sound.

    When two rows share the same name (e.g. forward-decl-only + full
    definition somewhere), prefer the row with a non-empty
    `def_file` so chain resolution lands on the definition site
    rather than a forward declaration.
    """
    by_name: dict[str, dict] = {}
    for r in rows:
        name = r["name"]
        if not name or name.startswith("("):
            continue
        cur = by_name.get(name)
        if cur is None or (not cur["def_file"] and r["def_file"]):
            by_name[name] = r
    return by_name


def resolve_typedef(
    start_name: str,
    by_name: dict[str, dict],
    seen: set[str] | None = None,
) -> dict | None:
    """Walk a typedef alias chain starting from `start_name`.

    Returns the row whose def_file/decl_files should drive scope
    classification — either the first non-typedef row reached
    (struct / union / enum), or the last typedef row whose `aliases`
    column is empty (anonymous or primitive base) or unresolvable
    (alias name not in the index).

    Returns None only when `start_name` itself isn't in the index.

    Cycle-guarded with a `seen` set — C doesn't allow recursive
    typedefs but the guard is cheap insurance.
    """
    if seen is None:
        seen = set()
    if start_name in seen:
        return by_name.get(start_name)
    seen.add(start_name)
    row = by_name.get(start_name)
    if row is None:
        return None
    if row["kind"] != "typedef":
        return row
    alias = row["aliases"]
    if not alias or alias not in by_name:
        return row
    return resolve_typedef(alias, by_name, seen)


def classify_type(row: dict, by_name: dict[str, dict], port_paths: set[str]) -> str:
    """Classify a type row.

    Non-typedef rows classify by their own def_file / decl_files
    directly (no `by_name` lookup, so anonymous-tag duplicates are
    scoped correctly per row).

    Typedef rows walk the alias chain and classify by the terminal
    row's def_file / decl_files. Typedefs with empty / unresolvable
    `aliases` fall back to their own def_file / decl_files (the
    typedef IS the identity carrier in those cases, e.g.
    `typedef enum { … } STATE;`).
    """
    if row["kind"] != "typedef":
        return classify(row["def_file"], parse_decl_files(row["decl_files"]), port_paths)
    if row["aliases"] and row["aliases"] in by_name:
        base = resolve_typedef(row["aliases"], by_name)
        if base is not None:
            return classify(base["def_file"], parse_decl_files(base["decl_files"]), port_paths)
    return classify(row["def_file"], parse_decl_files(row["decl_files"]), port_paths)


# ---------------------------------------------------------------- I/O helpers

def load_port_paths(scope_json: Path) -> set[str]:
    """Read `<target>/.crustify/scope.json` and return its port-scope
    file-path set.

    The scope manifest is composer-emitted by
    `compose.scope_manifest.compose()` and is the canonical port-scope
    file list for a given target invocation. v2 schema (`port` is an
    object whose `files` list is anchored on the CodeQL T1 tables, so
    `#ifdef`-elided files are absent):

        {"port": {"files": [...], "functions": [...], ...}}

    Every other file in the analysis tree is implicitly wrap-scope.
    """
    data = json.loads(scope_json.read_text())
    port = data.get("port", {})
    return set(port.get("files", []))


def load_port_entities(scope_json: Path, kind: str) -> set[tuple[str, str]]:
    """Return the v2 port-scope entity set for `kind`
    (``"functions"`` | ``"globals"`` | ``"macros"`` | ``"types"``) as a
    set of ``(name, defined_in)`` keys.

    Membership-lookup replacement for re-running `classify`/
    `entry_scope`/`classify_type` over the T1 CSVs: the scope composer
    already classified every entity once (including the header-macro
    carve-out) and wrote the port subset here, so downstream composers
    test ``(name, defined_in) in load_port_entities(...)``.
    """
    data = json.loads(scope_json.read_text())
    port = data.get("port", {})
    return {(e["name"], e.get("defined_in") or "") for e in port.get(kind, [])}


def load_wrap_entities(scope_json: Path, kind: str) -> set[tuple[str, str]]:
    """Wrap-scope entity set for `kind` as ``(name, def_file)`` keys — the
    closure :func:`compose_wrap` derived. Mirror of :func:`load_port_entities`
    for the ``wrap`` section, whose entries key the tag on ``type`` (types) /
    ``name`` (syms) and the file on ``defined_in``."""
    data = json.loads(scope_json.read_text())
    wrap = data.get("wrap", {})
    tagk = "type" if kind == "types" else "name"
    return {(e.get(tagk), e.get("defined_in") or "") for e in wrap.get(kind, [])}


_SCOPE_KINDS = ("functions", "globals", "macros", "types")


def origin_key(name: str, defined_in: str | None, declared_in) -> tuple[str, str]:
    """The uniform scope key for an entity: ``(name, defined_in or
    canonical_decl(declared_in))``. ``defined_in`` takes precedence; a null-def
    entity (callback / extern / phantom) falls back to its canonical declaring
    header. This is EXACTLY what the dag serializes per node (``Node.origin()``)
    and what a manifest row computes, so scope entries, dag nodes, and manifest
    rows all collide on the same key — and a real-def entity never aliases its
    null-def twin (the twin's key is a header, the real one's is the .c)."""
    if isinstance(declared_in, str):
        declared_in = [declared_in]
    return (name, defined_in or (canonical_decl(declared_in or []) or ""))


def scope_membership(
    scope_json: Path, which: str, *,
    kinds: tuple[str, ...] = _SCOPE_KINDS, synthetic: bool | None = None,
) -> set[tuple[str, str]]:
    """Unified scope-membership key set for ``which`` (``"port"`` | ``"wrap"``):
    ``{(name, origin)}`` via :func:`origin_key`, so a candidate is in scope iff
    its own ``origin_key`` is in the set. Needs both ``defined_in`` and
    ``declared_in`` on every scope entry (the latter is the def_file-empty
    complement). Handles both section schemas — the tag is ``type`` (types) /
    ``name`` (syms).

    ``kinds`` restricts the buckets unioned (a type query passes ``("types",)``;
    a symbol query the three sym buckets). ``synthetic``: ``None`` = all,
    ``True`` = only augment-added synthetics, ``False`` = exclude them (bindgen
    binds real C entities only, never synthetics)."""
    sec = json.loads(scope_json.read_text()).get(which, {})
    keys: set[tuple[str, str]] = set()
    for kind in kinds:
        for e in sec.get(kind, []):
            if synthetic is not None and bool(e.get("synthetic")) != synthetic:
                continue
            nm = e.get("type") or e.get("name")
            keys.add(origin_key(nm, e.get("defined_in"), e.get("declared_in")))
    return keys


SYNTHETIC_KINDS = ("string", "array")  # buffer-pass clusters


# Destructor role keys across schema versions. The dual-ownership split is
# `shared` (refcount-decrementing free, pairs with up_ref -> CArc) and
# `exclusive` (sole-owner plain free -> CBox); `fields` is the by-value POD
# disposer (*_dispose / *_cleanup -> CVal). `storage` is the pre-split legacy
# single free, tolerated until the on-disk types.json schemas are migrated.
_DTOR_ROLE_KEYS = ("shared", "exclusive", "fields", "storage")


def dtor_op_names(d) -> list[str]:
    """Destructor op function names from a `dtor` value, schema-tolerant.

    New schema: ``{shared, exclusive, fields}``. Legacy: ``{storage, fields}``
    (single pre-split free) or a bare string. Returns the non-null names in
    role order, deduped — so a type's `*_free` / `*_dispose` folds into its
    lifecycle regardless of which schema version produced the record. This is
    the single dtor-extraction primitive the dag / scope / consistency stages
    share, so the dual-dtor split lands uniformly.
    """
    if isinstance(d, dict):
        seen: set[str] = set()
        out: list[str] = []
        for k in _DTOR_ROLE_KEYS:
            v = d.get(k)
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out
    return [d] if d else []


def lifetime(rec: dict) -> dict:
    """The lifecycle sub-record — ``ctors``/``up_ref``/``dtor``/``clones``/
    ``locking``/``conditional_drop``. The current schema nests these
    under ``rec["lifetime"]``; this returns that **mutable** dict, falling back
    to ``rec`` itself for un-migrated flat records — so both the reads here and
    the partial-merge write target in ``query`` stay correct across migration.
    `kind`, `ops`, `fields`, `casted` stay at the record top level."""
    lc = rec.get("lifetime")
    return lc if isinstance(lc, dict) else rec


def alloc_fns(lc: dict) -> list[str]:
    """The byte-level allocator routines (`allocs`) — the raw-T-producing
    primitives bound to the type. Back-compat: falls back to the pre-refactor
    `ctors` key for un-migrated records (higher-level construction logic is now
    a free function; only the allocation primitive is bound)."""
    v = lc.get("allocs")
    return list(v if v is not None else (lc.get("ctors") or []))


def type_method_syms(entry: dict) -> list[str]:
    """The C function names that are this type's methods — its method surface,
    deduped, lifecycle-first.

    For a concrete type (struct/union/enum) this is DERIVED, not stored: its
    **lifecycle** (ctors ∪ dtor.{shared,exclusive,fields} ∪ up_ref ∪ clones ∪
    locking.{acquire,release}). In-place initializers (`*_init`, stack/embedded
    ctors that don't byte-allocate) live in `ctors`. Field accessors are NOT
    part of the surface —
    the wrapper derives per-field accessors from the field layout directly, so a
    C field-accessor function is an ordinary free function here. There is no
    `ops` list on a concrete type — the deps DAG and the consistency gate call
    this to recover the lifecycle set.

    For the fieldless synthetic clusters (kind ``string``/``array``) the method
    surface is their explicit ``ops`` list (realloc/cleanse have no field
    analog), returned verbatim.
    """
    if entry.get("kind") in SYNTHETIC_KINDS:
        return list(entry.get("ops") or [])
    lc = lifetime(entry)
    out: list[str] = alloc_fns(lc)
    out += dtor_op_names(lc.get("dtor"))
    if lc.get("up_ref"):
        out.append(lc["up_ref"])
    out += lc.get("clones") or []
    lock = lc.get("locking") or {}
    out += [v for v in (lock.get("acquire"), lock.get("release")) if v]
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def type_method_fns(analysis_root: Path) -> set[str]:
    """Union of every type's method surface (lifecycle ops) across the analysis
    tree. These are **folded** into their owning type's wrap unit (emitted by
    `wrap types` at the *type's* layer), so they must never be selected as
    standalone sym units — the wrap/port layer-slice selection subtracts this
    set, and the port stage uses it as its lifecycle filter."""
    fns: set[str] = set()
    for tj in Path(analysis_root).rglob("types.json"):
        try:
            doc = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        recs = doc if isinstance(doc, list) else (doc.get("types") or [])
        for rec in recs:
            if isinstance(rec, dict):
                fns.update(type_method_syms(rec))
    return fns


def in_scope_pred(scope_json: Path, which: str, **kw):
    """A predicate ``pred(node) -> bool`` over a dag Node, backed by
    :func:`scope_membership`. ``Node.defined_in`` is already the node's
    ``origin()`` (defined_in or canonical decl), so it keys directly.

    Synthetic-kind nodes (``string`` / ``array`` buffer clusters) are
    **always wrap, never port** — they live outside scope.json, so this rule
    is applied here so every node-path stage (wrap/port) classifies them
    uniformly. This is the single scope-classification primitive those stages
    share."""
    keys = scope_membership(scope_json, which, **kw)

    def pred(n) -> bool:
        if (getattr(n, "subkind", "") or "") in SYNTHETIC_KINDS:
            return which == "wrap"
        return (n.id, n.defined_in or "") in keys

    return pred


def load_csv(path: Path) -> list[dict]:
    """Load a T1 / T2 CSV emitted by `codeql bqrs decode --format=csv`."""
    with path.open() as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- self-test / CLI

def _selftest(csv_dir: Path, scope_json: Path) -> None:
    """Replay the validation report from `/tmp/t1/classify.py`. Useful
    for sanity-checking against a known database while developing
    downstream composers.
    """
    port = load_port_paths(scope_json)
    print(f"port paths: {len(port)}")

    fns = load_csv(csv_dir / "functions.csv")
    pf = sum(1 for r in fns if classify(r["def_file"], parse_decl_files(r["decl_files"]), port) == "port")
    print(f"functions: total={len(fns)}  port={pf}  wrap={len(fns) - pf}")

    macs = load_csv(csv_dir / "macros.csv")
    pm = sum(1 for r in macs if r["def_file"] in port)
    print(f"macros:    total={len(macs)}  port={pm}  wrap={len(macs) - pm}")

    gls = load_csv(csv_dir / "globals.csv")
    pg = sum(1 for r in gls if classify(r["def_file"], parse_decl_files(r["decl_files"]), port) == "port")
    print(f"globals:   total={len(gls)}  port={pg}  wrap={len(gls) - pg}")

    types = load_csv(csv_dir / "types.csv")
    by_name = build_types_index(types)
    pt = sum(1 for r in types if classify_type(r, by_name, port) == "port")
    print(f"types:     total={len(types)}  port={pt}  wrap={len(types) - pt}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Self-test the scope classifier.")
    ap.add_argument("--csv-dir", type=Path, required=True,
                    help="Directory containing T1 CSVs (functions.csv, macros.csv, globals.csv, types.csv).")
    ap.add_argument("--scope-json", type=Path, required=True,
                    help="Path to the target's scope.json (its `port.files`).")
    args = ap.parse_args()
    _selftest(args.csv_dir, args.scope_json)
