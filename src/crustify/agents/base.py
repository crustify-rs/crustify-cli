from __future__ import annotations

import json
from pathlib import Path

from crustify.agentlog import AgentLog, open_agent_log
from crustify.artifact_store import ArtifactStore
from crustify.layout import Layout

# Package root — used to locate prompts/ and templates/.
_PKG_ROOT = Path(__file__).parent.parent


def _skill_meta(path: Path) -> tuple[str, str, str | None]:
    """Parse ``name`` + ``description`` + optional ``bin`` from a SKILL.md YAML
    frontmatter block.

    Deliberately minimal — handles the fields crustify's SKILL.md format uses
    (scalar ``name:``, a folded/indented ``description:``, block scalar
    ``>-``/``>`` or inline, and a scalar ``bin:``) without taking a YAML
    dependency. The description's wrapped/indented continuation lines are
    collapsed to one line. ``bin`` is the logical name of the skill's CLI tool,
    resolved to an absolute path by the caller via the repo config's ``bins``
    map. Falls back to the file stem and an empty description when there is no
    frontmatter."""
    text = path.read_text()
    if not text.startswith("---"):
        return path.stem, "", None
    fm = text.split("---", 2)[1]
    name, desc, in_desc, binname = path.stem, [], False, None
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
        elif line.startswith("bin:"):
            binname = line.split(":", 1)[1].strip().strip("'\"") or None
        elif line.startswith("description:"):
            rest = line.split(":", 1)[1].strip().lstrip(">|").lstrip("-").strip()
            if rest:
                desc.append(rest)
            in_desc = True
    return name, " ".join(" ".join(desc).split()), binname

def _resolve_repo_root(target: Path) -> Path:
    """The repo root for ``target``: the one pinned by the CLI
    (:func:`crustify.layout.set_repo_root`), else ``target`` itself.

    It does NOT walk ancestors looking for a ``crustify/`` marker — an earlier
    docstring here claimed it did. That matters because `Layout` mkdirs the
    artifact dirs it is asked for, so constructing an agent for a subdirectory
    target WITHOUT a pinned root silently creates `<target>/crustify/` and
    resolves every later path under it."""
    return Layout.discover(target).repo_root


