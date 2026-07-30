"""Git-worktree isolation for wrap/port agents — our own routines.

Wrap/port agents share one Cargo workspace (`crustify/rust/`) and a few
append-only artifacts (`lib.rs`/`mod.rs` module lists, `Cargo.toml`, the shared
per-library port crate each `mod ffi_export` re-export lands in, the C feature
manifest), so (finding F3) their self-`cargo check` is contaminated by siblings'
in-flight edits and they race on the shared files.

Every agent therefore gets its **own git worktree**, serial or parallel alike —
isolation is not a parallelism optimisation, it is what makes an agent's scoped
`cargo check` mean anything.

Topology
--------
Two tiers, both real worktrees on real branches::

    <session branch>            crustify/session/<verb>-<SESSION_ID>
      base worktree             crustify/.worktrees/base
        |
        +-- child worktree      crustify/.worktrees/<slug>   (one per agent)
        +-- child worktree      ...

Every name carries the session, so concurrent or successive runs never collide::

    session branch    crustify/session/<verb>-<SESSION_ID>
    base worktree     crustify/.worktrees/base-<verb>-<SESSION_ID>
    child worktree    crustify/.worktrees/<verb>-<SESSION_ID>-<NN>-<stem>
    child branch      crustify/agent/<verb>-<SESSION_ID>-<NN>-<stem>

The **base** holds the session's integrated state. Each **child** forks from the
base branch, and the agent lands its own work by merging back into the base —
`--ff-only` after rebasing onto the current tip. Nothing here does that merging:
it is the agent's job.

Two properties of that hand-off are measured, not assumed:

  - With N children forked from one tip, at most ONE can fast-forward as
    committed. Every later agent MUST rebase onto the advanced tip and retry;
    `--ff-only` refusing is the "someone landed first" signal. Re-running its
    scoped checks after the rebase is what makes the integrated result validated
    rather than merely merged.
  - **`git merge` into the base worktree needs an explicit lock.** Git's
    `index.lock` guards the index WRITE, not the whole checkout, so concurrent
    `--ff-only` merges into one worktree interleave their working-tree updates
    and leave it half-applied (observed: one path staged-added, another
    staged-deleted yet present untracked, from 8 concurrent attempts). The
    divergence check is a correct CAS on the *ref*; it is not mutual exclusion
    on the *working tree*. Either serialize the merge on a lock the agents take,
    or advance the ref with `git update-ref <ref> <new> <old>` — a genuine
    atomic CAS — from the agent's own worktree and refresh the base checkout
    separately. `update-ref` does not touch working files.

Division of labour
------------------
  - **this module** — plumbing only: snapshot the working state onto a session
    branch, materialize the base worktree, fork a child per agent, symlink the
    shared read-only artifacts. It never merges and never tears anything down.
  - **the scheduler** — calls the above, once per session, and spawns agents.
    Its whole involvement in worktree management is "one worktree per agent".
  - **the agents** — codegen, commit, rebase, `--ff-only` into base, retry.

Nothing removes a worktree, and nothing in this module can. A finished child is
left in place deliberately: its branch is the record of what that agent produced,
and a partial or failed wave must stay inspectable (finding F12). Cleaning up is
a deliberate act outside the pipeline (`git worktree remove`).

Worktrees fork from **HEAD**: uncommitted changes in the main checkout are not
carried into them, so a wave is expected to start from a committed tree. What
HEAD cannot carry either is the gitignored, read-only-across-a-wave
state (`analysis`, `codeql`, `targets`, `.providers`, `crates.json`,
`build.json`); :func:`link_shared` symlinks those from the main checkout so a
worktree is a complete functional crustify tree without duplicating them.

The session branch is never merged into the user's own branch here. It is left
for review — landing it is a deliberate, separate act.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple

_WT_DIR = "crustify/.worktrees"   # gitignored; per-session worktrees live here


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in ``repo`` and return stdout (stripped)."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout.strip()


class SessionBase(NamedTuple):
    """The session's integration point: a real branch with a real worktree."""
    branch: str
    path: Path
    commit: str


