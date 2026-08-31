"""base.py — the single audit agent.

WHAT THIS BORROWS FROM crustify-cli, AND WHAT IT DELIBERATELY DOES NOT.

Kept, because they earned their place there and the reasons carry over:

  * the ``Backend`` protocol and the one call site that drives it. Running the
    provider CLI out-of-process, one subprocess per agent, is what makes usage
    accounting exact — the provider reports for that invocation and nothing
    else.
  * ``<provider>/<model>`` routing, where the provider selects the backend. A
    bare model id is ambiguous across services that price differently.
  * the system-preamble seam, so the same text reaches claude's append slot and
    codex's replace slot without diverging.
  * an on-disk artifact as the done signal, so a re-run is a no-op rather than
    a second bill.

Dropped, because crustify-audit is one agent over one workspace:

  * the stage/tier/output class hierarchy. There is one role, so there is one
    class and no ``SKILLS`` tuple to vary.
  * worktree isolation. The agent starts in the audited crate. Investigation
    artifacts stay under the audit root; once a finding is confirmed, the agent
    creates a target branch before making the source and regression-test patch.
    A worktree would buy real containment and cost a checkout per run; the trade
    is deliberate and the prompt carries the rule.
  * the DAG, the scope sets, the wave scheduler. There is no ordering problem:
    the agent decides what to look at.

WHERE THE LINE SITS. The harness hands over a workspace and a
writable directory, and starts the agent. Everything after that — what to
investigate, how to reduce it, what a reproduction looks like, how to structure
the advisory — is the agent's job. An earlier cut of this file pre-built a repro
crate and specified a findings JSON schema field by field; both were the harness
doing work it has no business doing, and a schema is a poor substitute for
telling an author what makes a report land. The prompt says what good looks
like; it does not hand over a form.
"""
from __future__ import annotations

import time

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from crustify_audit.agentlog import AgentLog, open_agent_log
from crustify_audit.layout import Layout

#: An agent ending faster than this did not hunt; it failed. Two in a row stop
#: the loop instead of spending the rest of the budget on the same failure.
_MIN_AGENT_SECONDS = 60

_PKG_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class InstrumentSpec:
    """One selectable verifier and the exact bug scope it gives an auditor."""

    label: str
    bug_classes: tuple[str, ...]
    reach: str