class CrustifyAgent:
    """One pipeline stage, driven by a provider-CLI backend.

    Each agent runs against a *target* (the subdirectory crustify is
    scoped to) and additionally has access to the *repo_root* (the
    repository the target lives in, read from the target-tier
    ``scope-config.json``). The two coincide for whole-repository ports.

    The ``tier`` class attribute decides which ``.crustify/`` directory
    this agent's ``output`` artifact lives in:

      - ``tier = "target"`` (default) — `<target>/.crustify/<output>`.
        Used by every agent that produces subsystem-scoped output
        (types, symbols, …).
      - ``tier = "repo_root"`` — `<repo_root>/.crustify/<output>`. Used
        by agents whose artifact is project-wide and target-independent.

    Agent logs always go to the *target* tier
    (`<target>/.crustify/logs/<session>/`), regardless of ``tier``,
    because they're scoped to the invocation, not the repository.
    """

    name: str         # subclasses set this
    model: str        # subclasses set this
    stage: str        # label used in skip messages + log filename
    prompt_dir: str | None = None  # optional subdir under prompts/ (e.g. "wrapper");
                                    # the prompt file is prompts/<prompt_dir>/<stage>.md
    # The skills this agent is given, as (dep, path-under-dep) pairs resolved
    # through the repo config's `deps` map. Hardcoded rather than declared in
    # each SKILL.md or listed in cli-config.json: the set is a property of the
    # ROLE, and the role is the class. `crustify-orchestrator` is deliberately
    # absent — wave scheduling is not a worker agent's job, and a skill it can
    # never act on is context it pays for on every request.
    SKILLS: tuple[tuple[str, str], ...] = (
        ("crustify", "skills/crustify-oracle/SKILL.md"),
        ("crustify-prim", "SKILL.md"),
    )
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
        # worktree is actually in play) so every `crustify-cli <repo_root> …` the
        # prompt runs — and every artifact path (rust/, logs) — resolves to the
        # worktree, not the pinned main repo. Without it, parallel agents' scaffold
        # writes + commits leak into the shared main checkout. Default (None) keeps
        # the pinned-main behaviour for the in-place / non-isolated path.
        self.layout = Layout(repo_root) if repo_root is not None else Layout.discover(self.target)
        self.repo_root = self.layout.repo_root
        # Repo-relative target id (e.g. "ssl/statem", or "." for the repo
        # root) — the value the prompt passes as crustify's second positional.
        self.target_rel = self.layout.rel_target(self.target)
        # Target-tier store: crustify/targets/<rel>/ (logs, scope, config).
        self.target_store = ArtifactStore(self.layout.target_dir(self.target))
        # Repo-root-tier store: crustify/ (analysis, build.json).
        self.root_store = ArtifactStore(self.layout.root)
        # Convenience alias for the tier this agent's output belongs to.
        self.store = self.root_store if self.tier == "repo_root" else self.target_store

    def run(self) -> None:
        if self._is_done():
            print(f"[crustify] {self.stage}: output already on disk, skipping.")
            return

        prompt = self._prompt()
        from crustify import config as _cfg
        from crustify.agents.backends import get_backend
        from crustify.models import resolve as _resolve_model

        # The model selects the backend: a Claude model can only be driven
        # by the claude CLI, an OpenAI one only by codex.
        model = _cfg.MODEL_OVERRIDE or self.model
        backend = _resolve_model(model).backend

        with self._make_log() as log:
            get_backend(backend).run(
                name=self.name,
                model=model,
                prompt_template=prompt,
                arguments=self._arguments(),
                system_preamble=self.system_preamble(),
                work_dir=str(getattr(self, "_work_dir", None) or self.target),
                log=log,
            )

    def _log_stem(self) -> str:
        """Filename stem for this agent's logs, unique within a session.

        ``stage_suffix`` disambiguates concurrent agents of the same stage
        under ``--parallel`` so they never clobber each other's files.
        """
        stem = self.stage.replace("/", "_").replace(" ", "_")
        if self.stage_suffix:
            safe_suffix = (
                self.stage_suffix
                .replace("/", "_")
                .replace(" ", "_")
                .replace(".", "_")
            )
            stem = f"{stem}__{safe_suffix}"
        return stem

    def _make_log(self) -> AgentLog:
        """Open this agent's output sinks (see :mod:`crustify.agentlog`).

        Logs are always written under the *target* tier so they stay
        co-located with the invocation that produced them.
        """
        from crustify import config as crustify_config

        return open_agent_log(
            self.target_store.root / "logs" / crustify_config.SESSION_ID,
            self._log_stem(),
        )

    def _is_done(self) -> bool:
        """Check whether this agent's output artifact already exists.

        When ``output`` is ``None`` the agent has no single on-disk
        artifact to check and always returns ``False``; the agent runs
        every time and is responsible for its own per-entry skip logic
        (e.g. wrapper agents walk their manifests and skip already-
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
        # No substitution beyond the caller's `.format(**arguments)`. The
        # `<!-- PRINCIPLES -->` and `<!-- SKILLS -->` markers a stage prompt
        # carries are inert: they record where each part of the system preamble
        # sits relative to the task, and are deliberately NOT sentinels — a
        # marker that silently expanded would put the text back in the user
        # turn, which is the compaction path this design exists to leave.
        return prompt_file.read_text()

    def _arguments(self) -> dict:
        # `target` is the repo-RELATIVE id and `repo_root` the full path —
        # together the two positionals every `crustify-cli <repo_root> <target> …`
        # invocation in a prompt needs. `git_base` is the wave's base worktree,
        # the branch an isolated agent lands its own commit on (empty outside a
        # wave). Supplied to EVERY agent: `str.format` ignores a key the template
        # does not reference, and a template referencing a key nobody supplies
        # dies with KeyError before the agent issues a request.
        # Subclasses extend (super()._arguments()).
        from crustify import config as _cfg
        return {"target": self.target_rel, "repo_root": str(self.repo_root),
                "git_base": _cfg.SESSION_BASE}

    def _template(self, name: str) -> str:
        return (_PKG_ROOT.parent / "templates" / name).read_text()

    def _repo_config(self) -> dict:
        """Repo-wide crustify config (``crustify/cli-config.json``): dep paths and
        the SKILL.md set. Memoised; empty dict if the file is absent."""
        cfg = getattr(self, "_repo_cfg_cache", None)
        if cfg is None:
            p = self.layout.repo_config
            cfg = json.loads(p.read_text()) if p.exists() else {}
            self._repo_cfg_cache = cfg
        return cfg

    def _dep(self, name: str, fallback: Path | None = None) -> Path | None:
        """Resolve an absolute dependency path declared under ``deps`` in the
        repo config (e.g. ``crustify-prim``), else ``fallback``."""
        raw = self._repo_config().get("deps", {}).get(name)
        return Path(raw) if raw else fallback

    def _render_skills(self) -> str:
        """Render this agent's :attr:`SKILLS` set as a metadata index
        (name — description + on-disk path), as its own section of the system
        preamble — a sibling of principles.md, not a section inside it.

        Mirrors a skill-aware harness's tier-1 load: the metadata rides in the
        system prompt unconditionally (the routing signal), while the body is
        read on demand from the path. That indirection is what makes this work
        on codex, which has no skill mechanism of its own — the index is prose
        naming a file the agent opens with the tool it already has.

        Descriptions are single-sourced from each SKILL.md's frontmatter, so
        they never drift from the skill itself. The framing sentence is emitted
        here rather than kept in principles.md: it is about how to read the
        index, so it belongs to the index, and principles.md stays principles."""
        bins = self._repo_config().get("bins", {})
        # `crustify` resolves without config in a source checkout; an
        # out-of-tree dep (crustify-prim) has no meaningful fallback and is
        # skipped when unconfigured rather than guessed at.
        fallback = {"crustify": _PKG_ROOT.parent.parent}
        blocks = []
        for dep, rel in self.SKILLS:
            root = self._dep(dep, fallback.get(dep))
            if root is None:
                continue
            p = root / rel
            if not p.exists():
                continue
            name, desc, binname = _skill_meta(p)
            block = f"- {name} — {desc}\n  read in full: {p}"
            # A skill that declares a `bin:` also advertises that tool's
            # absolute path (from the repo config's `bins` map) — so the agent
            # invokes it directly rather than relying on PATH, and discovers its
            # flags from the tool's own `--help`. Same rail as the SKILL.md path.
            binpath = bins.get(binname) if binname else None
            if binpath:
                block += f"\n  binary: {binpath}"
            blocks.append(block)
        body = "\n".join(blocks) if blocks else "(no skills configured)"
        return (
            "## Skills\n\n"
            "Reusable how-to guides for recurring decisions. If a skill's "
            "`description` below matches what you're doing, **read that skill's "
            "file in full** before proceeding - the description is the routing "
            "signal; the body is the procedure.\n\n"
            f"{body}"
        )

    def _principles_md(self) -> Path:
        """The always-on principles doc, packaged next to the stage prompts.

        In `prompts/` rather than `docs/` because that is what it is: a prompt
        fragment. Neither provider CLI loads it from a canonical path — claude
        reads `CLAUDE.md`, codex reads a repo-root `AGENTS.md`, and this file
        was at neither — so the old name advertised a loading mechanism that
        never existed."""
        return _PKG_ROOT / "prompts" / "principles.md"

    def _render_principles(self) -> str:
        """principles.md verbatim. Empty string if the doc is absent.

        No substitution: the skill index used to be spliced into a sentinel in
        here, which made a principles doc that was partly not principles. The
        two are concatenated in :meth:`system_preamble` instead."""
        p = self._principles_md()
        return p.read_text() if p.exists() else ""

    def system_preamble(self) -> str:
        """What the backend puts in its system slot, above the stage prompt.

        The principles doc and the skill index go here rather than into the
        stage prompt for one reason: the system prompt is not part of
        ``messages``, so nothing a long agent run does can summarize it away.
        A 100+ turn agent that has had its file contract compacted into a
        paraphrase is the failure this prevents, and it is silent when it
        happens — the agent keeps working, just against a lossy copy of the
        rules.

        It is also byte-identical across every agent of a wave, which makes it
        one shared cacheable prefix rather than N. (Cache entries only become
        readable once the first response starts streaming, so a wave launched
        at full concurrency still pays N writes; staggering the first agent is
        what collects the reads.)

        Two independent documents, concatenated: principles.md is the same for
        every agent, the skill index varies with :attr:`SKILLS`. The
        `<!-- PRINCIPLES -->` and `<!-- SKILLS -->` markers in the stage prompts
        record where each one lands relative to the task; neither is a
        substitution point."""
        return "\n\n---\n\n".join(
            part for part in (self._render_principles().rstrip(),
                              self._render_skills()) if part)
