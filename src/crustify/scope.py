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

Which half the config SEEDS is `campaign_objective`: a ``port`` campaign seeds
``targeted`` off ``impl_files`` + ``api_headers`` and derives ``imported`` as
its closure; a ``wrap`` campaign owns nothing, so ``targeted`` composes empty
and ``imported`` is seeded off ``api_headers`` directly.

:func:`build` memoizes per `(repo_root, target, full)` for the life of the process,
which is what makes the multi-read commands cheap: `deps_dag.compose` alone
wants it twice, and `query` up to six times.

`analyze scope --dump` still writes the JSON. Nothing reads it back.
"""
from __future__ import annotations

import sys
from pathlib import Path

from crustify.layout import Layout


#: (repo_root, target, full) -> composed manifest. Process-lifetime only: a
#: stage is one process, and the CSVs cannot change under it mid-run. `full` is
#: part of the key because it composes a DIFFERENT manifest from the same
#: inputs — a wrap campaign read with port seeding.
_CACHE: dict[tuple[str, str, bool], dict] = {}


def build(layout: Layout, target: Path, *, stage: str, full: bool = False) -> dict:
    """Compose this target's scope manifest — both sections — and return it.

    ``full`` overrides ``campaign_objective`` to ``port`` for this composition:
    a wrap campaign's ``impl_files`` become targeted, so bodies and full struct
    layouts are in scope. It is the scope half of ``query dag --full`` and
    changes nothing on a campaign that is already ``port``.

    Raises ``SystemExit`` with a stage-tagged message when an input is missing,
    so a caller never has to pre-check.
    """
    ck = (str(layout.repo_root), str(target), full)
    hit = _CACHE.get(ck)
    if hit is not None:
        return hit

    # On-disk cache, fingerprinted against `scope-config.json` + the CodeQL
    # tables. Saves 1.45s on every command that touches scope, which is all of
    # them: the manifest composers need it to narrow their emit. The `full`
    # view caches to its own file, so the two never overwrite each other.
    from crustify import cache as _cache
    fp = _cache.fingerprint(layout, target)
    disk = _cache.load(layout.scope(target, full=full), fp)
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
    # The campaign is STATED, not inferred from which file key is populated:
    # both `impl_files` and `api_headers` stand on every campaign, and
    # `campaign_objective` alone decides which of them seeds which section.
    # `full` overrides it to `port` — the same config read body-deep.
    try:
        objective = _sm.PORT if full else _sm.campaign_objective(config)
    except ValueError as e:
        raise SystemExit(f"{stage}: {config_path}: {e}")
    seed_paths = set(_sm.seed_candidates(config, layout.repo_root, objective))
    manifest = _sm.compose(config_path, t1, layout.repo_root, objective)
    target_paths = _scope.load_targeted_paths(manifest)
    # An empty file set means the campaign covers nothing. There is no implicit
    # walk to fall back on, so this is always a config error — a mistyped path,
    # or a list that never got filled in — and it would otherwise compose a
    # well-formed, entirely empty scope that every later stage reports as
    # "nothing to do".
    if not target_paths and not seed_paths:
        named = (f"`{_sm.IMPL_FILES}` and `{_sm.API_HEADERS}` are"
                 if objective == _sm.PORT else f"`{_sm.API_HEADERS}` is")
        raise SystemExit(
            f"{stage}: {config_path} selects no files. On a `{objective}` "
            f"campaign {named} empty, or name(s) no path under "
            f"{layout.repo_root} matches.")
    # The imported half needs the targeted half, and only that — it reads
    # neither syms.json nor types.json, so scope stands alone ahead of the
    # manifest composers.
    manifest[_scope.IMPORTED] = compose_import(
        t1, t2, manifest,
        _scope.load_csv(includes_csv),
        target_paths,
        _scope.load_csv(t1 / "types.csv"),
        _scope.load_csv(t2 / "field_type_uses.csv"),
        seed_paths,
    )
    manifest = _cache.store(layout.scope(target, full=full), manifest, fp)
    _CACHE[ck] = manifest
    return manifest


def try_build(layout: Layout, target: Path, *, full: bool = False) -> dict | None:
    """`build`, returning ``None`` instead of exiting when scope cannot be
    composed — for the callers that treat a scope-less target (``.``) as "no
    classification available" rather than an error.

    Call it only on the branch that needs scope. Composing costs ~1.5s, and the
    common oracle query (`query syms --name X` with no scope filter) has no
    business paying it.
    """
    try:
        return build(layout, target, stage="scope", full=full)
    except SystemExit:
        return None