# This is the source of truth for both the CLI choices and the auditor prompt.
# Keep the classes phrased as things an instrument can actually demonstrate,
# not a general unsafe-Rust taxonomy: selecting an instrument constrains which
# candidates the auditor is allowed to spend its hunt on.
INSTRUMENT_SPECS = {
    "miri": InstrumentSpec(
        label="Miri",
        bug_classes=(
            "Rust-side out-of-bounds access, use-after-free, and invalid pointer use",
            "reads of uninitialized data and invalid values such as bad enum discriminants",
            "misaligned accesses and violated intrinsic preconditions",
            "Rust reference aliasing violations under Stacked Borrows and Tree Borrows",
            "data races in Rust code",
        ),
        reach=(
            "Code Miri can interpret. Most foreign calls are unsupported, so do not "
            "claim that a Rust-only model demonstrates behavior across the C boundary."
        ),
    ),
    "asan/ubsan": InstrumentSpec(
        label="AddressSanitizer + UndefinedBehaviorSanitizer (ASan/UBSan)",
        bug_classes=(
            "heap, stack, or global out-of-bounds access and buffer overflow",
            "use-after-free, use-after-return/scope, double-free, and invalid free",
            "null or misaligned pointer dereference and invalid object-size or pointer arithmetic",
            "signed integer overflow, division overflow, divide by zero, and invalid shifts",
            "invalid C/C++ enum, bool, and related runtime values covered by UBSan",
        ),
        reach=(
            "Executed native code only. Rebuild the relevant Rust and C/C++ code with "
            "the sanitizers and link their runtimes into the final reproducer."
        ),
    ),
    "bsan": InstrumentSpec(
        label="BorrowSanitizer (BSan)",
        bug_classes=(
            "Tree Borrows aliasing violations across Rust and foreign code",
            "writes through raw or foreign pointers that conflict with live Rust references",
            "access through pointers invalidated by Rust reborrows or exclusivity changes",
        ),
        reach=(
            "Executed Rust and LLVM-supported foreign code. It checks Rust aliasing "
            "rules; it is not a replacement for general memory-error sanitizers."
        ),
    ),
    "msan": InstrumentSpec(
        label="MemorySanitizer (MSan)",
        bug_classes=(
            "use of uninitialized memory in a branch, an address computation, or a syscall argument",
            "uninitialized bytes crossing the Rust/foreign boundary in either direction",
            "reads of struct tails, padding, or buffers a foreign initializer left partly unwritten",
            "typed reads of `MaybeUninit` storage that foreign code did not fully initialize",
        ),
        reach=(
            "Executed native code in which EVERY component is instrumented: the Rust "
            "standard library via `-Zbuild-std`, the crate, and the C/C++ it links. "
            "Memory written by uninstrumented code reads as uninitialized, so a "
            "partial build produces false positives rather than a clean run: rebuild "
            "the foreign code from source and disable its hand-written assembly. "
            "`-Zsanitizer=memory` already links the Rust MSan runtime -- do not also "
            "pass `-fsanitize=memory` to the linker, which duplicates it and fails "
            "the link on __ubsan symbols. It reports USES of uninitialized memory, "
            "not copies, so a value that is copied but never read will not be "
            "reported. Cannot share a binary with ASan or TSan; build it separately."
        ),
    ),
    "tsan": InstrumentSpec(
        label="ThreadSanitizer (TSan)",
        bug_classes=(
            "data races between Rust threads, foreign threads, or the two mixed",
            "unsynchronized access through `&T` where `Send`/`Sync` is asserted by hand",
            "races on foreign library state shared across threads, including refcounts and caches",
            "destruction of an object while another thread still uses it",
        ),
        reach=(
            "Executed Rust and C/C++ built with thread instrumentation. It reports "
            "only races on interleavings that actually run, so a clean result means "
            "the schedules exercised were clean, not that the type is `Sync`. Cannot "
            "share a binary with ASan or MSan; build it separately."
        ),
    ),
}

INSTRUMENTS = tuple(INSTRUMENT_SPECS)