def session_base(repo: Path, session: str) -> SessionBase:
    """Materialize the session's base: a branch at the current working state,
    checked out in its own worktree.

    A **branch**, not a dangling commit, because it is what the agents merge
    into: `--ff-only` needs a ref to advance, and a commit reachable from no ref
    is gc-able. A **worktree**, not just a branch, so the integrated state exists
    on disk to build and inspect.

    The worktree is NOT itself the concurrency control — see the module
    docstring: two agents merging into it at once corrupt its state. Whatever
    advances this branch must be serialized by the agents (a lock they take) or
    done by ref CAS with the checkout refreshed after.

    Idempotent **within** a session and inert across sessions: both the branch
    and the worktree path carry ``session``, so a later dependency layer of the
    same run adopts the existing base — integrating onto the branch that already
    holds the earlier layers' landed work instead of re-snapshotting the main
    tree — while the next run gets its own.

    Session-scoping the PATH is what makes that safe. With a fixed
    ``.worktrees/base``, a second run adopted the first run's directory and
    returned its own (never-created) branch name, so the first `add_worktree`
    off it died on an unknown revision.

    A previous session's landed work lives only on its own branch: a new session
    snapshots the main checkout, which does not have it. Land the session branch
    before starting the next run, or the earlier output stays stranded.
    """
    branch = f"crustify/session/{session}"
    wt = repo / _WT_DIR / f"base-{session}"
    if wt.exists():
        on = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        if on != branch:
            raise RuntimeError(
                f"session base {wt} has {on} checked out, expected {branch}. "
                f"Remove it (`git worktree remove --force {wt}`) and re-run.")
        return SessionBase(branch, wt, _git(wt, "rev-parse", "HEAD"))
    sha = _git(repo, "rev-parse", "HEAD")
    wt.parent.mkdir(parents=True, exist_ok=True)
    # `worktree add -b` creates the branch AND checks it out in one command; a
    # separate `git branch` first was redundant. Plain `-b`, never `-B`/`--force`:
    # the session name carries a random suffix, so an existing branch of this name
    # is a genuine anomaly, and force-moving it would silently abandon whatever
    # had landed on it.
    _git(repo, "worktree", "add", "--quiet", "-b", branch, str(wt), sha)
    return SessionBase(branch, wt, sha)


def add_worktree(repo: Path, base_ref: str, slug: str) -> Path:
    """Fork a child worktree off ``base_ref`` on its own branch, and return its
    path. It has the full base state and its own index, so the agent writes and
    `cargo check`s in complete isolation.

    ``slug`` is expected to end in a random suffix (see the scheduler), which
    makes the name unique by construction: nothing here removes a pre-existing
    worktree or moves a pre-existing branch. It used to force both, which meant a
    name collision silently destroyed an earlier agent's unlanded branch instead
    of failing. A collision now raises.

    Its own branch (not `--detach`) so the agent's commits stay referenced: they
    are the record of what it produced, and must survive for inspection or retry
    if the agent fails before landing them."""
    wt = repo / _WT_DIR / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--quiet", "-b", f"crustify/agent/{slug}",
         str(wt), base_ref)
    return wt


_SHARED = (".providers", "analysis", "analysis.baseline", "codeql", "targets",
           "tmp", "crates.json", "build.json")


def link_shared(wt: Path, repo: Path) -> None:
    """Symlink the gitignored, read-only-across-a-wave crustify artifacts (the
    analysis dirs plus the repo-root-tier `crates.json` / `build.json` stores)
    from the main checkout into the worktree, so the worktree is a *complete*
    functional crustify tree (its `Layout` resolves `analysis` / `codeql` /
    `targets` / `crates.json` / `build.json` to the single shared copy) without
    duplicating them. They never change during a wave; agent logs written under
    `targets/` thus land in the shared tree.

    `build.json` is gitignored, so unlike the generated headers (committed, hence
    carried by HEAD) it reaches a worktree by NO other route. The wrap/port agents only READ it (the build
    descriptor's `build_commands` + feature wiring); it is written before any
    wave — so it satisfies the same read-only-across-a-wave contract as
    `crates.json` below.

    ``.providers`` must be here for a subtle reason: the agent backends resolve
    the provider CLI's config home as ``Layout(repo_root).providers(cli)``, and an
    isolated agent's ``repo_root`` IS its worktree — while ``Layout.providers``
    **mkdirs** the path. Unlinked, every worktree therefore gets a freshly created
    EMPTY provider config instead of crustify's shared one, and the CLI runs
    against it with no error: a silent loss of provider settings, which is the
    worst available failure mode.

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
