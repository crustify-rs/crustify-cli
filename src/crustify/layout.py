"""Artifact layout for the translation executor."""
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
        """Repo-wide crustify config (``cli-config.json``): dependency checkout
        paths, executable paths and optional prompt capabilities. Lives at the
        ``crustify/`` root so it is shared across targets. Core role skills come
        from the agent class; ``prompt_capabilities`` selects optional generic
        skills and role guidance resolved through ``deps`` and ``bins``.

        Oracle target configuration lives in the standalone oracle tree."""
        return self.root / "cli-config.json"

    # ------------------------------------------------- target identity
    def rel_target(self, target: Path) -> str:
        t = Path(target).resolve()
        if t == self.repo_root:
            return "."
        return t.relative_to(self.repo_root).as_posix()

    @property
    def campaigns(self) -> Path:
        """Root of all target-scoped campaign artifacts."""
        return self.root / "campaigns"

    def campaign_dir(self, target: Path) -> Path:
        """Tracked wave plans and logs for one explicit oracle target."""
        return self.campaigns / self.rel_target(target)

    def logs(self, target: Path) -> Path:
        return self.campaign_dir(target) / "logs"
