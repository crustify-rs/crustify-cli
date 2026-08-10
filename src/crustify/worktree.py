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
One integration branch with no checkout, and one worktree per agent::

    session branch    crustify/session/<verb>-<SESSION_ID>   (no worktree)
      child worktree  crustify/.worktrees/<verb>-<SESSION_ID>-<NN>-<stem>-<hex8>
      child branch    crustify/agent/<verb>-<SESSION_ID>-<NN>-<stem>-<hex8>
      ...

Landing
-------
An agent commits in its own worktree and lands by pushing to the session branch
in the LOCAL repository (no remote, no server)::

    G=$(git rev-parse --git-common-dir)
    until git push -q "$G" "HEAD:refs/heads/<session branch>"; do
        git rebase <session branch>    # resolve conflicts, --continue, re-check
    done

That is race-free with no lock, because a push is a single atomic ref update
with the fast-forward check evaluated INSIDE the ref lock: exactly one of N
concurrent pushes wins and the rest are rejected as non-fast-forward, which is
the signal to rebase and retry. Rebasing keeps the branch strictly linear.
Verified with 8 agents landing simultaneously while all editing one shared
append-only file: 8/8 landed, one parent per commit, every addition preserved.

What does NOT work, so nobody re-derives it:

  - `git merge --ff-only` run in a shared base worktree. `index.lock` does
    contend, but its scope is the index WRITE — the merge's fast-forward
    decision reads HEAD *before* it and the ref update lands *after* it, so a
    merge that judged the ff legal against a stale HEAD can still take the lock
    later and apply its tree over the winner's. Measured 3/3 on pristine repos
    with 8 concurrent merges: one won HEAD, three had checked out their files,
    and a fourth's index write survived — HEAD, index and working tree in mutual
    disagreement. A wide merge window only *looks* clean because the loser
    aborts before reaching its own checkout, so safe-abort vs. inconsistent-base
    is a timing lottery.
  - The same push with `receive.denyCurrentBranch=updateInstead`, which exists
    to allow pushing to a checked-out branch. Concurrent pushes interleaved the
    checkout, left the base dirty, and its clean-tree precondition then locked
    everyone out: 1 landed, 7 livelocked.
  - `git update-ref <ref> <new>` without the old value. It is only a
    compare-and-swap with `<old>`; the two-argument form exits 0 and leaves a
    sibling's landed commit unreachable. Push needs no such argument.

Division of labour
------------------
  - **this module** — plumbing only: create the session branch, fork a worktree
    per agent, symlink the shared read-only artifacts. It never lands, merges,
    or tears anything down.
  - **the scheduler** — calls the above and spawns agents. Its whole involvement
    in worktree management is "one worktree per agent".
  - **the agents** — codegen, commit, push, rebase-on-rejection, retry.

Nothing in this module removes a worktree. An agent purges its OWN child when it
has landed (`git worktree remove --force .` works from inside it, and the
`crustify/agent/<slug>` branch survives as the record of what it produced). So a
child DIRECTORY that outlives a wave marks an agent that did not finish — the
inspectable-failure guarantee of finding F12, as a signal rather than a pile.

Worktrees fork from **HEAD**: uncommitted changes in the main checkout are not
carried into them, so a wave is expected to start from a committed tree. What
HEAD cannot carry either is the gitignored, read-only-across-a-wave state
(`codeql`, `targets`, `.providers`, `cli-config.json`,
`cli-config.json`);
:func:`link_shared` symlinks those from the main checkout so a worktree is a
complete functional crustify tree without duplicating them.

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
    """The session's integration point: a branch. Nothing is checked out on it —
    see :func:`session_base`."""
    branch: str
    commit: str


def session_base(repo: Path, session: str) -> SessionBase:
    """Create (or adopt) the session's integration branch at ``HEAD``.

    A branch and **no worktree**. Nothing checks it out, which is precisely what
    lets agents land on it concurrently: `git push <git-common-dir>
    HEAD:refs/heads/<branch>` is a single atomic ref update with the
    fast-forward check evaluated INSIDE the ref lock, and git refuses to push to
    a branch that IS checked out somewhere (because that would desynchronize the
    checkout). So the absence of a base worktree is load-bearing, not thrift.

    Giving the base a worktree was tried and dropped: nothing read it. Agents
    land by push and rebase from the branch NAME, so the checkout only ever went
    stale, needed a refresh command, and needed a never-edit-it rule to make the
    refresh safe. Materialize one on demand instead::

        git worktree add --detach <path> crustify/session/<verb>-<SESSION_ID>

    Idempotent within a session and inert across sessions: the branch name
    carries ``session``, so a later dependency layer of the same run adopts the
    branch that already holds the earlier layers' landed work rather than
    resetting it to HEAD, while the next run gets its own.

    A previous session's work lives only on its own branch — a new session
    branches from HEAD, which does not have it. Land the session branch before
    starting the next run, or the earlier output stays stranded.
    """
    branch = f"crustify/session/{session}"
    existing = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                    check=False)
    if existing:
        return SessionBase(branch, existing)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", branch, sha)
    return SessionBase(branch, sha)


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


