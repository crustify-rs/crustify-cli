"""Scope classification — the load-bearing composer every other
composer imports.

The classification rule is the definition-anchored rule documented
in `utils/codeql/README.md`:

  An entity belongs to the TARGET iff EITHER:
    1. (Primary path) It has a definition in the CodeQL database AND
       that definition's file is in `scope.json`'s `target.files`, OR
    2. (Fallback for entities with no definition in the DB) It has
       no definition in the database AND all its declarations live
       in target files.

  Otherwise the entity is an IMPORT — reached, not named.

For typedefs an additional rule applies: a typedef inherits the
scope of its terminal underlying user type, walking the `aliases`
chain emitted by `entities/types.ql`. This avoids the typedef ↔
struct scope-split that would otherwise classify e.g.
`SSL_CONNECTION` (typedef in a target header) and `ssl_connection_st`
(struct in a reached header) on opposite sides of the boundary even
though they represent the same conceptual type.

This module exports the rule as a single classifier function plus
a typedef-resolution helper. Higher-level orchestration —
loading the T1 CSVs, building the type index, producing per-section
splits — happens in the manifest composers (`types_manifest.py`,
`syms_manifest.py`, etc.) that import from here.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


# ---------------------------------------------------------------- core rule

def classify(def_file: str, decl_files: list[str], target_paths: set[str]) -> str:
    """Apply the definition-anchored rule.

    Args:
      def_file: repository-relative path of the entity's definition
        site. Empty string when no definition is in the CodeQL DB.
      decl_files: list of repository-relative paths of all
        declaration entries (typically the headers carrying
        `extern` / forward declarations). May be empty.
      target_paths: the target's file set (`scope.json`'s `target.files`).

    Returns :data:`TARGET` or :data:`IMPORT`.
    """
    if def_file:
        return TARGET if def_file in target_paths else IMPORT
    # Fallback: no def in DB, classify by declarations.
    if not decl_files:
        # No def AND no decls — treat as an import (unknown origin, safer
        # to surface as a boundary entity than to silently claim it).
        return IMPORT
    return TARGET if all(d in target_paths for d in decl_files) else IMPORT


_C_TU_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".cu")


def entry_scope(
    kind: str,
    def_file: str,
    decl_files: list[str],
    target_paths: set[str],
) -> str:
    """Per-entry section classification (the rule consumers apply).

    Identical to `classify` for ordinary entities, with one carve-out
    for macros: a `#define` cannot be re-exported Rust->C, so a macro
    whose home is a HEADER is *always* an import (kept as a C define and
    read through bindgen) even when that header is the target's own —
    anything still in C may `#include` it. Only a TU-local macro defined
    in a `.c` that is itself in the target belongs to the target: it has
    no external consumer, and it is inlined when that translation unit
    is translated.

    This is the single source of truth shared by the composer's macro
    handling, bindgen's surface filter, and the deps-DAG section query.
    """
    if (kind or "").startswith("macro"):
        home = def_file or (decl_files[0] if decl_files else "")
        if home.endswith(_C_TU_SUFFIXES) and home in target_paths:
            return TARGET
        return IMPORT
    return classify(def_file, decl_files, target_paths)


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


def classify_type(row: dict, by_name: dict[str, dict], target_paths: set[str]) -> str:
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
        return classify(row["def_file"], parse_decl_files(row["decl_files"]), target_paths)
    if row["aliases"] and row["aliases"] in by_name:
        base = resolve_typedef(row["aliases"], by_name)
        if base is not None:
            return classify(base["def_file"], parse_decl_files(base["decl_files"]), target_paths)
    return classify(row["def_file"], parse_decl_files(row["decl_files"]), target_paths)


# ---------------------------------------------------------------- I/O helpers

def _doc(scope_src) -> dict:
    """Accept the scope manifest either as a `Path` to a `scope.json` or as the
    composed dict itself.

    The dict form is the live one: `crustify.scope.build` composes the manifest
    in memory, so nothing has to have written it out first — and a `scope.json`
    on disk can no longer be silently stale against the `scope-config.json` a
    human just edited. The `Path` form stays for the standalone composer CLIs
    and the `--dump` snapshot."""
    if isinstance(scope_src, dict):
        return scope_src
    return json.loads(Path(scope_src).read_text())


#: The two sections of a composed `scope.json`, as constants rather than bare
#: strings. Both words are already spoken for elsewhere — `target` is the CLI
#: positional and `build.json`'s output filename, `import` shades into
#: `#include` — so a literal is ambiguous at a glance where `scope.TARGET` is
#: not. The pair they replace was worse: `"port"` and `"wrap"` are ALSO
#: objective values, audit stage labels and scheduler pool keys, so a sweep
#: over those literals could not tell a section key from a verb.
TARGET = "target"
IMPORT = "import"
SECTIONS = (TARGET, IMPORT)


def load_target_paths(scope_json) -> set[str]:
    """The target's own file set — every file `scope-config.json`'s `files`
    named that the build actually compiled.

    Composer-emitted by `compose.scope_manifest.compose()`; the `files` list is
    anchored on the CodeQL T1 tables, so an `#ifdef`-elided file is absent:

        {"target": {"files": [...], "functions": [...], ...}}

    Every other file an in-scope entity reaches is an IMPORT and lands in the
    sibling section. Note this says nothing about what will be DONE with those
    files — port or wrap is the translate stage's objective, not a property of
    the scope.
    """
    return set(_doc(scope_json).get(TARGET, {}).get("files", []))


def load_seed_paths(scope_json) -> set[str]:
    """The files `scope-config.json`'s `files.import` named, expanded — echoed
    into the manifest as `import.seeds`.

    Empty for a target campaign, where the import section is DERIVED from the
    target. Non-empty for a wrap campaign, where those files ARE the section's
    seed: it is what tells a consumer that a struct defined in one of them is a
    public value type (full field layout) rather than an opaque handle."""
    return set(_doc(scope_json).get(IMPORT, {}).get("seeds", []))


def load_entities(scope_json, section: str, kind: str) -> set[tuple[str, str]]:
    """The entity set of `section` (:data:`TARGET` | :data:`IMPORT`) for `kind`
    (``"functions"`` | ``"globals"`` | ``"macros"`` | ``"types"``), as
    ``(name, defined_in)`` keys.

    Membership-lookup replacement for re-running `classify` / `entry_scope` /
    `classify_type` over the T1 CSVs: the scope composer classified every
    entity once — including the header-macro carve-out — so a downstream
    composer tests membership here instead of deriving scope a second time and
    risking a different answer. The tag is read through :func:`entry_tag`, the
    same accessor :func:`scope_membership` uses, so the two agree.
    """
    return {(entry_tag(e), e.get("defined_in") or "")
            for e in _doc(scope_json).get(section, {}).get(kind, [])}


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
    scope_json, section: str, *,
    kinds: tuple[str, ...] = _SCOPE_KINDS,
) -> set[tuple[str, str]]:
    """Unified membership key set for ``section`` (:data:`TARGET` |
    :data:`IMPORT`):
    ``{(name, origin)}`` via :func:`origin_key`, so a candidate is in scope iff
    its own ``origin_key`` is in the set. Needs both ``defined_in`` and
    ``declared_in`` on every scope entry (the latter is the def_file-empty
    complement). Handles both section schemas — the tag is ``type`` (types) /
    ``name`` (syms).

    ``kinds`` restricts the buckets unioned (a type query passes ``("types",)``;
    a symbol query the three sym buckets). Every entry is a real C entity: the
    scope sections are anchored on the CodeQL T1 tables, so there is nothing
    synthesized to filter out."""
    sec = _doc(scope_json).get(section, {})
    keys: set[tuple[str, str]] = set()
    for kind in kinds:
        for e in sec.get(kind, []):
            nm = entry_tag(e)
            keys.add(origin_key(nm, e.get("defined_in"), e.get("declared_in")))
    return keys


def entry_tag(e: dict):
    """The identifier of a types.json / scope.json entry: ``name`` (current
    schema), with a ``type`` fallback for un-migrated records. The record-level
    identifier was renamed ``type`` -> ``name`` to match the ``syms.json`` base;
    the field-level ``type`` (a ``fields[]`` element's C type) is unrelated and
    is NOT read through here."""
    return e.get("name") or e.get("type")


def clone_op_names(cloned_by) -> list[str]:
    """Flatten a `cloned_by` block to fn names.

    ``cloned_by = {deep: [...], upref: [...]}`` — `deep` duplicators produce a
    fresh allocation, `upref`s bump the refcount; a fn that branches between the
    two appears in both, so the modes are unioned and deduped. This is the
    single clone-extraction primitive the dag / scope / consistency / schedule
    stages share; callers pass ``type_cloned_by(entry, lifecycle)``."""
    if not isinstance(cloned_by, dict):
        return []
    out: list[str] = []
    for role in ("deep", "upref"):
        v = cloned_by.get(role)
        if v:
            out += (v if isinstance(v, list) else [v])
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _empty_lifecycle() -> dict:
    return {"dropped_by": [], "cloned_by": {"deep": [], "upref": []},
            "fields_disposed_by": []}


def _entry_pair(src) -> tuple[list, list]:
    """Accept either the composed ``(type_entries, sym_entries)`` pair or a
    legacy analysis-root path, and return the pair.

    The pair is the live form: records are composed and store-overlaid by
    :mod:`crustify.manifests`, with no per-stem tree to walk. The path form
    stays for the standalone composer CLIs, which run outside the orchestrator
    and still take a directory."""
    if isinstance(src, tuple):
        return src
    root = Path(src)
    types, syms = [], []
    for tj in sorted(root.rglob("types.json")):
        try:
            types += json.loads(tj.read_text()).get("types") or []
        except (OSError, ValueError):
            pass
    for sj in sorted(root.rglob("syms.json")):
        try:
            syms += json.loads(sj.read_text()).get("symbols") or []
        except (OSError, ValueError):
            pass
    return types, syms


def build_lifecycle_index(analysis_root) -> dict[str, dict]:
    """Reverse-derive every type's lifecycle roles from the SYMS tree.

    A concrete type stores no lifecycle of its own. The fact lives once, on the
    acting symbol: ``syms.json``'s entry-level ``lifetime``
    ``{for, is_dropper, is_disposer, is_cloner}`` (see docs/schemas/syms.md).
    This inverts that relation — the composer-side equivalent of
    ``query symbols --lifetime-for`` — so the dag / consistency / schedule
    stages read one derived index instead of a field that could drift from the
    symbol that actually implements the role.

    Returns ``tag -> {dropped_by: [fn], cloned_by: {deep: [fn], upref: [fn]},
    fields_disposed_by: [fn]}``, keyed by the CANONICAL type tag: a symbol's arg
    records its pointee type as written (``SSL``), which is resolved through the
    types tree's ``typedef`` aliases to the struct tag (``ssl_st``) so both
    spellings land on one entry. Types with no role are absent, not empty.
    """
    type_entries, sym_entries = _entry_pair(analysis_root)

    # alias -> canonical tag. `name` is the canonical spelling; every `typedef`
    # alias resolves to it. setdefault, not assignment: a canonical name always
    # wins over an alias of the same string.
    canon: dict[str, str] = {}
    for t in type_entries:
        tag = entry_tag(t)
        if not tag:
            continue
        canon[tag] = tag
        td = t.get("typedef")
        for alias in (td if isinstance(td, list) else [td] if td else []):
            if alias:
                canon.setdefault(alias, tag)

    out: dict[str, dict] = {}
    for s in sym_entries:
        lf = s.get("lifetime")
        if not isinstance(lf, dict):
            continue
        arg = next((a for a in s.get("ptr_args") or []
                    if a.get("name") == lf.get("for")), None)
        fn = s.get("name")
        if arg is None or not fn:
            continue
        tag = canon.get(arg.get("type")) or arg.get("type")
        if not tag:
            continue
        rec = out.setdefault(tag, _empty_lifecycle())
        if lf.get("is_dropper") is True:
            rec["dropped_by"].append(fn)
        if lf.get("is_disposer") is True:
            rec["fields_disposed_by"].append(fn)
        cl = lf.get("is_cloner")
        if isinstance(cl, dict):
            for mode in ("deep", "upref"):
                if cl.get(mode):
                    rec["cloned_by"][mode].append(fn)

    def _dedup(xs):
        seen: set[str] = set()
        return sorted(x for x in xs if not (x in seen or seen.add(x)))

    for rec in out.values():
        rec["dropped_by"] = _dedup(rec["dropped_by"])
        rec["fields_disposed_by"] = _dedup(rec["fields_disposed_by"])
        for mode in ("deep", "upref"):
            rec["cloned_by"][mode] = _dedup(rec["cloned_by"][mode])
    return out


def _lifecycle_of(entry: dict, lifecycle) -> dict:
    """This entry's roles from a :func:`build_lifecycle_index` result.

    ``lifecycle`` is optional only so a caller holding no index still gets a
    well-formed empty record rather than a crash; omitting it yields NO roles,
    so every caller that walks real type records must pass the index it built.
    """
    if not isinstance(lifecycle, dict):
        return _empty_lifecycle()
    return lifecycle.get(entry_tag(entry)) or _empty_lifecycle()


def type_dropped_by(entry: dict, lifecycle=None) -> list:
    """The type's destructors: a flat LIST, reverse-derived from the symbols
    whose ``lifetime.is_dropper`` acts on an arg of this type."""
    return _lifecycle_of(entry, lifecycle)["dropped_by"]


def type_cloned_by(entry: dict, lifecycle=None) -> dict:
    """The type's ``{deep, upref}`` clone block, reverse-derived from the
    symbols whose ``lifetime.is_cloner`` acts on an arg of this type."""
    return _lifecycle_of(entry, lifecycle)["cloned_by"]


def type_fields_disposed_by(entry: dict, lifecycle=None) -> list:
    """The type's field-teardown routines (``*_cleanup`` / ``*_dispose``),
    reverse-derived from the symbols whose ``lifetime.is_disposer`` acts on an
    arg of this type."""
    return _lifecycle_of(entry, lifecycle)["fields_disposed_by"]


def type_method_syms(entry: dict, lifecycle=None) -> list[str]:
    """The C function names that are this type's methods — its method surface,
    deduped, lifecycle-first.

    Wholly DERIVED, never stored: the type's lifecycle (``dropped_by`` ∪
    ``cloned_by.{deep,upref}`` ∪ ``fields_disposed_by``), reverse-derived from
    the acting symbols via ``lifecycle``. Field accessors are NOT part of the
    surface -- the wrapper derives per-field accessors from the field layout
    directly, so a C field-accessor function is an ordinary free function here.
    """
    lc = _lifecycle_of(entry, lifecycle)
    out: list[str] = list(lc["dropped_by"])
    out += clone_op_names(lc["cloned_by"])
    out += lc["fields_disposed_by"]
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def type_method_fns(analysis_root: Path, lifecycle=None) -> set[str]:
    """Union of every type's method surface (lifecycle ops) across the analysis
    tree. These are **folded** into their owning type's wrap unit (emitted by
    `wrap types` at the *type's* layer), so they must never be selected as
    standalone sym units — the wrap/port layer-slice selection subtracts this
    set, and the port stage uses it as its lifecycle filter.

    Builds the lifecycle index itself when not handed one, since it already
    walks the same tree."""
    root = Path(analysis_root)
    if lifecycle is None:
        lifecycle = build_lifecycle_index(root)
    fns: set[str] = set()
    for tj in root.rglob("types.json"):
        try:
            doc = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        recs = doc if isinstance(doc, list) else (doc.get("types") or [])
        for rec in recs:
            if isinstance(rec, dict):
                fns.update(type_method_syms(rec, lifecycle))
    return fns


def in_scope_pred(scope_json, section: str, **kw):
    """A predicate ``pred(node) -> bool`` over a dag Node, backed by
    :func:`scope_membership`. ``Node.defined_in`` is already the node's
    ``origin()`` (defined_in or canonical decl), so it keys directly. This is
    the single section-classification primitive the node-path stages share."""
    keys = scope_membership(scope_json, section, **kw)

    def pred(n) -> bool:
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
    tgt = load_target_paths(scope_json)
    print(f"target paths: {len(tgt)}")

    fns = load_csv(csv_dir / "functions.csv")
    pf = sum(1 for r in fns if classify(r["def_file"], parse_decl_files(r["decl_files"]), tgt) == TARGET)
    print(f"functions: total={len(fns)}  target={pf}  import={len(fns) - pf}")

    macs = load_csv(csv_dir / "macros.csv")
    pm = sum(1 for r in macs if r["def_file"] in tgt)
    print(f"macros:    total={len(macs)}  target={pm}  import={len(macs) - pm}")

    gls = load_csv(csv_dir / "globals.csv")
    pg = sum(1 for r in gls if classify(r["def_file"], parse_decl_files(r["decl_files"]), tgt) == TARGET)
    print(f"globals:   total={len(gls)}  target={pg}  import={len(gls) - pg}")

    types = load_csv(csv_dir / "types.csv")
    by_name = build_types_index(types)
    pt = sum(1 for r in types if classify_type(r, by_name, tgt) == TARGET)
    print(f"types:     total={len(types)}  target={pt}  import={len(types) - pt}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Self-test the scope classifier.")
    ap.add_argument("--csv-dir", type=Path, required=True,
                    help="Directory containing T1 CSVs (functions.csv, macros.csv, globals.csv, types.csv).")
    ap.add_argument("--scope-json", type=Path, required=True,
                    help="Path to the target's scope.json (its `target.files`).")
    args = ap.parse_args()
    _selftest(args.csv_dir, args.scope_json)