class AuditAgent:
    """One UB hunt over one workspace.

    The agent is handed the workspace path and nothing else; it derives the
    artifact root from it and runs with `tmp/` as its working directory. It
    leaves one lead note per candidate it investigated and one advisory per bug
    it actually crashed. It edits target source only on a new branch and only
    to remediate a confirmed finding.

    Runs ACCUMULATE. The agent reads what earlier runs left in ``crustify/audit/advisories/``
    and ``crustify/audit/leads/`` before starting, so a second run extends the record instead
    of re-deriving it.
    """

    name = "crustify-audit"
    stage = "ub"
    #: Default model. Overridden by ``--model``; the provider prefix selects
    #: the backend, so this is the only place a default is stated.
    model = "anthropic/claude-opus-5"

    def __init__(
        self,
        layout: Layout,
        *,
        model: str | None = None,
        timeout_s: int | None = None,
        billing: str = "subscription",
        effort: str = "high",
        objective: str = "audit",
        workset: "Sequence[str] | None" = None,
        instruments: "Sequence[str] | None" = None,
        tag: str | None = None,
    ) -> None:
        self.layout = layout
        self.model = model or self.model
        self.timeout_s = timeout_s
        #: `audit` | `audit+patch` | `patch` | `revisit`. Only the patching
        #: objectives may touch target source, and only inside a worktree --
        #: which is what keeps concurrent agents off each other's checkout.
        self.objective = objective
        #: Files this agent confines its hunt to. Empty means the whole crate,
        #: which is the single-agent case. A workset is what makes several
        #: agents on one target additive rather than duplicative. Under
        #: `revisit` these are LEAD notes, not source files: the unit of work
        #: is a question someone already asked, so the division is over
        #: questions rather than over the crate.
        self.workset = tuple(workset or ())
        self.instruments = tuple(dict.fromkeys(instruments or INSTRUMENTS))
        unknown = set(self.instruments) - set(INSTRUMENTS)
        if unknown:
            raise ValueError(f"unknown instruments: {', '.join(sorted(unknown))}")
        #: Distinguishes concurrent agents in the log directory.
        self.tag = tag
        #: `subscription` | `api` — see the backends, which is where it changes
        #: the argv rather than the environment.
        self.billing = billing
        #: Codex reasoning effort. Claude has no corresponding CLI setting.
        self.effort = effort

    # ------------------------------------------------------------------ run

    def run(self) -> tuple[int, int]:
        """Drive the hunt until the deadline. Returns (advisories, leads).

        THERE IS NO DONE SIGNAL AND NO SKIP, deliberately. Runs accumulate:
        each reads what earlier ones left in `crustify/audit/advisories/` and `crustify/audit/leads/` and adds
        to them. Skipping when an artifact exists would make the second run --
        the one that builds on the first -- impossible. The cost is that `ub`
        always spends, so the CLI reports what is already there before starting.

        `timeout_s` IS A BUDGET, NOT A LEASH. An agent is never killed: each
        runs to its own completion, and only then does the loop ask whether the
        deadline has passed. So the wall clock OVERSHOOTS by however long the
        last agent takes, which is the price of never truncating a reduction
        half-written. Killing at a deadline throws away exactly the work that
        was closest to done.

        Respawning is the same accumulation the CLI does across invocations,
        moved inside one: agent N+1 reads what agent N wrote before it starts,
        so the second pass extends the record instead of re-deriving it.

        `timeout_s` of `None` runs ONE agent. A budget of nothing is not a
        licence to loop forever.
        """
        from crustify_audit.agents.backends import get_backend
        from crustify_audit.models import resolve as resolve_model

        route = resolve_model(self.model)
        backend = get_backend(route.backend)
        started = time.monotonic()
        deadline = started + self.timeout_s if self.timeout_s else None
        spawned, short = 0, 0
        while True:
            was, t0 = self.counts(), time.monotonic()
            with open_agent_log(self.layout.logs, self.stage, self.tag) as log:
                backend.run(
                    name=self.name,
                    model=route.model,
                    provider=route.provider,
                    prompt_template=self._prompt(),
                    arguments=self._arguments(),
                    system_preamble=self.system_preamble(),
                    # The workspace, because it is the only directory that is
                # certain to exist: the agent creates the artifact tree itself,
                # so nothing can start inside it. The prompt is what keeps
                # writes out of the checkout — see `system_preamble`.
                work_dir=str(self.layout.repo),
                    log=log,
                    billing=self.billing,
                    effort=self.effort,
                )
            spawned += 1
            took, now = time.monotonic() - t0, time.monotonic()
            adv, leads = self.counts()
            left = int(deadline - now) if deadline else 0
            print(f"[crustify-audit] agent {spawned} ended after "
                  f"{took / 60:.1f}m — advisories {adv} (+{adv - was[0]}), "
                  f"leads {leads} (+{leads - was[1]})"
                  + (f", {left // 60}m of budget left" if deadline else ""))
            if deadline is None or now >= deadline:
                break
            # An agent that dies on the way up would otherwise be respawned
            # until the budget is gone, paying each time for the same failure.
            # Two in a row that end almost immediately is that, not a hunt.
            short = short + 1 if took < _MIN_AGENT_SECONDS else 0
            if short >= 2:
                print(f"[crustify-audit] two agents ended within "
                      f"{_MIN_AGENT_SECONDS}s — stopping rather than "
                      f"respawning into the same failure.")
                break
        if deadline and spawned > 1:
            print(f"[crustify-audit] {spawned} agents over "
                  f"{(time.monotonic() - started) / 60:.1f}m "
                  f"(budget {self.timeout_s // 60}m)")
        return self.counts()

    def counts(self) -> tuple[int, int]:
        """(advisories, leads) on disk. The harness counts files; it does not
        parse them."""
        n = lambda d: len(list(d.glob("*.md"))) if d.is_dir() else 0
        return n(self.layout.advisories), n(self.layout.leads)

    # -------------------------------------------------------------- prompt

    def _prompt(self) -> str:
        return (_PKG_ROOT / "prompts" / "ub.md").read_text()

    def _arguments(self) -> dict:
        """The one thing the agent cannot work out for itself.

        Everything it was once handed besides this is DERIVABLE from the
        workspace — the artifact root is `<workspace>/crustify/audit`, the
        scratch dir sits under it — or better asked of the machine than read
        from an answer the harness cached before the agent started, which is
        what an instrument list is.

        Every injected fact is a place the prompt and the layout can drift
        apart, and one more thing for the agent to trust instead of check.
        """
        return {"workspace": str(self.layout.repo),
                "objective": self.objective,
                "workset": self._workset_text(),
                "instruments": self._instruments_text()}

    def _instruments_text(self) -> str:
        """Selected instruments, bug classes, and limits injected into the prompt."""
        sections = []
        for name in self.instruments:
            spec = INSTRUMENT_SPECS[name]
            classes = "\n".join(f"  - {bug_class}" for bug_class in spec.bug_classes)
            sections.append(
                f"### {spec.label} (`{name}`)\n\n"
                f"Hunt for these bug classes:\n\n{classes}\n\n"
                f"Reach and limitation: {spec.reach}"
            )
        return "\n\n".join(sections)

    def _workset_text(self) -> str:
        """The workset, or the sentence that says there isn't one.

        Under `revisit` the same flag carries lead notes rather than source
        files, so the phrasing has to change with it: a source workset bounds
        where a bug may live, a lead workset names the questions to settle.
        """
        if self.objective == "revisit":
            if not self.workset:
                return ("Every lead in `crustify/audit/leads/` that is not "
                        "already settled. Read them first and work the ones "
                        "still recorded as open or unproven.")
            listing = "\n".join(f"  {f}" for f in self.workset)
            return ("These leads, and only these:\n\n" + listing + "\n\n"
                    "Each names a hypothesis an earlier run could not settle. "
                    "Re-derive it from the current source rather than from the "
                    "note, and settle it or say precisely what is still "
                    "missing. Other agents are working the remaining leads at "
                    "the same time and share your `advisories/` and `leads/`.")
        if not self.workset:
            return ("The whole crate. Nothing in it is out of scope.")
        listing = "\n".join(f"  {f}" for f in self.workset)
        return ("These files, and only these:\n\n" + listing + "\n\n"
                "Read whatever you need to understand them — callers, callees, "
                "the C on the other side. But the bug you report must live in "
                "one of them. Other agents are working the rest of the crate "
                "at the same time and share your `advisories/` and `leads/`.")

    def system_preamble(self) -> str:
        """The role and the one hard rule.

        Short on purpose. Everything procedural lives in the prompt, which is
        editable without touching code; this is only what must hold whatever
        the agent is hunting.
        """
        return (
            "You audit Rust code that wraps C, looking for undefined behaviour "
            "reachable from safe code.\n\n"
            "A finding you cannot demonstrate is a hypothesis. Say which you "
            "are reporting.\n\n"
            "HARD RULE. Inside the audited checkout you may write ONLY under "
            "its `crustify/audit/` directory -- your leads, advisories, and "
            "scratch work belong there. Other agents are reading that same "
            "checkout while you work.\n\n"
            + ("Your objective is `audit`: you do not modify target source, "
               "tests, or build files at all."
               if self.objective == "audit" else
               "Your objective is `revisit`: you re-investigate leads someone "
               "else opened and you do not modify target source, tests, or "
               "build files at all."
               if self.objective == "revisit" else
               "Your objective is `" + self.objective + "`: target source, "
               "tests, and build files are edited ONLY inside a git worktree "
               "you create, never in the checkout itself.")
        )