#: Gitignored, read-only-across-a-wave artifacts symlinked into each worktree.
#: A TRACKED artifact must not be here: git checks it out from HEAD, so the
#: worktree already holds its own copy, and sharing a written one would send
#: every agent's writes into the same file. `ownership-store.json` is tracked
#: for that reason — an agent submits through `query --update` into its own
#: copy and its landing commit carries the findings, the route its Rust output
#: already takes.
_SHARED = (".providers", "codeql", "targets", "tmp", "cli-config.json")

#: Shared entries created ON DEMAND rather than by an earlier stage, so the
#: main checkout may not hold them yet when the first wave starts. They are
#: seeded there before linking (see :func:`link_shared`); everything else in
#: `_SHARED` is produced by a prior stage and its absence is a real error.
_SHARED_LAZY_DIRS = (".providers", "tmp")


def link_shared(wt: Path, repo: Path) -> None:
    """Symlink the gitignored, read-only-across-a-wave crustify artifacts from
    the main checkout into the worktree, so the worktree is a *complete*
    functional crustify tree (its `Layout` resolves `codeql` /
    `targets` / `crates.json` / `build.json` to the single shared copy) without
    duplicating them. They never change during a wave; agent logs written under
    `targets/` thus land in the shared tree.

    `build.json` is gitignored, so it reaches a worktree by NO other route. The
    wrap/port agents only READ it (the build descriptor's `build_commands` +
    feature wiring); it is written before any wave — so it satisfies the same
    read-only-across-a-wave contract as `crates.json` below.

    ``.providers`` must be here for a subtle reason: the agent backends resolve
    the provider CLI's config home as ``Layout(repo_root).providers(cli)``, and an
    isolated agent's ``repo_root`` IS its worktree — while ``Layout.providers``
    **mkdirs** the path. Unlinked, every worktree therefore gets a freshly created
    EMPTY provider config instead of crustify's shared one, and the CLI runs
    against it with no error: a silent loss of provider settings, which is the
    worst available failure mode.

    ``cli-config.json`` is here for the same reason as ``build.json``: it is
    hand-authored, machine-local (absolute paths to the crustify and
    crustify-prim checkouts, and to their binaries), and therefore not committed
    — so HEAD cannot carry it into a worktree. Without the symlink an agent's
    ``Layout.repo_config`` resolves to a file that is not there, every skill
    path fails to resolve, and the whole set silently disappears from its system
    prompt as the literal "(no skills configured)" inside principles.md.

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
        # A lazily-created shared dir must be MATERIALIZED in the main checkout
        # before the link, not skipped for not existing yet. `.providers` is
        # created on demand by `Layout.providers()` (which mkdirs), so on the
        # first wave in a fresh tree it is absent here — the `src.exists()`
        # guard below then skips it, `Layout.providers()` mkdirs a REAL dir
        # inside the worktree, and the purge takes it with the worktree. That is
        # exactly the silent failure this docstring warns about, plus a worse
        # one: codex's session rollout lands in CODEX_HOME, so the run's cost
        # accounting is destroyed with the worktree ("no session rollout found;
        # this run is unaccounted"). Only dirs are seeded — the shared FILES
        # (crates.json / build.json / cli-config.json) must stay skipped when
        # absent, since an empty stand-in for one of those is worse than none.
        if not src.exists() and d in _SHARED_LAZY_DIRS:
            src.mkdir(parents=True, exist_ok=True)
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            _link_into(src, dst)


def _link_into(src: Path, dst: Path) -> None:
    """Symlink ``src`` at ``dst``, or — when ``dst`` already exists as a real
    directory — link ``src``'s missing children into it, recursively.

    The plain case is a whole shared dir that the worktree does not have. The
    recursive case exists because a shared dir may be PARTIALLY materialized by
    the checkout: `targets/` holds the tracked `<t>/scope-config.json` beside the
    gitignored `scope.json`, `deps-dag.json` and `logs/`, so `git worktree add`
    creates `targets/<t>/` and a whole-dir symlink would be skipped. Skipping it
    would send the wave's per-agent logs into the worktree, where the purge takes
    them — losing exactly the cost and wall records a run is measured by.

    A destination that already exists as a symlink or file is left alone: it is
    either a previous link or a tracked artifact that must win."""
    if not dst.exists():
        dst.symlink_to(src.resolve())
        return
    if dst.is_symlink() or not (dst.is_dir() and src.is_dir()):
        return
    for child in src.iterdir():
        _link_into(child, dst / child.name)
