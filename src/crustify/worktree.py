"""Git-worktree isolation for parallel wrap/port agents — our own routines.

Parallel wrap/port agents share one Cargo workspace (`crustify/rust/`) and a few
append-only artifacts (`lib.rs`/`mod.rs` module lists, `Cargo.toml`, the shared
per-library port crate each `mod ffi_export` re-export lands in, the C feature
manifest), so (finding F3) their self-`cargo check` is contaminated by siblings'
in-flight edits and they race on the shared files.

This module gives each parallel unit its own **git worktree** of the repo, so it
writes and validates in isolation; a :class:`crustify.agents.merge.CrustifyMerge`
agent then merges the worktrees back. The agent **drives the merge itself** (git
does the mechanical union of the disjoint, file-grained changes; the agent only
hand-resolves the handful of shared-file conflicts and runs the integrated
post-wave validation) — so this module stays pure git plumbing: snapshot, spawn
worktrees, set up a merge worktree, apply the result, tear down.

The one wrinkle: `crustify/` is **untracked** (regenerable artifacts) and
`git worktree` only carries committed state. We capture the full current working
state (tracked C edits ∪ untracked `crustify/`) as a *dangling* commit via a
throwaway index (`GIT_INDEX_FILE` + `write-tree` + `commit-tree`) — **without
touching the user's index, working tree, or branch**. Heavy / read-only-across-
session dirs (`rust/target`, `codeql`, `analysis`, `targets`, `tmp`) are kept out
of the snapshot by `crustify/.gitignore` (so `git add -A` skips them); agents
still read the shared `analysis`/`codeql` via absolute paths into the main tree.

Division of labour (per design):

  - **wrap/port agents** do their codegen **and `git commit`** their work inside
    their own worktree (the orchestrator does not commit for them).
  - **the merge agent** (:class:`crustify.agents.merge.CrustifyMerge`) runs in
    the *main* tree, `git apply --3way`-s each agent's ``base..HEAD`` diff into
    it (resolving the rare shared-file conflict), runs the integrated validation,
    and then **removes the worktrees** — i.e. it owns "apply" and "clean up".
  - this module is the thin **orchestrator-side** plumbing only:

  - :func:`snapshot_base`   — dangling commit of the working state.
  - :func:`add_worktree`    — `git worktree add --detach` at the base.
  - :func:`worktree_head`   — read the commit an agent produced in its worktree.
  - :func:`remove_worktree` / :func:`prune` — teardown helpers used by the **merge
    agent** (happy path) and by :func:`add_worktree` to clear a stale slug. The
    orchestrator no longer prunes after a wave: surviving worktrees from a
    partial/failed wave are preserved for inspection/retry (finding F12).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_WT_DIR = "crustify/.worktrees"   # gitignored; per-session worktrees live here


def _git(repo: Path, *args: str, index: Path | None = None,
         check: bool = True) -> str:
    """Run a git command in ``repo`` and return stdout (stripped). ``index``
    routes the op through a throwaway index so the real one is never touched."""
    env = dict(os.environ, GIT_INDEX_FILE=str(index)) if index is not None else None
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def _temp_index() -> Path:
    with tempfile.NamedTemporaryFile(prefix="crustify-idx.", delete=False) as tf:
        return Path(tf.name)


def snapshot_base(repo: Path) -> str:
    """Capture the full current working state (tracked changes ∪ untracked
    ``crustify/``, minus whatever ``crustify/.gitignore`` excludes) as a
    **dangling commit**, using a throwaway index so the user's index / working
    tree / branch are never touched. Returns the commit SHA."""
    idx = _temp_index()
    try:
        _git(repo, "read-tree", "HEAD", index=idx)
        _git(repo, "add", "-A", index=idx)           # respects .gitignore
        tree = _git(repo, "write-tree", index=idx)
        head = _git(repo, "rev-parse", "HEAD")
        return _git(repo, "commit-tree", tree, "-p", head,
                    "-m", "crustify worktree session base", index=idx)
    finally:
        idx.unlink(missing_ok=True)


def add_worktree(repo: Path, base_commit: str, slug: str) -> Path:
    """Create a detached worktree at ``base_commit`` and return its path. It has
    the full snapshot state and its own index, so the agent writes and
    `cargo check`s in complete isolation."""
    wt = repo / _WT_DIR / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        remove_worktree(repo, wt)
    _git(repo, "worktree", "add", "--detach", "--quiet", str(wt), base_commit)
    return wt


_SHARED = ("analysis", "analysis.baseline", "codeql", "targets", "tmp",
           "crates.json", "build.json")


def link_shared(wt: Path, repo: Path) -> None:
    """Symlink the gitignored, read-only-across-a-wave crustify artifacts (the
    analysis dirs plus the repo-root-tier `crates.json` / `build.json` stores)
    from the main checkout into the worktree, so the worktree is a *complete*
    functional crustify tree (its `Layout` resolves `analysis` / `codeql` /
    `targets` / `crates.json` / `build.json` to the single shared copy) without
    duplicating them. They never change during a wave; agent logs written under
    `targets/` thus land in the shared tree.

    `build.json` is gitignored AND untracked, so unlike the generated headers
    (committed in HEAD, hence carried by `snapshot_base`'s `git add -A`) it
    reaches a worktree by NO other route: `snapshot_base` respects `.gitignore`
    and skips it. The wrap/port/merge agents only READ it (the build
    descriptor's `build_commands` + feature wiring); it is written before any
    wave — so it satisfies the same read-only-across-a-wave contract as
    `crates.json` below.

    ``crates.json`` joins the shared set on the **eager pre-seed** contract:
    the placement oracle must be fully populated for the wave's units BEFORE
    the wave starts (`scaffold --dag-layer N` / `scaffold --all`, single-
    threaded → deterministic, no race), so agents only ever READ it — no
    mid-wave miss-fill, no per-worktree `cp`, no merge-back. A genuine miss
    would write THROUGH the symlink into the shared main copy (a race); that is
    the signal the pre-seed was incomplete, to be hardened with a `chmod 444`
    fail-fast guard if it ever bites."""
    for d in _SHARED:
        src = repo / "crustify" / d
        dst = wt / "crustify" / d
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src.resolve())


def worktree_head(wt: Path) -> str:
    """The commit SHA at the worktree's HEAD — the work the agent committed
    (the orchestrator reads this to hand the merge agent the result commits)."""
    return _git(wt, "rev-parse", "HEAD")


def ensure_committed(wt: Path, message: str) -> str:
    """Safety net: if an agent left uncommitted changes in its worktree, commit
    them so nothing is lost. Returns the head SHA. (The agent is the primary
    committer; this only fires when it forgot.)"""
    _git(wt, "add", "-A")
    if _git(wt, "status", "--porcelain"):
        _git(wt, "commit", "--quiet", "--no-verify", "-m", message)
    return _git(wt, "rev-parse", "HEAD")


def remove_worktree(repo: Path, wt: Path) -> None:
    """Tear down one worktree (force — its detached commit is dangling)."""
    _git(repo, "worktree", "remove", "--force", str(wt), check=False)


def prune(repo: Path) -> None:
    """Drop the worktree dir + administrative records after a session."""
    _git(repo, "worktree", "prune", check=False)
    wt_root = repo / _WT_DIR
    if wt_root.exists():
        import shutil
        shutil.rmtree(wt_root, ignore_errors=True)
