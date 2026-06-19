from __future__ import annotations

import threading
from pathlib import Path
from typing import IO

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.print_to_console import ConsolePrinter
from kiss.core.printer import MultiPrinter, Printer
from kiss.core.relentless_agent import RelentlessAgent

from crustify.artifact_store import ArtifactStore
from crustify.layout import Layout

# Package root — used to locate prompts/ and templates/.
_PKG_ROOT = Path(__file__).parent.parent

# Appended to a wrap/port agent's prompt during a worktree-isolated parallel
# wave: the agent owns committing its own work in its private worktree (the
# orchestrator only safety-nets anything left uncommitted).
_ISOLATED_COMMIT_FOOTER = """

---

## Finalize — commit your work (isolated worktree)

You are running inside your **own git worktree**, isolated from the other
parallel agents. Your `cargo check` / `clippy` therefore see only your own work
on top of the shared base — trust them. When your codegen is complete **and your
scoped checks pass**, stage and commit everything you wrote, here, before you
finish:

```
git add -A
git commit -m "crustify: <stage> <your unit(s)>"
```

Commit exactly once, at the end. Do not push, do not touch any other checkout.
"""


def _resolve_repo_root(target: Path) -> Path:
    """Repo root = the nearest ancestor of ``target`` containing
    ``crustify/`` (the repo marker). No config field needed."""
    return Layout.discover(target).repo_root


