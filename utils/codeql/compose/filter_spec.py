"""Shared CLI-narrowing filter for composer + redo precursors.

Carries the full set of CLI-level analyze flags in one bundle so the
composer, agent, and redo precursors all share the same semantics.

## Fields

  - **`dirs` / `files` / `names`** — seed selectors. An entry is a
    *seed* when it matches at least one of these (union semantics).
    Seed mode is active iff any of these is non-empty.
  - **`scope_json_path`** — optional path to a `scope.json` enabling
    port/wrap classification + the seed admission gate. When
    `None`, the composer treats every entry as wrap-scope (base
    analysis only — no port additions, no closure expansion).
  - **`port_only` / `wrap_only`** — mutually exclusive post-emission
    filters. After the seed/closure logic runs, keep only entries
    that emitted with port additions (`port_only`) or only those
    that emitted as wrap-shape (`wrap_only`).

## Behaviour grid

| scope.json passed? | Effect on seeds and closure |
|---|---|
| **No** (default) | No port/wrap split. Every entry is wrap-shape (base only). No closure expansion. Seeds emit only if they pass `--port-only`/`--wrap-only` (and the wrap_only side admits everything). |
| **Yes** | Port/wrap classification per the scope.json. A seed is admitted iff it's port-scope OR wrap-reachable from port code per the scope.json. Port seeds emit with port additions; wrap seeds + closure entries emit as base shape. |

## Why scope.json is optional now

The previous model implicitly read `<target>/.crustify/scope.json`
on every analyze invocation, making "port-aware" the default and
silently enabling a wrap-reach gate that could surprise users
(e.g. `--name SSL_new` emitting nothing because statem doesn't
reach SSL_new). Making `--scope` opt-in surfaces the gate explicitly
in the CLI invocation. Without `--scope`, the composer does no
port-related reasoning at all — the output is purely seed-driven.

`analyze_scope` (a composer-only stage) still
reads the target's `scope.json` implicitly — that command is
defined as "operate on the target's port-scope file set," not as
a general query.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FilterSpec:
    """Bundle of CLI narrowing flags. See module docstring."""

    # Seed selectors (any-of union).
    dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)

    # The composed scope manifest (dict) enabling port-aware analysis; a Path
    # to a `scope.json` is still accepted for the standalone composer CLIs.
    # None disables the seed gate AND port/wrap classification entirely, which
    # widens the emit to the repo-wide universe — so a caller that MEANT to be
    # scoped must never let this fall to None silently.
    scope_json_path: "Path | dict | None" = None

    # Post-emission filters (mutually exclusive). port_only keeps
    # only entries emitted with port additions; wrap_only keeps
    # only base-shape entries.
    port_only: bool = False
    wrap_only: bool = False

    # Emit EVERY candidate, skipping the out-of-scope reachability drop
    # (repo-wide inventory). scope.json still classifies port/wrap for
    # entries that qualify; out-of-scope entries emit as base-shape and are
    # classified out-of-scope by scope.json absence. The default (False) is
    # scope-only: emit port + wrap-reachable, drop the rest.
    unscoped: bool = False

    def is_empty(self) -> bool:
        """True iff every narrowing flag is at its default value."""
        return (
            not self.dirs
            and not self.files
            and not self.names
            and self.scope_json_path is None
            and not self.port_only
            and not self.wrap_only
        )

    def is_seed_mode(self) -> bool:
        """True iff any of `--dir` / `--file` / `--name` is set."""
        return bool(self.dirs or self.files or self.names)

    def expand_closure(self) -> bool:
        """In seed mode, whether to pull the transitive field-type /
        dependency closure of the seeds into the emitted manifest.

        `--name` is **precise**: it seeds ONLY the named entities, with NO
        closure expansion (focused single-entity analysis - e.g. one type
        for a model-comparison run). `--dir` / `--file` still expand the
        closure of the region they select. When `--name` is combined with a
        dir/file selector, the precise (no-closure) semantics win.
        """
        return self.is_seed_mode() and not self.names


def _normalize_dir(d: str) -> str:
    return d if d.endswith("/") else d + "/"


def _entry_paths(entry: dict[str, Any]) -> list[str]:
    """Source-tree paths the entry occupies — `defined_in` plus the
    first `declared_in` (when present). Used by dir-prefix matching.
    """
    paths: list[str] = []
    df = entry.get("defined_in")
    if df:
        paths.append(df)
    decls = entry.get("declared_in")
    if isinstance(decls, list) and decls:
        paths.append(decls[0])
    elif isinstance(decls, str) and decls:
        paths.append(decls)
    return paths


def _matches_dir(entry: dict[str, Any], dirs_norm: list[str]) -> bool:
    for p in _entry_paths(entry):
        for pref in dirs_norm:
            if p == pref.rstrip("/") or p.startswith(pref):
                return True
    return False


def _matches_file(entry: dict[str, Any], files_set: set[str]) -> bool:
    df = entry.get("defined_in")
    if df and df in files_set:
        return True
    decls = entry.get("declared_in")
    if isinstance(decls, list):
        return any(d in files_set for d in decls)
    if isinstance(decls, str):
        return decls in files_set
    return False


def is_seed(
    entry: dict[str, Any],
    spec: FilterSpec,
    *,
    name_key: str = "name",
) -> bool:
    """Predicate: is `entry` a seed under `spec`?

    A seed is an entry that matches at least one of the
    `--dir` / `--file` / `--name` predicates. Returns False when
    `spec.is_seed_mode()` is False (no narrowing flags set).
    """
    if not spec.is_seed_mode():
        return False

    if spec.names and entry.get(name_key) in spec.names:
        return True
    if spec.files and _matches_file(entry, set(spec.files)):
        return True
    if spec.dirs and _matches_dir(
        entry, [_normalize_dir(d) for d in spec.dirs]
    ):
        return True
    return False


def entry_matches(
    entry: dict[str, Any],
    spec: FilterSpec,
    *,
    is_port_scope: bool,
    name_key: str = "name",
) -> bool:
    """Filter-mode predicate (legacy intersection semantics). Used by
    `types_manifest` (no closure) and by redo precursors.

    Treats `dirs` / `files` / `names` as an intersection filter and
    applies the `port_only` / `wrap_only` post-filter.
    Returns True for every entry when `spec.is_empty()`.
    """
    if spec.is_empty():
        return True

    has_what = bool(spec.dirs or spec.files or spec.names)

    union_match = False
    if spec.names and entry.get(name_key) in spec.names:
        union_match = True
    if not union_match and spec.files and _matches_file(entry, set(spec.files)):
        union_match = True
    if not union_match and spec.dirs and _matches_dir(
        entry, [_normalize_dir(d) for d in spec.dirs]
    ):
        union_match = True

    if has_what and not union_match:
        return False

    # Post-filter.
    if spec.port_only and not is_port_scope:
        return False
    if spec.wrap_only and is_port_scope:
        return False

    return True
