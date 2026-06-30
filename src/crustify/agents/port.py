"""CrustifyPort — the port-stage codegen agent (agent half).

Receives one scheduled batch from :mod:`crustify._schedule` — always a
**free-symbol** pool (functions + globals; types and macros are never scheduled
for port) — and fills the scaffolded anchors with safe Rust, re-exported with
the C body feature-guarded out.

The port divergence from wrap: ``workspace_root = repo_root`` — the agent owns
the per-file ``CRUSTIFY_<FILE>`` build wiring, and re-exports live in each
ported file's ``mod ffi_export`` submodule (no central ``ffi-exports`` crate),
so it operates at the repo root, not just the Rust workspace.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyPort(CrustifyAgent):
    """Port one scheduled batch of free symbols (functions / globals) C → Rust."""

    name = "CrustifyPort"
    model = "claude-opus-4-8"
    output = None  # writes the Rust workspace; idempotency is the per-item todo.

    def __init__(
        self,
        target: Path,
        *,
        symbols: list[dict] | None = None,     # free fns/globals: {name, defined_in}
        feature_file: str = "",
        repo_root: Path | None = None,         # worktree root in an isolated wave
    ) -> None:
        super().__init__(target, repo_root=repo_root)
        self._symbols = list(symbols or [])
        self._feature_file = feature_file

    @property
    def stage(self) -> str:  # type: ignore[override]
        head = ([s.get("name", "batch") for s in self._symbols] or ["batch"])[0]
        return f"port_{re.sub(r'[^A-Za-z0-9_]+', '_', str(head))}"

    def _prompt(self) -> str:
        return (_PKG_ROOT / "prompts" / "port" / "port.md").read_text()

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        # Resolve crustify-crate from the repo-wide dep config when present,
        # falling back to the in-tree sibling layout for un-configured repos.
        crate_root = self._dep(
            "crustify-crate", crustify_root / ".." / "crustify-crate")
        return {
            "target":         self.target_rel,
            "repo_root":      str(self.repo_root),
            # The port divergence: the agent operates at the repo root.
            "workspace_root": str(self.repo_root),
            "analysis_root":  str(self.layout.analysis),
            # Identity only — the agent pulls the rest via `crustify query`.
            "symbols":        json.dumps(self._symbols),
            "feature_file":   self._feature_file,
            "build_json":     str(self.layout.build_json),
            "discipline":     str(crustify_root / "docs" / "DISCIPLINE.md"),
            "crustify_crate": str(crate_root / "src" / "lib.rs"),
            # Always-on principles preamble (AGENTS.md), with the role-scoped
            # skill index (name + description + path) spliced into its
            # `<!-- SKILLS_INDEX -->` sentinel. Inlined as `{principles}`.
            "principles":     self._render_principles(),
        }
