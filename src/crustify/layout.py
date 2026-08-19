"""Single-directory artifact layout.

All crustify artifacts live under one visible ``crustify/`` directory at
the repository root. ``repo_root`` is an **explicit input** (the CLI's
first positional, pinned via :func:`set_repo_root`) — never discovered by
walking the filesystem — and its artifacts live at ``repo_root/crustify/``:

    <repo_root>/
      crustify/
        build.json  crates.json  cli-config.json   # repo-tier, project-wide
        ownership-store.json                      # the authored analysis
        codeql/{t1,t2,db}/                        # CodeQL db + fact tables
        targets/<repo-relative-target>/           # per-target invocation state
          scope-config.json                       # authored: objective + file sets
          scope.json  deps-dag.json               # derived, fingerprinted
          deps-dag.full.json                      # ditto, body-deep (--full)
          logs/
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
    def rust(self) -> Path:
        return self.root / "rust"

    def providers(self, cli: str) -> Path:
        """Config home crustify hands a provider CLI (``claude`` / ``codex``),
        so a run reads crustify's settings rather than the operator's.

        Not a full sandbox: claude keeps session transcripts in the real
        ``~/.claude/projects/`` regardless of ``ANTHROPIC_CONFIG_DIR``, and
        codex resolves auth from ``CODEX_HOME`` — so pointing that at a
        crustify path loses a ChatGPT-subscription login (an OpenRouter
        ``env_key`` needs no auth file and is unaffected)."""
        d = self.root / ".providers" / cli
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def repo_config(self) -> Path:
        """Repo-wide crustify config (``cli-config.json``) — two blocks:
        ``deps``, the absolute paths to the crustify and ffibox
        checkouts, and ``bins``, the absolute path to each tool a skill declares
        via its ``bin:`` key. Lives at the ``crustify/`` root so it is shared
        across targets. The skill set itself is not configured here: it is
        :attr:`crustify.agents.base.CrustifyAgent.SKILLS`, resolved through
        ``deps``.

        Named apart from the per-target :meth:`config` (``scope-config.json``)
        on purpose: two files both called ``config.json``, one repo-tier and one
        target-tier, are indistinguishable in a diff, a log line or a prompt."""
        return self.root / "cli-config.json"

    # ------------------------------------------------- target-tier (per-target)
    def rel_target(self, target: Path) -> str:
        t = Path(target).resolve()
        if t == self.repo_root:
            return "."
        return t.relative_to(self.repo_root).as_posix()

    def target_dir(self, target: Path) -> Path:
        return self.root / "targets" / self.rel_target(target)

    def config(self, target: Path) -> Path:
        """The target's authored SCOPE definition (``scope-config.json``):
        ``campaign_objective`` (``port`` | ``wrap``), the two file sets
        ``impl_files`` / ``api_headers``, and ``out_of_scope``. Input to the
        scope composer, which derives the sibling ``scope.json`` — a separate file so
        a recompute of that derived output can never clobber it. The repo root
        and the target id are CLI positionals, and this file's own location
        records the target, so neither is restated inside it."""
        return self.target_dir(target) / "scope-config.json"

    def scope(self, target: Path) -> Path:
        """The derived ``targeted`` set, ``imported`` closure and ``api`` view —
        a fingerprinted cache (:mod:`crustify.cache`).

        ONE file, no ``full`` variant: scope no longer depends on
        ``campaign_objective``, so there is only ever one composition of it.
        The objective survives as a dag-only fork, and it is `deps-dag.json`
        that has a ``.full.`` sibling."""
        return self.target_dir(target) / "scope.json"

    def deps_dag(self, target: Path, *, full: bool = False) -> Path:
        """The layered dependency graph, beside scope.json — a fingerprinted
        cache (:mod:`crustify.cache`).

        Target-tier because the graph is: its edges are narrowed by scope, so an
        imported node contributes only its signature and the layering differs
        per target. ``full`` names the body-deep sibling, for the same reason
        :meth:`scope` does."""
        return self.target_dir(target) / (
            "deps-dag.full.json" if full else "deps-dag.json")

    def logs(self, target: Path) -> Path:
        return self.target_dir(target) / "logs"

