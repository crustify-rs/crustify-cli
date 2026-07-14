"""Single-directory artifact layout.

All crustify artifacts live under one visible ``crustify/`` directory at
the repository root. ``repo_root`` is an **explicit input** (the CLI's
first positional, pinned via :func:`set_repo_root`) — never discovered by
walking the filesystem — and its artifacts live at ``repo_root/crustify/``:

    <repo_root>/
      crustify/
        build.json   alloc.json                  # repo-tier, project-wide
        analysis/    codeql/{t1,t2,db}/
        targets/<repo-relative-target>/           # per-target invocation state
          config.json   scope.json   logs/   kiss/
        rust/                                     # the shared Rust crates
      ssl/  crypto/  …                            # vanilla C tree (no artifacts)

A *target* is addressed by its repo-relative path (``ssl/statem``) — the
CLI's second positional. The repo root itself is addressable as ``.`` (or an
empty target); a repo-wide analysis is normally driven by ``--unscoped`` on a
real target rather than by targeting the root.
"""
from __future__ import annotations

import os
from pathlib import Path

CRUSTIFY = "crustify"

OUT_SUFFIX_ENV = "CRUSTIFY_OUT_SUFFIX"


def manifest_name(kind: str) -> str:
    """Per-stem manifest filename for a manifest `kind` (``type``/``types``
    -> ``types.json``; anything else -> ``syms.json``), honoring the
    ``CRUSTIFY_OUT_SUFFIX`` env var for isolated parallel analyzer runs.

    With ``CRUSTIFY_OUT_SUFFIX=opus`` the names become ``types_opus.json`` /
    ``syms_opus.json`` so concurrent runs write disjoint files: the canonical
    (suffix-less) tree is left untouched and downstream consumers
    (``rglob("types.json")``) ignore the suffixed artifacts. Set by
    ``analyze types/syms --out-suffix``; read by both the composer emit and
    every ``crustify query`` the agents shell out to (env inheritance) - the
    agents pass no path, so the suffix must travel through the environment."""
    base = "types" if kind in ("type", "types") else "syms"
    suffix = os.environ.get(OUT_SUFFIX_ENV, "").strip()
    return f"{base}_{suffix}.json" if suffix else f"{base}.json"


_REPO_ROOT: Path | None = None  # pinned once by the CLI; never marker-walked


def set_repo_root(repo_root: Path) -> None:
    """Pin the repo root explicitly — the CLI's first positional. Once set,
    :meth:`Layout.discover` returns it directly: crustify never walks the
    filesystem looking for a ``crustify/`` marker."""
    global _REPO_ROOT
    _REPO_ROOT = Path(repo_root).resolve()


def find_repo_root(start: Path) -> Path:
    """The pinned repo root (:func:`set_repo_root`). With nothing pinned —
    e.g. a direct library/test caller — ``start`` itself is taken as the repo
    root. **Never** walks ancestors; the repo root is an explicit input."""
    if _REPO_ROOT is not None:
        return _REPO_ROOT
    return Path(start).resolve()


class Layout:
    """Resolves every crustify artifact path from one ``crustify/`` root."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.root = self.repo_root / CRUSTIFY

    @classmethod
    def discover(cls, start: Path) -> "Layout":
        return cls(find_repo_root(start))

    # ----------------------------------------------------- repo-tier (shared)
    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    @property
    def codeql(self) -> Path:
        return self.root / "codeql"

    @property
    def t1(self) -> Path:
        return self.codeql / "t1"

    @property
    def t2(self) -> Path:
        return self.codeql / "t2"

    @property
    def codeql_db(self) -> Path:
        return self.codeql / "db"

    @property
    def build_json(self) -> Path:
        return self.root / "build.json"

    @property
    def crates_json(self) -> Path:
        return self.root / "crates.json"

    @property
    def alloc_json(self) -> Path:
        return self.root / "alloc.json"

    @property
    def rust(self) -> Path:
        return self.root / "rust"

    @property
    def repo_config(self) -> Path:
        """Repo-wide crustify config — absolute dep paths (crustify-crate,
        kiss_ai, crustify itself) and the SKILL.md set indexed into prompts.
        Distinct from the per-target :meth:`config`; lives at the
        ``crustify/`` root so it is shared across targets."""
        return self.root / "config.json"

    @property
    def port_features(self) -> Path:
        """The CUMULATIVE `CRUSTIFY_<FILE>` flag manifest, in the **git-tracked**
        `rust/` tree (NOT under the symlinked `targets/`), so each isolated
        worktree forks its committed baseline via `git worktree add` and appends
        only its own chain's files — a per-worktree-coherent flag set the merge
        then unions. Read at C-build time by `src/libgit2/CMakeLists.txt`."""
        return self.rust / "port-features.json"

    # ------------------------------------------------- target-tier (per-target)
    def rel_target(self, target: Path) -> str:
        t = Path(target).resolve()
        if t == self.repo_root:
            return "."
        return t.relative_to(self.repo_root).as_posix()

    def target_dir(self, target: Path) -> Path:
        return self.root / "targets" / self.rel_target(target)

    def config(self, target: Path) -> Path:
        return self.target_dir(target) / "config.json"

    def scope(self, target: Path) -> Path:
        return self.target_dir(target) / "scope.json"

    def logs(self, target: Path) -> Path:
        return self.target_dir(target) / "logs"

    def kiss(self, target: Path) -> Path:
        return self.target_dir(target) / "kiss"
