from __future__ import annotations

import hashlib
import json
import re as _re
from dataclasses import dataclass
from pathlib import Path

from crustify.agentlog import AgentLog, open_agent_log
from crustify.artifact_store import ArtifactStore
from crustify.layout import Layout

# Package root — used to locate prompts/.
_PKG_ROOT = Path(__file__).parent.parent


#: Plain-markdown skill fields — `- Skill name:` / `- Bin path:` /
#: `- Doc path:` / `- Description:`, each continued by its indented wrap lines.
#: The shape a skill takes when its whole content IS its metadata, so there is
#: no body left to warrant frontmatter. `Doc path` is where the procedure DID
#: end up: relative to the skill file's own directory, so it survives whatever
#: absolute path the checkout sits at.
_MD_FIELD_RE = _re.compile(
    r"^-\s*(Skill name|Bin path|Doc path|Description)\s*:\s*(.*)$",
    _re.IGNORECASE)

# A local role header wraps guidance around a repository-owned generic skill.
# Keeping the anchor in the header makes that composition explicit in source.
_GENERIC_SKILL_ANCHOR = "<!-- SKILL -->"


@dataclass(frozen=True)
class SkillSpec:
    """A generic skill plus optional role-specific prompt guidance."""

    dep: str
    path: str
    capability: str | None = None
    role_header: str | None = None


