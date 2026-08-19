"""scope.py — the per-target scope manifest, composed in memory.

`scope.json` is a pure function of `scope-config.json` (hand-authored) and the
CodeQL T1/T2 tables. Every stage read it off disk, which made it the one
artifact whose staleness a human could cause directly: edit `scope-config.json`,
forget `analyze scope`, and wrap/query/dag all run against the previous port
set without a word. That is exactly the failure `dag.build` removed for the
graph, and the same fix applies — compose it, don't cache it.

Two halves, both composer-only:

  ``targeted``  from `scope-config.json` + T1, ~0.12s
  ``imported``  the closure of ``targeted`` over T1/T2, ~1.4s (dominated by
                parsing `macro_expansions.csv` and friends)

Plus a cross-cutting ``api`` view: what ``api_headers`` PUBLISHES, selected on
declaration sites. Not a section — it overlaps both.

None of the three depends on ``campaign_objective``. What a campaign COVERS is
a property of its file sets; the objective decides only what the DAG does with
them, and :mod:`compose.deps_dag` is its single consumer.

:func:`build` memoizes per `(repo_root, target)` for the life of the process,
which is what makes the multi-read commands cheap: `deps_dag.compose` alone
wants it twice, and `query` up to six times.

`analyze scope --dump` still writes the JSON. Nothing reads it back.
"""
from __future__ import annotations

import sys
from pathlib import Path

from crustify.layout import Layout


#: (repo_root, target) -> composed manifest. Process-lifetime only: a stage is
#: one process, and the CSVs cannot change under it mid-run.
_CACHE: dict[tuple[str, str], dict] = {}


def build(layout: Layout, target: Path, *, stage: str) -> dict:
    """Compose this target's scope manifest — both sections plus the ``api``
    view — and return it.

    Raises ``SystemExit`` with a stage-tagged message when an input is missing,
    so a caller never has to pre-check.
    """
    ck = (str(layout.repo_root), str(target))
    hit = _CACHE.get(ck)
    if hit is not None:
        return hit

    # On-disk cache, fingerprinted against `scope-config.json` + the CodeQL
    # tables. Saves 1.45s on every command that touches scope, which is all of
    # them: the manifest composers need it to narrow their emit.
    from crustify import cache as _cache
    fp = _cache.fingerprint(layout, target)
    disk = _cache.load(layout.scope(target), fp)
    if disk is not None:
        _CACHE[ck] = disk
        return disk

    from compose import scope_manifest as _sm
    from compose import scope as _scope
    from compose.import_closure import compose_import

    t1, t2 = layout.t1, layout.t2
    if not (t1 / "functions.csv").is_file():
        raise SystemExit(
            f"{stage}: no CodeQL T1 tables at {t1}. "
            f"Run `crustify-oracle {target} extract-ql` first.")
    config_path = layout.config(target)
    if not config_path.is_file():
        raise SystemExit(
            f"{stage}: no scope-config.json at {config_path}. It is authored by "
            f"hand — it names the file sets and the campaign objective — and "
            f"there is nothing to derive scope from without it.")
    includes_csv = t1 / "includes.csv"
    if not includes_csv.is_file():
        raise SystemExit(
            f"{stage}: no includes.csv at {includes_csv}. "
            f"Run `crustify-oracle {target} extract-ql` first.")

    import json
    config = json.loads(config_path.read_text())
    # Validated here even though scope does not branch on it: the dag does, and
    # a typo should fail at the first command that touches the config rather
    # than at the first one that happens to need a graph.
    try:
        objective = _sm.campaign_objective(config)
    except ValueError as e:
        raise SystemExit(f"{stage}: {config_path}: {e}")
    manifest = _sm.compose(config_path, t1, layout.repo_root)
    target_paths = _scope.load_targeted_paths(manifest)
    # An empty file set means the campaign covers nothing. There is no implicit
    # walk to fall back on, so this is always a config error — a mistyped path,
    # or a list that never got filled in — and it would otherwise compose a
    # well-formed, entirely empty scope that every later stage reports as
    # "nothing to do".
    if not target_paths:
        raise SystemExit(
            f"{stage}: {config_path} selects no files. `{_sm.IMPL_FILES}` and "
            f"`{_sm.API_HEADERS}` are both empty, or name paths that nothing "
            f"under {layout.repo_root} matches, or name only files this build "
            f"never compiled (the sets are anchored on the T1 tables).")
    # The imported half needs the targeted half, and only that — it reads
    # neither syms.json nor types.json, so scope stands alone ahead of the
    # manifest composers.
    manifest[_scope.IMPORTED] = compose_import(
        t1, t2, manifest,
        _scope.load_csv(includes_csv),
        target_paths,
        _scope.load_csv(t1 / "types.csv"),
        _scope.load_csv(t2 / "field_type_uses.csv"),
    )
    manifest = _cache.store(layout.scope(target), manifest, fp)
    _CACHE[ck] = manifest
    return manifest


def try_build(layout: Layout, target: Path) -> dict | None:
    """`build`, returning ``None`` instead of exiting when scope cannot be
    composed — for the callers that treat a scope-less target (``.``) as "no
    classification available" rather than an error.

    Call it only on the branch that needs scope. Composing costs ~1.5s, and the
    common oracle query (`query syms --name X` with no scope filter) has no
    business paying it.
    """
    try:
        return build(layout, target, stage="scope")
    except SystemExit:
        return None
