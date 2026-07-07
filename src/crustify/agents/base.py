from __future__ import annotations

import json
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


def _skill_meta(path: Path) -> tuple[str, str, set[str] | None]:
    """Parse ``name`` + ``description`` + ``roles`` from a SKILL.md YAML
    frontmatter block.

    Deliberately minimal — handles the fields crustify's SKILL.md format uses
    (scalar ``name:``, inline-list ``roles: [a, b]``, and a folded/indented
    ``description:``, block scalar ``>-``/``>`` or inline) without taking a
    YAML dependency. The description's wrapped/indented continuation lines are
    collapsed to one line. ``roles`` is ``None`` when the field is absent
    (treated as universal by the caller). Falls back to the file stem / empty
    description / no roles if there is no frontmatter."""
    text = path.read_text()
    if not text.startswith("---"):
        return path.stem, "", None
    fm = text.split("---", 2)[1]
    name, desc, in_desc, roles = path.stem, [], False, None
    for line in fm.splitlines():
        if in_desc:
            # A new top-level key (non-indented, contains ':') ends the block.
            if line[:1] not in (" ", "\t", "") and ":" in line:
                in_desc = False
            else:
                desc.append(line.strip())
                continue
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("roles:"):
            raw = line.split(":", 1)[1].strip().strip("[]")
            roles = {r.strip().strip("'\"") for r in raw.split(",") if r.strip()}
        elif line.startswith("description:"):
            rest = line.split(":", 1)[1].strip().lstrip(">|").lstrip("-").strip()
            if rest:
                desc.append(rest)
            in_desc = True
    return name, " ".join(" ".join(desc).split()), roles

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
    # Skill audience for the `{skills}` slot: only registered SKILL.md whose
    # frontmatter `roles` intersect this tuple are rendered into the prompt.
    # Translators (port / wrap / scaffold / …) default to the discovery +
    # primitive skills; orchestration skills (roles: [orchestrator]) are filtered
    # out. An orchestrator-role agent would override this.
    skill_roles: tuple[str, ...] = ("translator",)
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
        text = prompt_file.read_text()
        # A prompt may carry the role-scoped skill index inline via the shared
        # `<!-- SKILLS_INDEX -->` sentinel (the same convention AGENTS.md uses
        # through _render_principles) instead of the `{principles}` slot. Braces
        # in the rendered index are escaped so it survives the downstream
        # RelentlessAgent `prompt_template.format(**arguments)`.
        if "<!-- SKILLS_INDEX -->" in text:
            idx = self._render_skills().replace("{", "{{").replace("}", "}}")
            text = text.replace("<!-- SKILLS_INDEX -->", idx)
        return text

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

    def _repo_config(self) -> dict:
        """Repo-wide crustify config (``crustify/config.json``): dep paths and
        the SKILL.md set. Memoised; empty dict if the file is absent."""
        cfg = getattr(self, "_repo_cfg_cache", None)
        if cfg is None:
            p = self.layout.repo_config
            cfg = json.loads(p.read_text()) if p.exists() else {}
            self._repo_cfg_cache = cfg
        return cfg

    def _dep(self, name: str, fallback: Path | None = None) -> Path | None:
        """Resolve an absolute dependency path declared under ``deps`` in the
        repo config (e.g. ``crustify-crate``), else ``fallback``."""
        raw = self._repo_config().get("deps", {}).get(name)
        return Path(raw) if raw else fallback

    def _render_skills(self) -> str:
        """Render the configured ``skills`` SKILL.md set as a metadata index
        (name — description + on-disk path) for a ``{skills}`` prompt slot,
        scoped to this agent's :attr:`skill_roles`.

        Mirrors a skill-aware harness's tier-1 load: the metadata is injected
        into the prompt unconditionally (the routing signal), while the body is
        read on demand from the path. Single-sourced from each SKILL.md's
        frontmatter, so descriptions never drift from the skill itself. A skill
        whose frontmatter ``roles`` do not intersect this agent's roles is
        skipped (an untagged skill is universal)."""
        mine = set(self.skill_roles)
        blocks = []
        for raw in self._repo_config().get("skills", []):
            p = Path(raw)
            if not p.exists():
                continue
            name, desc, roles = _skill_meta(p)
            if roles is not None and not (roles & mine):
                continue  # scoped to roles this agent does not have
            blocks.append(f"- {name} — {desc}\n  read in full: {p}")
        return "\n".join(blocks) if blocks else "(no skills configured)"

    def _agents_md(self) -> Path:
        """The always-on principles doc (`AGENTS.md`), resolved from the
        `crustify` dep root (`docs/AGENTS.md`) with the in-tree layout as
        fallback."""
        return self._dep("crustify", _PKG_ROOT.parent.parent) / "docs" / "AGENTS.md"

    def _render_principles(self) -> str:
        """The always-on principles preamble for the `{principles}` prompt slot:
        AGENTS.md verbatim, with its ``<!-- SKILLS_INDEX -->`` sentinel replaced
        by this agent's role-scoped skill index. Substituted as a `.format`
        *value*, so its (single) braces are inserted literally, never re-parsed.
        Empty string if AGENTS.md is absent."""
        p = self._agents_md()
        if not p.exists():
            return ""
        return p.read_text().replace("<!-- SKILLS_INDEX -->", self._render_skills())