def _skill_meta(path: Path) -> tuple[str, str, str | None, Path | None]:
    """Parse ``name`` + ``description`` + optional ``bin`` + optional ``doc``
    from a skill file.

    Two shapes, because crustify has two kinds of skill. One carries a real
    procedure in its body and declares its metadata in YAML frontmatter. The
    other IS its metadata, written as a plain markdown list — either because
    the procedure lives in a tool's ``--help``, or because it lives in a
    document the skill points at with ``Doc path``.

    Deliberately minimal — no YAML dependency. Frontmatter: scalar ``name:``, a
    folded/indented ``description:`` (block scalar ``>-``/``>`` or inline), and
    a scalar ``bin:``. Plain markdown: ``- Skill name:`` / ``- Bin path:`` /
    ``- Doc path:`` / ``- Description:``. Either way the description's wrapped
    continuation lines are collapsed to one line, ``bin`` is the LOGICAL tool
    name (resolved to an absolute path by the caller via the repo config's
    ``bins`` map), and ``doc`` is resolved here against the skill file's own
    directory."""
    text = path.read_text()
    if not text.startswith("---"):
        name, desc, binname, doc, cur = path.stem, [], None, None, None
        for line in text.splitlines():
            m = _MD_FIELD_RE.match(line)
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                cur = key
                if key == "skill name":
                    name = val
                elif key == "bin path":
                    binname = val or None
                elif key == "doc path":
                    doc = (path.parent / val).resolve() if val else None
                else:
                    desc.append(val)
                continue
            # An indented line continues the field above; anything else ends it.
            if cur == "description" and line[:1] in (" ", "\t") and line.strip():
                desc.append(line.strip())
            elif line.strip():
                cur = None
        return name, " ".join(" ".join(desc).split()), binname, doc
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
    return name, " ".join(" ".join(desc).split()), binname, None

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
    repository the target lives in). The two coincide for repo-root targets.

    The ``tier`` class attribute decides which ``.crustify/`` directory
    this agent's ``output`` artifact lives in:

      - ``tier = "target"`` (default) — `<target>/.crustify/<output>`.
        Used by every agent that produces subsystem-scoped output
        (types, symbols, …).
      - ``tier = "repo_root"`` — `<repo_root>/.crustify/<output>`. Used
        by agents whose artifact is project-wide and target-independent.

    Agent logs always go to the campaign tier
    (`crustify/campaigns/logs/<session>/`), regardless of ``tier``,
    because they're scoped to the invocation, not the repository.
    """

    name: str         # subclasses set this
    model: str        # subclasses set this
    stage: str        # label used in skip messages + log filename
    prompt_dir: str | None = None  # optional subdir under prompts/ (e.g. "wrapper");
                                    # the prompt file is prompts/<prompt_dir>/<stage>.md
    # Core skills are a property of the role. Subclasses may add optional
    # prompt-only capabilities selected from cli-config.json.
    SKILLS: tuple[SkillSpec, ...] = ()
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
        # worktree, not the pinned main repo. Without it, parallel agents' Rust-tree
        # writes and commits leak into the shared main checkout. Default (None) keeps
        # the pinned-main behaviour for the in-place / non-isolated path.
        self.layout = Layout(repo_root) if repo_root is not None else Layout.discover(self.target)
        self.repo_root = self.layout.repo_root
        # Repo-relative target id (e.g. "ssl/statem", or "." for the repo
        # root) — the value the prompt passes as crustify's second positional.
        self.target_rel = self.layout.rel_target(self.target)
        # Campaign store (tracked wave plans, logs and invocation-local artifacts).
        self.campaign_store = ArtifactStore(self.layout.campaigns)
        # Repo-root-tier store: crustify/ (analysis, build.json).
        self.root_store = ArtifactStore(self.layout.root)
        # Convenience alias for the tier this agent's output belongs to.
        self.store = self.root_store if self.tier == "repo_root" else self.campaign_store

    def run(self) -> None:
        if self._is_done():
            print(f"[crustify] {self.stage}: output already on disk, skipping.")
            return

        prompt = self._prompt()
        system_preamble = self.system_preamble()
        arguments = self._arguments()
        rendered_prompt = prompt.format(**arguments)
        from crustify import config as _cfg
        from crustify.agents.backends import get_backend
        from crustify.models import resolve as _resolve_model

        # The model selects the backend: a Claude model can only be driven
        # by the claude CLI, an OpenAI one only by codex.
        model = _cfg.MODEL_OVERRIDE or self.model
        backend = _resolve_model(model).backend

        with self._make_log() as log:
            caps = ", ".join(self.prompt_capabilities()) or "none"
            prompt_hash = hashlib.sha256(
                (system_preamble + "\0" + rendered_prompt).encode()
            ).hexdigest()
            log.line(f"[crustify] prompt capabilities: {caps}")
            log.line(f"[crustify] prompt hash: {prompt_hash}")
            get_backend(backend).run(
                name=self.name,
                model=model,
                prompt_template=prompt,
                arguments=arguments,
                system_preamble=system_preamble,
                work_dir=str(getattr(self, "_work_dir", None) or self.target),
                log=log,
            )

    def _log_stem(self) -> str:
        """Filename stem for this agent's logs, unique within a session.

        ``stage_suffix`` disambiguates concurrent agents of the same stage so
        they never clobber each other's files.
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

        Logs are always written under the campaign tier so every wave in the
        orchestrator campaign shares one session namespace.
        """
        from crustify import config as crustify_config

        return open_agent_log(
            self.campaign_store.root / "logs" / crustify_config.SESSION_ID,
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
        # `<!-- CONVENTIONS -->` and `<!-- SKILLS -->` markers a stage prompt
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

    def _repo_config(self) -> dict:
        """Repo-wide config: dependency paths, binaries and prompt capabilities.

        Memoised per agent; an absent file is an empty configuration.
        """
        cfg = getattr(self, "_repo_cfg_cache", None)
        if cfg is None:
            p = self.layout.repo_config
            cfg = json.loads(p.read_text()) if p.exists() else {}
            self._repo_cfg_cache = cfg
        return cfg

    def _dep(self, name: str, fallback: Path | None = None) -> Path | None:
        """Resolve an absolute dependency path declared under ``deps`` in the
        repo config (e.g. ``ffibox``), else ``fallback``."""
        raw = self._repo_config().get("deps", {}).get(name)
        return Path(raw) if raw else fallback

    def skill_specs(self) -> tuple[SkillSpec, ...]:
        """The generic skills and role overlays rendered for this agent."""
        return self.SKILLS

    def prompt_capabilities(self) -> tuple[str, ...]:
        """Optional capabilities selected for this prompt.

        This is descriptive, not an access-control boundary: omitted
        capabilities remain discoverable through the ordinary shell.
        """
        return tuple(
            spec.capability for spec in self.skill_specs()
            if spec.capability is not None
        )

    def _render_skills(self) -> str:
        """Render this agent's :attr:`SKILLS` set as a metadata index
        (name — description + on-disk path), as its own section of the system
        preamble — a sibling of conventions.md, not a section inside it.

        Mirrors a skill-aware harness's tier-1 load: the metadata rides in the
        system prompt unconditionally (the routing signal), while the body is
        read on demand from the path. That indirection is what makes this work
        on codex, which has no skill mechanism of its own — the index is prose
        naming a file the agent opens with the tool it already has.

        Descriptions are single-sourced from each skill's metadata, so
        they never drift from the skill itself. The framing sentence is emitted
        here rather than kept in conventions.md: it is about how to read the
        index, so it belongs to the index, and conventions.md stays
        conventions."""
        bins = self._repo_config().get("bins", {})
        # `crustify` resolves without config in a source checkout.
        # Out-of-tree capabilities have no meaningful fallback. When selected,
        # a missing path is an error rather than a silently different prompt.
        fallback = {"crustify": _PKG_ROOT.parent.parent}
        blocks = []
        for spec in self.skill_specs():
            root = self._dep(spec.dep, fallback.get(spec.dep))
            if root is None:
                raise SystemExit(
                    f"prompt capability {spec.capability or spec.path!r}: "
                    f"cli-config.json has no deps.{spec.dep} path")
            p = root / spec.path
            if not p.exists():
                raise SystemExit(
                    f"prompt capability {spec.capability or spec.path!r}: "
                    f"skill file does not exist: {p}")
            name, desc, binname, doc = _skill_meta(p)
            block = f"- {name} — {desc}"
            # What to open, if anything. A frontmatter skill carries its own
            # procedure, so the path is the skill file. A metadata-only one
            # points at wherever the procedure actually lives (`Doc path`), and
            # when it names nothing there is nothing to open — pointing back at
            # the file would spend a tool call re-reading what was just inlined.
            body = p if p.read_text().startswith("---") else doc
            if body:
                block += f"\n  read in full: {body}"
            # A skill that declares a `bin:` also advertises that tool's
            # absolute path (from the repo config's `bins` map) — so the agent
            # invokes it directly rather than relying on PATH, and discovers its
            # flags from the tool's own `--help`. Same rail as the SKILL.md path.
            binpath = bins.get(binname) if binname else None
            if binpath:
                block += f"\n  binary: {binpath}"
            if spec.role_header:
                header = _PKG_ROOT / "prompts" / spec.role_header
                if not header.is_file():
                    raise SystemExit(
                        f"prompt capability {spec.capability!r}: role header "
                        f"does not exist: {header}")
                template = header.read_text()
                if template.count(_GENERIC_SKILL_ANCHOR) != 1:
                    raise SystemExit(
                        f"prompt capability {spec.capability!r}: role header "
                        f"must contain one {_GENERIC_SKILL_ANCHOR} anchor: {header}")
                guidance = template.replace(_GENERIC_SKILL_ANCHOR, "").strip()
                if guidance:
                    block += "\n  Additional role guidance:\n" + "\n".join(
                        f"  {line}" if line else ""
                        for line in guidance.splitlines()
                    )
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

    def _conventions_md(self) -> Path:
        """The always-on conventions doc, at the checkout's ``docs/``.

        Human-readable in its own right and read by anyone working on crustify,
        not only spliced into a prompt — which is what puts it beside
        ``docs/orchestrator-playbook.md`` rather than under ``prompts/``. What lives in
        ``prompts/`` is what the pipeline RENDERS: the stage templates and the
        skill descriptions. Neither provider CLI loads this from a canonical
        path — claude reads ``CLAUDE.md``, codex a repo-root ``AGENTS.md``, and
        it is at neither — so it reaches an agent only by being read here."""
        return _PKG_ROOT.parent.parent / "docs" / "conventions.md"

    def _render_conventions(self) -> str:
        """conventions.md verbatim. Empty string if the doc is absent.

        No substitution: the skill index used to be spliced into a sentinel in
        here, which made a conventions doc that was partly not conventions. The
        two are concatenated in :meth:`system_preamble` instead."""
        p = self._conventions_md()
        return p.read_text() if p.exists() else ""

    def system_preamble(self) -> str:
        """What the backend puts in its system slot, above the stage prompt.

        The conventions doc and the skill index go here rather than into the
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

        Two independent documents, concatenated: conventions.md is the same for
        every agent, the skill index varies with :meth:`skill_specs`. The
        `<!-- CONVENTIONS -->` and `<!-- SKILLS -->` markers in the stage prompts
        record where each one lands relative to the task; neither is a
        substitution point."""
        return "\n\n---\n\n".join(
            part for part in (self._render_conventions().rstrip(),
                              self._render_skills()) if part)