class CrustifyAgent:
    """Thin wrapper around RelentlessAgent for a single pipeline stage.

    Each agent runs against a *target* (the subdirectory crustify is
    scoped to) and additionally has access to the *repo_root* (the
    repository the target lives in, read from the target-tier
    ``config.json``). The two coincide for whole-repository ports.

    The ``tier`` class attribute decides which ``.crustify/`` directory
    this agent's ``output`` artifact lives in:

      - ``tier = "target"`` (default) — `<target>/.crustify/<output>`.
        Used by every agent that produces subsystem-scoped output
        (analyzer, port, type_wrapper, …).
      - ``tier = "repo_root"`` — `<repo_root>/.crustify/<output>`. Used
        by agents whose artifact is project-wide and target-independent
        (BuildPropose, BuildExecute).

    Agent logs always go to the *target* tier
    (`<target>/.crustify/logs/<session>/`), regardless of ``tier``,
    because they're scoped to the invocation, not the repository.
    """

    name: str         # subclasses set this
    model: str        # subclasses set this
    stage: str        # label used in skip messages + log filename
    prompt_dir: str | None = None  # optional subdir under prompts/ (e.g. "analyzer");
                                    # the prompt file is prompts/<prompt_dir>/<stage>.md
    output: str | None = None  # path under .crustify/; when set, artifact existence
                               # is the agent-level done signal (skip on re-run).
                               # When None the agent always runs — the orchestrator
                               # is responsible for gating invocation.
    tier: str = "target"       # "target" | "repo_root"; selects which tier owns
                               # this agent's output artifact.
    # Set per-instance (not class) when an agent is one of many running
    # in parallel — disambiguates log filenames so concurrent agents
    # don't clobber each other's logs. None on instances that don't
    # need disambiguation.
    stage_suffix: str | None = None

    def __init__(self, target: Path, *, repo_root: Path | None = None) -> None:
        self.target = target.resolve()
        # An isolated-wave agent passes its WORKTREE as `repo_root` (only when a
        # worktree is actually in play) so every `crustify <repo_root> …` the
        # prompt runs — and every artifact path (rust/, logs) — resolves to the
        # worktree, not the pinned main repo. Without it, parallel agents' scaffold
        # writes + commits leak into the shared main checkout. Default (None) keeps
        # the pinned-main behaviour for the in-place / non-isolated path.
        self.layout = Layout(repo_root) if repo_root is not None else Layout.discover(self.target)
        self.repo_root = self.layout.repo_root
        # Repo-relative target id (e.g. "ssl/statem", or "_root") — the value
        # the prompt passes as crustify's second positional.
        self.target_rel = self.layout.rel_target(self.target)
        # Target-tier store: crustify/targets/<rel>/ (logs, scope, kiss, …).
        self.target_store = ArtifactStore(self.layout.target_dir(self.target))
        # Repo-root-tier store: crustify/ (analysis, build.json, alloc.json).
        self.root_store = ArtifactStore(self.layout.root)
        # Convenience alias for the tier this agent's output belongs to.
        self.store = self.root_store if self.tier == "repo_root" else self.target_store

    def run(self) -> None:
        if self._is_done():
            print(f"[crustify] {self.stage}: output already on disk, skipping.")
            return

        prompt = self._prompt()
        from crustify import config as _cfg0
        if getattr(_cfg0, "ISOLATED_WAVE", False) and getattr(self, "_commits_own_work", True):
            prompt += _ISOLATED_COMMIT_FOOTER
        tools = self._tools()

        printer, log_fh = self._make_printer()

        try:
            from crustify import config as _cfg
            agent = RelentlessAgent(self.name)
            agent.run(
                model_name=_cfg.MODEL_OVERRIDE or self.model,
                prompt_template=prompt,
                arguments=self._arguments(),
                tools=tools,
                # Agents default to the target dir; a worktree-isolated agent
                # (e.g. CrustifyMerge) overrides this to its own worktree.
                work_dir=str(getattr(self, "_work_dir", None) or self.target),
                printer=printer,
                verbose=False,  # output is managed via the printer
            )
        finally:
            if log_fh is not None:
                log_fh.close()

    def _make_printer(self) -> tuple[Printer | None, IO[str] | None]:
        """Build a Printer based on runtime config flags.

        Returns ``(printer, log_file_handle)``.  The caller **must** close
        *log_file_handle* when it is no longer needed (typically in a
        ``finally`` block).

        Logs are always written under the *target* tier so they stay
        co-located with the invocation that produced them.
        """
        from crustify import config as crustify_config

        printers: list[Printer] = []
        log_fh: IO[str] | None = None

        if crustify_config.LOG_TO_CONSOLE:
            printers.append(ConsolePrinter())

        if crustify_config.LOG_TO_FILE:
            log_dir = self.target_store.root / "logs" / crustify_config.SESSION_ID
            log_dir.mkdir(parents=True, exist_ok=True)
            safe_name = self.stage.replace("/", "_").replace(" ", "_")
            if self.stage_suffix:
                safe_suffix = (
                    self.stage_suffix
                    .replace("/", "_")
                    .replace(" ", "_")
                    .replace(".", "_")
                )
                safe_name = f"{safe_name}__{safe_suffix}"
            log_fh = open(log_dir / f"{safe_name}.log", "w")  # noqa: SIM115
            printers.append(ConsolePrinter(file=log_fh))

        if len(printers) == 0:
            return None, log_fh
        if len(printers) == 1:
            return printers[0], log_fh
        return MultiPrinter(printers), log_fh

    def _is_done(self) -> bool:
        """Check whether this agent's output artifact already exists.

        When ``output`` is ``None`` the agent has no single on-disk
        artifact to check and always returns ``False``; the agent runs
        every time and is responsible for its own per-entry skip logic
        (e.g. analyzer agents walk their manifests and skip already-
        annotated entries). Stage completion is purely data-driven —
        there is no ``state.json``; the artifact's presence on disk
        IS the signal.
        """
        if self.output is not None:
            return self.store.artifact_exists(self.output)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prompt(self) -> str:
        base = _PKG_ROOT / "prompts"
        prompt_file = (
            base / self.prompt_dir / f"{self.stage}.md"
            if self.prompt_dir else base / f"{self.stage}.md"
        )
        return prompt_file.read_text()

    def _arguments(self) -> dict:
        # `target` is the repo-RELATIVE id and `repo_root` the full path —
        # together the two positionals every `crustify <repo_root> <target> …`
        # invocation in a prompt needs. Subclasses extend (super()._arguments()).
        return {"target": self.target_rel, "repo_root": str(self.repo_root)}

    def _tools(self) -> list:
        useful = UsefulTools(stop_event=threading.Event())
        return [useful.Bash, useful.Read, useful.Edit, useful.Write]

    def _template(self, name: str) -> str:
        return (_PKG_ROOT.parent / "templates" / name).read_text()
