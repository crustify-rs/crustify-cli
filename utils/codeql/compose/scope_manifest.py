"""Derive `<target>/scope.json`'s `target` section from `scope-config.json`.

Pure filesystem walk + path predicate — no CodeQL, no agent. The
target-tier scope manifest is the canonical file list for a given target
invocation.

Rule:

  1. A file is a *candidate* if `config.files` names it — directly, or
     through a trailing-slash directory entry that expands to every
     C/C++ source or header (`.c`, `.cc`, `.cpp`, `.h`, `.hpp`) beneath
     it.
  2. A candidate survives iff its repo-root-relative path does not match
     any entry in `config.out_of_scope.paths` (entries ending with `/`
     match recursively; otherwise exact-file match).
  3. Everything an in-scope entity REACHES but that `config.files` does
     not name is an import, derived by `import_closure.py` into the sibling
     `import` section.

The section says what the target COVERS, never what will be done with it:
port or wrap is the translate stage's objective, chosen per unit by the
orchestrator, not a property recorded here.

`scope.json` schema:

    {
      "_comment": "...",
      "target": {"files": [...], "functions": [...], ...},
      "import": {"files": [...], "functions": [...], ...}
    }
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:  # package import (compose.scope_manifest) — the normal path
    from . import scope as _scope
except ImportError:  # script invocation fallback (python scope_manifest.py)
    import scope as _scope  # type: ignore

_SOURCE_EXTS = {".c", ".cc", ".cpp"}
_HEADER_EXTS = {".h", ".hpp"}
_ALL_EXTS = _SOURCE_EXTS | _HEADER_EXTS

_COMMENT = (
    "The TARGET section: every entity whose home file `scope-config.json`'s "
    "`files` names. The candidate file universe is that list expanded "
    "(trailing-slash entries walked) minus `config.out_of_scope.paths`; the "
    "ENTITY sets (functions/globals/macros/types) and the derived `files` list "
    "are then anchored on the CodeQL T1 tables, so they reflect the "
    "#ifdef-resolved (post-preprocessor) view of what the build actually "
    "compiled — a file whose body is elided by a disabled feature contributes "
    "no entities and therefore does not appear in `files`. Entities are keyed "
    "by (name, defined_in) because bare names collide (file-local statics). "
    "Classification (`classify`/`entry_scope`/`classify_type`, including the "
    "header-macro carve-out) runs ONCE here; downstream composers read these "
    "sets via `scope.load_entities` instead of re-deriving scope from the "
    "CSVs. Build-config-dependent by construction. This section says what the "
    "target COVERS, never what will be done with it — port or wrap is the "
    "translate stage's objective, chosen per unit by the orchestrator. "
    "Everything an entity here reaches that `files` does not name is an "
    "IMPORT, in the sibling section. Paths are repo-root-relative."
)


def _is_out_of_scope(rel_path: str, out_of_scope: list[str]) -> bool:
    for pattern in out_of_scope:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern):
                return True
        elif rel_path == pattern:
            return True
    return False


def _walk_dir(rel_dir: str, repo_root: Path, out_of_scope: list[str]) -> list[str]:
    """Every source/header file under ``rel_dir``, repo-root-relative."""
    base = repo_root if rel_dir in (".", "", "/") else (repo_root / rel_dir)
    base = base.resolve()

    files: list[str] = []
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix not in _ALL_EXTS:
            continue
        try:
            rel = str(p.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            continue  # symlinks pointing outside repo_root; skip
        if _is_out_of_scope(rel, out_of_scope):
            continue
        files.append(rel)
    return files


def enumerate_files(config: dict, repo_root: Path) -> list[str]:
    """Return the repo-root-relative candidate file paths ``config.files``
    names.

    Always an explicit list — there is no implicit directory walk, so it
    must name everything the target covers. Entries are repo-root-relative
    and may be either:

      * a **file** — ``include/internal/statem.h``
      * a **directory**, written with a trailing slash — ``ssl/`` — which
        expands to every source/header file beneath it

    The trailing-slash convention matches ``out_of_scope.paths``, so the
    two path fields read the same way, and ``out_of_scope`` applies to
    whatever a directory entry expands to. Directory entries are what make
    "this whole subtree **plus** these specific headers" expressible: a
    port cluster often spans a tree it does not own, because the structs
    its code implements are declared in headers that live elsewhere.

    The result is only the *candidate* set; the T1 tables decide which of
    these actually compiled under the build configuration — so naming a
    file that the build never compiled is harmless, not an error.
    """
    out_of_scope = list(config.get("out_of_scope", {}).get("paths", []))
    entries = config.get("files") or []

    files: list[str] = []
    for entry in entries:
        if entry.endswith("/"):
            files.extend(_walk_dir(entry, repo_root, out_of_scope))
        elif not _is_out_of_scope(entry, out_of_scope):
            files.append(entry)
    return sorted(set(files))


def _target_entities(t1_dir: Path, candidate_files: set[str]) -> dict[str, list[dict]]:
    """Classify every T1 entity against the candidate (config-named) file set
    and return the TARGET subset per kind.

    Reuses the single load-bearing classifier in `scope.py` — this is
    the ONE place section classification runs; downstream composers
    consume the result rather than re-deriving it from the CSVs. The
    rules mirror the per-row call sites exactly:

      - functions / globals → `classify` (definition-anchored, decl
        fallback).
      - macros → `entry_scope` with the header-macro carve-out: a
        `#define` belongs to the target only when its home is a target
        `.c` TU.
      - types → `classify_type` (typedef alias-chain resolution).

    Entities are keyed by (name, defined_in); `defined_in` may be empty for
    declaration-only entities.
    """
    funcs = _scope.load_csv(t1_dir / "functions.csv")
    globs = _scope.load_csv(t1_dir / "globals.csv")
    macros = _scope.load_csv(t1_dir / "macros.csv")
    types_rows = _scope.load_csv(t1_dir / "types.csv")
    by_name = _scope.build_types_index(types_rows)

    def _cls(r: dict) -> bool:
        return _scope.classify(
            r["def_file"], _scope.parse_decl_files(r["decl_files"]), candidate_files
        ) == _scope.TARGET

    # Every entry carries BOTH `defined_in` and `declared_in` (the latter the
    # def_file-empty complement: a null-def callback/extern/typedef is keyed by
    # its declaring header). `linkage` is dropped — never read off scope.json.
    def _ent(r: dict, **extra) -> dict:
        return {"name": r["name"], "defined_in": r["def_file"],
                "declared_in": _scope.parse_decl_files(r.get("decl_files", "")),
                **extra}

    tgt_funcs = [_ent(r) for r in funcs if _cls(r)]
    tgt_globs = [_ent(r) for r in globs if _cls(r)]
    tgt_macros = []
    for r in macros:
        df = r["def_file"] or ""
        if _scope.entry_scope("macro", df, [df] if df else [], candidate_files) == _scope.TARGET:
            tgt_macros.append(_ent(r))
    # Types split: a callback is a function-pointer typedef — symbol-shaped, not
    # a layout type — so it is bucketed with `functions` (untagged), matching how
    # import_closure emits import-side callbacks. Everything else is a real type.
    tgt_types = []
    for r in types_rows:
        if _scope.classify_type(r, by_name, candidate_files) != _scope.TARGET:
            continue
        # An ANONYMOUS tag (`(unnamed enum)`, `(unnamed class/struct/union)`) is
        # a synthetic placeholder CodeQL reuses for every anonymous definition in
        # the DB, so it is not a name anything can reference or place: dozens of
        # distinct types collide on the one string. The analysis tree already
        # drops them (types.json carries none); this keeps scope.json in
        # agreement. Their FIELDS are not lost — `entities/fields.ql` flattens an
        # anonymous member into its named parent under a qualified name.
        if str(r.get("name") or "").startswith("("):
            continue
        if r.get("unaliased_kind") == "callback":
            tgt_funcs.append(_ent(r))
        else:
            tgt_types.append(_ent(r, kind=r["kind"]))
    keyf = lambda e: (e["defined_in"], e["name"])
    return {
        "functions": sorted(tgt_funcs, key=keyf),
        "globals": sorted(tgt_globs, key=keyf),
        "macros": sorted(tgt_macros, key=keyf),
        "types": sorted(tgt_types, key=keyf),
    }


def _contributing_files(
    t1_dir: Path, entities: dict[str, list[dict]], candidate_files: set[str]
) -> list[str]:
    """Derive the port `files` list: every candidate file that is the
    definition site OR a declaration site of at least one port entity.

    Drops `#ifdef`-elided files (zero entities → never a def/decl site)
    while keeping declaration-only port headers. decl sites are pulled
    from the raw CSVs since the slim entity records omit decl_files.
    """
    files: set[str] = set()
    for kind in ("functions", "globals", "types", "macros"):
        for e in entities[kind]:
            if e["defined_in"] in candidate_files:
                files.add(e["defined_in"])
    keyset = {
        kind: {(e["defined_in"], e["name"]) for e in entities[kind]}
        for kind in ("functions", "globals", "types")
    }
    for csv_name, kind in (("functions.csv", "functions"),
                           ("globals.csv", "globals"),
                           ("types.csv", "types")):
        for r in _scope.load_csv(t1_dir / csv_name):
            if (r["def_file"], r["name"]) not in keyset[kind]:
                continue
            for d in _scope.parse_decl_files(r.get("decl_files", "")):
                if d in candidate_files:
                    files.add(d)
    return sorted(files)


def compose(config_path: Path, t1_dir: Path, repo_root: Path | None = None) -> dict:
    """Emit the manifest's `target` section.

    `t1_dir` is `<repo_root>/crustify/codeql/t1` — the entity tables
    produced by `analyze extract-ql`. Expanding `files` gives the
    candidate universe; the T1 tables decide which files and entities
    actually compiled under this build configuration.

    `repo_root` is the CLI's first positional (per the Layout contract); it is
    **not** read from `scope-config.json`, so the same in-repo config stays
    portable across git worktrees. When omitted (e.g. standalone `main()`), it
    is derived from the canonical `<repo_root>/crustify/codeql/t1` location of
    `t1_dir`.
    """
    config = json.loads(config_path.read_text())
    if repo_root is None:
        repo_root = t1_dir.resolve().parents[2]
    else:
        repo_root = Path(repo_root).resolve()
    candidate_files = set(enumerate_files(config, repo_root))
    entities = _target_entities(t1_dir, candidate_files)
    files = _contributing_files(t1_dir, entities, candidate_files)
    return {
        "_comment": _COMMENT,
        _scope.TARGET: {
            "files": files,
            "functions": entities["functions"],
            "globals": entities["globals"],
            "macros": entities["macros"],
            "types": entities["types"],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Emit <target>/scope.json from scope-config.json."
    )
    ap.add_argument(
        "--config", type=Path, required=True,
        help="Path to <target>/scope-config.json.",
    )
    ap.add_argument(
        "--t1", type=Path, required=True,
        help="Path to the CodeQL T1 CSV dir (<repo_root>/crustify/codeql/t1).",
    )
    ap.add_argument(
        "--out", type=Path, required=True,
        help="Path to write scope.json (typically <target>/.crustify/scope.json).",
    )
    args = ap.parse_args()

    manifest = compose(args.config, args.t1)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    p = manifest[_scope.TARGET]
    print(
        f"target: {len(p['files'])} files, {len(p['functions'])} fn / "
        f"{len(p['globals'])} gv / {len(p['macros'])} mac / "
        f"{len(p['types'])} ty → {args.out}"
    )


if __name__ == "__main__":
    main()
