"""CrustifyMerge — apply + validate + clean up after a parallel wrap/port wave.

Parallel wrap/port agents each ran in their own git worktree
(:mod:`crustify.worktree`), wrote file-grained (mostly disjoint) changes, and
**committed their own work** there. This agent runs in the **main** checkout and
owns the finish:

  1. **Apply** every agent's ``base..HEAD`` diff into the main tree
     (`git apply --3way`). Disjoint changes land cleanly; the handful of shared
     append-only artifacts (`lib.rs`/`mod.rs` module lists, `Cargo.toml`, the
     per-library port crate the `mod ffi_export` re-exports land in, the C
     feature manifest) may conflict — almost always a union of additions, which
     it resolves.
  2. **Validate** the unified tree — the integrated `cargo check` / `clippy`
     (and, for port, the C flag-on/off build matrix) no isolated agent could run.
  3. **Clean up** — `git worktree remove` every per-agent worktree.

Because it runs in the main tree (not a worktree), "apply" is inherent — the
merged result simply *is* the main working tree when it finishes — and it can
remove every worktree (none is its own cwd). By design it has little to do.
"""
from __future__ import annotations

import json
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyMerge(CrustifyAgent):
    """Apply the parallel wave's per-agent commits into the main tree, resolve
    the rare shared-file conflicts, run the integrated validation, and remove the
    worktrees."""

    name = "CrustifyMerge"
    model = "claude-opus-4-8"
    prompt_dir = "merge"
    output = None  # idempotency is the orchestrator's (one merge per wave).
    _commits_own_work = False  # leaves the merged result uncommitted in the main tree

    def __init__(
        self,
        target: Path,
        *,
        base_commit: str,              # the wave's snapshot base
        results: list[dict],           # [{"slug","worktree","commit"}] per agent
        crates: list[str],             # crates touched this wave (validation scope)
        stage: str,                    # "wrap" | "port"
        feature_file: str | None = None,   # port: the CRUSTIFY_<FILE> manifest
    ) -> None:
        super().__init__(target)       # work_dir defaults to the main target tree
        self._base_commit = base_commit
        self._results = list(results)
        self._crates = list(crates)
        self._wave_stage = stage
        self._feature_file = feature_file or ""

    @property
    def stage(self) -> str:  # type: ignore[override]
        return f"merge_{self._wave_stage}"

    def _prompt(self) -> str:
        return (_PKG_ROOT / "prompts" / "merge" / "merge.md").read_text()

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        return {
            "repo_root":      str(self.repo_root),
            "workspace_root": str(self.layout.rust),
            "base_commit":    self._base_commit,
            "results":        json.dumps(self._results),
            "crates":         json.dumps(self._crates),
            "wave_stage":     self._wave_stage,
            "feature_file":   self._feature_file,
            "build_json":     str(self.layout.build_json),
            "discipline":     str(crustify_root / "docs" / "DISCIPLINE.md"),
        }
