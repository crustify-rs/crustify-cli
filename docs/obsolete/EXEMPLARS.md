# Exemplars

Running log of **good calls** the LLM agents made on real runs — the positive
mirror of [PITFALLS.md](PITFALLS.md). Where Pitfalls records mistakes so the
next run doesn't repeat them, Exemplars records non-obvious *correct* judgment
calls so the next run reliably *reaches for them*, and so a reviewer can
recognise the signal of a good port.

Maintained for three reasons:

1. **Prompt / skill authoring.** When an agent resists an easy-but-wrong path
   and does the harder-but-right thing, the principle behind it is worth
   encoding (in AGENTS.md, a skill, or DISCIPLINE.md) so it becomes the default,
   not a lucky one-off.
2. **Manifest / port audit.** A reviewer can use these as positive markers
   (*"the ported ctor keeps an eager `*_dispose` next to a RAII-owned value —
   is a later branch reading that value's state? §2026-06-30 says that's
   load-bearing, not redundant"*).
3. **Cross-agent learning.** A good move discovered for one agent often
   generalises to siblings (a faithful-teardown insight in `CrustifyPort`
   applies to `CrustifyTypeWrapper` too).

Entry conventions (the positive mirror of the Pitfalls fields):

- Date the call surfaced (ISO date), used as the entry anchor.
- Agent name.
- Brief title.
- **What it did** — the exemplary decision observed in the output.
- **Why it's right** — the principle that makes it correct.
- **The lure it resisted** — the easy/wrong path most would take (so we can see
  what the good call avoided).
- **Reinforce** — where to encode the principle so future runs default to it.
- **Recognise** — a cheap signal a reviewer can check to confirm the pattern
  was applied (the mirror of Pitfalls' *Self-check*).

---

## 2026-06-30 — CrustifyPort — `indexer_new`: treated `git_str_dispose` as an overloaded *free + control-flow signal*, not just teardown; kept the eager call at the C site with `CVal` RAII as backstop, and inlined per-exit cleanup to constant-fold C's runtime guard

**Context.** Porting `indexer_new` (a ctor) to the uninit ladder
(`CBoxUninit<GitIndexer>` → `assume_init` → `CBox`). The C body has two local
`git_str`s (`path`, `tmp_path`), disposed eagerly mid-function and again in a
shared `cleanup:` label.

**What it did.** It wrapped the locals as `CVal<GitStr>` (RAII disposal) **and
still** emitted the explicit `ffi::git_str_dispose(tmp_path.as_ptr())` at the
exact C call site (right after `git_packfile_alloc`, before the error check).
It then **inlined** the C `cleanup:` epilogue at each error-exit instead of a
shared label, and **pruned** the `tmp_path` `p_unlink` at the post-dispose
exits — keeping it only at the mktmp-failure exit.

**Why it's right.** `git_str_dispose` is overloaded: it frees the buffer **and
zeroes the str's length**, and C's cleanup branches on
`if (git_str_len(&tmp_path) > 0) p_unlink(...)` to decide whether to delete the
temp file. So the dispose is a *state signal in the cleanup state machine*, not
pure teardown. RAII (`CVal::drop`) releases the buffer but fires at **scope
end** — too late to drive a *mid-function* guard. Keeping the eager dispose
preserves "len → 0 *now*, so a later failure must NOT unlink the file the
packfile already owns"; `CVal`'s idempotent `Drop` then backstops every
early-return path (`git_str_dispose` is idempotent, so the double call is a
no-op). Inlining cleanup per-exit let it **constant-fold** C's runtime
`git_str_len` guard: live at the mktmp-fail exit (`len` may be > 0), provably
dead at the post-dispose exits (`len == 0`) — same observable behaviour,
specialised per site, no dead branches. It also faithfully reproduced C's
partial-init leak (storage-only `git__free` of the header; the formed packfile
leaks) rather than silently "fixing" it.

**The lure it resisted.** *"It's a `CVal` — RAII handles disposal, so drop the
manual `git_str_dispose`."* That deletes the signal: the mid-function
`git_str_len` checks would then read a still-nonzero length and **wrongly
`p_unlink` the temp file the packfile now owns** — a real behavioural divergence
(and a latent double-unlink). The deeper lure is assuming a C `*_dispose`/
`*_free` is *only* resource release.

**Reinforce.** AGENTS.md *faithful-port* rule: a C teardown call may be
overloaded as **resource-release + a control-flow signal**; RAII covers the
release, but the signal half must stay at the original call site. (Sibling of
the `z_stream` / by-value-`CVal` move pitfall — both are "RAII timing ≠ C
timing" cases, opposite outcomes.)

**Recognise.** When a ported function keeps an explicit `*_dispose`/`*_free` on
a value that is *also* RAII-owned, check whether a later branch reads that
value's state (length / flag / null). If yes → the eager call is load-bearing,
keep it. If no downstream state-read → the eager call is redundant and RAII
alone suffices (the agent made exactly this distinction: kept it for `tmp_path`,
flagged `path`'s as non-load-bearing).
