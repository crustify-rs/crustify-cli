"""cli.py — `crustify-audit`.

    crustify-audit <workspace> unsafe [--json] [--name NAME ...]
    crustify-audit <workspace> ub     [--model M] [--billing B] [--effort E]
                                      [--timeout MIN]
                                      [--instruments I ...]

TWO VERBS, AND THE SPLIT IS THE POINT.

`unsafe` is deterministic: a rustc HIR/typeck pass over the crate's unsafe
surface. No LLM or network. Two runs over one tree agree exactly, so a diff
between them is a change in the crate.

`ub` is agentic: one agent reading the crate, hunting undefined behaviour
reachable from safe code and authoring the advisory itself.

Separate verbs rather than one command with a flag, because a caller must always
know whether the answer in front of them is reproducible. Hiding that behind a
flag makes "did an LLM decide this?" unanswerable from the command line.

There is no `report` verb. Writing the advisory is the agent's job, not a
formatter's -- a template the harness fills in would flatten exactly the
judgement the agent is there to exercise.

WHY A SEPARATE BINARY FROM crustify-cli. Both crustify binaries mandate
``<repo_root> <target>`` and refuse to run without a ``crustify/`` directory.
This tool's most valuable use is auditing a crate that has never heard of
crustify. That is a different CLI contract, not a new subcommand.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from crustify_audit.layout import Layout


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crustify-audit",
        description="Find soundness bugs and safety trade-offs in Rust that wraps C. "
                    "`unsafe` is deterministic; `ub` drives one LLM agent over "
                    "its output.")
    p.add_argument("workspace", metavar="REPO",
                   help="Path to the REPOSITORY to audit. The root is the "
                        "subject; the agent locates the Rust within it. The "
                        "repo, not the crate, because auditing an FFI wrapper "
                        "means building the C library beside it.")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser(
        "unsafe",
        help="DETERMINISTIC unsafe metrics. No LLM.",
        description="Measure the crate's unsafe surface with a rustc driver "
                    "over HIR and typeck. This is the canonical deterministic "
                    "Crustify safety pass. "
                    "Reproducible: same tree, same bytes. Needs the crate to "
                    "compile; when it does not, there are no counts rather than "
                    "approximate ones. It says HOW MUCH unsafety is there, never "
                    "which of it is wrong.")
    m.add_argument("--json", action="store_true",
                   help="Print the document instead of a summary.")
    m.add_argument(
        "--name", nargs="+", action="extend", default=None, metavar="NAME",
        help="Report raw-pointer, dereference, manual Deref/DerefMut, and "
             "wrapper-slice sites for these C type or symbol names in each "
             "workspace crate. Raw-pointer matching does not require a wrapper.")

    h = sub.add_parser(
        "ub",
        help="AGENTIC hunt for undefined behaviour. Costs money.",
        description="Drive ONE agent hunting UB reachable from safe code. It "
                    "reads the crate itself to find what is worth looking at, "
                    "and can run `unsafe` for the numbers. The agent builds its own "
                    "reproductions, checks them with the selected instruments, and writes the "
                    "advisory itself — the harness supplies a workspace and a "
                    "scratch directory and nothing else. It never writes to the "
                    "audited workspace outside its `crustify/audit/` directory. It "
                    "writes one file per lead investigated into crustify/audit/leads/ "
                    "and one advisory per CONFIRMED bug into "
                    "crustify/audit/advisories/ — an advisory means something actually "
                    "crashed. Runs ACCUMULATE: there is no skip, and the agent "
                    "reads what earlier runs left before starting, so a second "
                    "run extends the record instead of re-deriving it.")
    h.add_argument("--objective",
                   choices=("audit", "audit+patch", "patch", "revisit"),
                   default="audit",
                   help="What this run is for. `audit` (default) hunts and "
                        "writes advisories and leads, and touches nothing else "
                        "in the target — which is what makes several agents "
                        "safe to run against one checkout at the same time. "
                        "`audit+patch` also repairs what it confirms; `patch` "
                        "skips the hunt and repairs advisories that are already "
                        "there. Both patching objectives develop in a git "
                        "worktree, so they still do not disturb the checkout "
                        "other agents are reading. `revisit` hunts nothing new: "
                        "it re-investigates the leads named in `--workset`, "
                        "which is what you want after adding an instrument that "
                        "can settle a hypothesis an earlier run had to leave "
                        "open.")
    h.add_argument("--workset", nargs="+", action="extend", default=None,
                   metavar="PATH",
                   help="Confine this agent to these files, so that agents run "
                        "in parallel against one target divide the crate "
                        "instead of duplicating each other. Paths relative to "
                        "the repo. Omitted, the whole crate is in scope. Under "
                        "`--objective revisit` these are lead notes under "
                        "`crustify/audit/leads/` rather than source files, and "
                        "the division is over open questions rather than over "
                        "the crate; omitted, every unsettled lead is in scope.")
    from crustify_audit.agents.base import INSTRUMENTS
    h.add_argument("--instruments", nargs="+", choices=INSTRUMENTS,
                   default=None, metavar="INSTRUMENT",
                   help="Constrain the hunt and advisory evidence to one or "
                        "more of: miri (Rust UB), asan/ubsan (native memory "
                        "and language UB), bsan (Tree Borrows aliasing across "
                        "FFI), msan (uninitialized memory), tsan (data races). "
                        "The resolved prompt lists the exact bug classes. "
                        "msan and tsan each need their own build and cannot "
                        "share a binary with asan/ubsan. Omitted, all are "
                        "selected.")
    h.add_argument("--model", default=None, metavar="PROVIDER/MODEL",
                   help="e.g. anthropic/claude-opus-5, openai/gpt-5.6. The "
                        "provider prefix selects the backend and is mandatory.")
    h.add_argument("--billing", choices=("subscription", "api"),
                   default="subscription",
                   help="How the provider CLI authenticates. `subscription` "
                        "uses the CLI's own logged-in account; `api` uses a key "
                        "from the environment. Not cosmetic and not switchable "
                        "by env var alone: claude keeps sending its stored OAuth "
                        "token unless `--bare`, and codex reads auth.json and "
                        "ignores API keys unless OpenAI or OpenRouter is declared "
                        "with an env_key. A missing provider key fails at launch; "
                        "OpenRouter supports only `api` billing.")
    h.add_argument("--effort",
                   choices=("low", "medium", "high", "xhigh", "max", "ultra"),
                   default=os.environ.get("CRUSTIFY_EFFORT") or "high",
                   metavar="LEVEL",
                   help="Codex reasoning effort for this auditor (default: "
                        "CRUSTIFY_EFFORT, or high). Accepted but ignored by "
                        "the Claude backend.")
    h.add_argument("--timeout", type=int, default=30, metavar="MINUTES",
                   dest="timeout",
                   help="Wall-clock BUDGET for the run (default 30, 0 runs one "
                        "agent). Agents are spawned one after another until the "
                        "budget is reached, and are never killed: each finishes "
                        "on its own, so the run OVERSHOOTS by however long the "
                        "last one takes. Each agent reads what the previous one "
                        "wrote, so the record accumulates within the run as it "
                        "does across runs. This is the only cap that works under "
                        "subscription auth — `--max-budget-usd` meters API-call "
                        "spend, of which there is none, and the CLI has no turn "
                        "limit.")
    return p


def _cmd_unsafe(layout: Layout, args) -> int:
    from crustify_audit import unsafe_scan as M
    path = M.write(layout, names=args.name)
    doc = json.loads(path.read_text())
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print(f"[crustify-audit] unsafe -> {path}\n")
        print(M.summarize(doc))
        print(f"\n  Counts, not judgement. Next: crustify-audit "
              f"{layout.repo} ub")
    return 0


def _cmd_ub(layout: Layout, args) -> int:
    from crustify_audit.agents.base import AuditAgent

    agent = AuditAgent(layout, model=args.model,
                       timeout_s=(args.timeout * 60) or None,
                       billing=args.billing,
                       effort=args.effort,
                       objective=args.objective,
                       workset=args.workset,
                       instruments=args.instruments)

    print("[crustify-audit] resolved instrument and bug-class scope:\n")
    print(agent._instruments_text())
    # There is no skip: runs accumulate. Say what is already on disk so the
    # caller knows this run is adding to a record rather than starting one --
    # and knows it is about to spend either way.
    was = agent.counts()
    if any(was):
        print(f"[crustify-audit] existing record: {was[0]} advisor"
              f"{'y' if was[0] == 1 else 'ies'}, {was[1]} lead note(s). "
              f"The agent reads these before it starts.")

    now = agent.run()
    new_adv, new_leads = now[0] - was[0], now[1] - was[1]

    print(f"[crustify-audit] advisories : {now[0]} ({new_adv:+d})  {layout.advisories}")
    print(f"[crustify-audit] leads      : {now[1]} ({new_leads:+d})  {layout.leads}")
    print(f"[crustify-audit] repros     : {layout.scratch}")
    if now[0] == 0:
        print("\n  No advisories: nothing was demonstrated. That is a result, "
              "not a failure —\n  read the leads to see what was judged.")
    else:
        print("\n  Triage each advisory: run its reproduction yourself, then "
              "check it matches\n  the source line it cites. Both hold -> real.")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"error: workspace does not exist: {ws}", file=sys.stderr)
        raise SystemExit(2)
    layout = Layout(ws)
    fn = {"unsafe": _cmd_unsafe, "ub": _cmd_ub}
    raise SystemExit(fn[args.command](layout, args))


if __name__ == "__main__":
    main()
