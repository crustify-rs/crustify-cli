"""TranslateAgent — the merged wrap-stage codegen agent (agent half).

One agent for every batch recorded in a wave document:

  * a **type** batch (a struct / union / enum + a budget slice of its field
    accessors) — the type route in ``translator.md``, and
  * a **free-symbol** batch (functions / globals not bound to one type) —
    the symbol route in ``translator.md``.

Ordinary wrap-scope *macros* are not standalone units. The translator that
needs one extends the owning `-sys` crate's bindgen allowlist or shim lazily.

The standalone oracle selects and orders units. The executor resolves their
authored `.rs` homes and this agent fills one batch's scheduler-local anchors.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from crustify.agents.base import CrustifyAgent, SkillSpec, _PKG_ROOT

_TYPE_KINDS = ("struct", "union", "enum", "macro")

_CORE_SKILLS = (
    SkillSpec("crustify", "src/crustify/prompts/skills/translator.md"),
)

_CAPABILITY_SKILLS: dict[str, SkillSpec] = {
    "crustify-oracle": SkillSpec(
        "crustify-oracle", "SKILL.md", capability="crustify-oracle",
        role_header="skills/oracle.md",
    ),
    "ffibox": SkillSpec(
        "ffibox", "SKILL.md", capability="ffibox",
        role_header="skills/ffibox.md",
    ),
    "crustify-audit": SkillSpec(
        "crustify-audit", "SKILL.md", capability="crustify-audit",
        role_header="skills/audit.md",
    ),
}


class TranslateAgent(CrustifyAgent):
    """Emit safe Rust wrapper(s) for one scheduled wrap batch (type or syms)."""

    name = "TranslateAgent"
    model = "anthropic/claude-opus-5"
    output = None  # scheduler gates via the per-item todo; agent runs when called.

    def __init__(
        self,
        target: Path,
        *,
        batch_kind: str,                       # "type" | "syms"
        deps: list[str] | None = None,         # push path only; pull path discovers deps itself
        # type batch — parallel lists over the batch's type(s).
        tags: list[str] | None = None,
        kinds: list[str] | None = None,
        entry_files: list[str] | None = None,
        rs_outs: list[str] | None = None,
        fields_per: list[list[str]] | None = None,      # parallel to tags
        ops_per: list[list[str]] | None = None,         # parallel to tags
        op_rs_outs_per: list[list[str]] | None = None,  # parallel to ops_per
        fallback_deps: list[str] | None = None,         # deps not wrapped yet → raw ffi::T
        naked_users: list[str] | None = None,           # users to switch onto this wrapper
        # pull path (single struct/union/enum): the agent discovers everything via
        # oracle queries; the scheduler hands only the field window.
        # syms batch:
        rs_out: str | None = None,
        syms: list[dict] | None = None,
        # Lifetime-discovery mode, reached through a wave the oracle wrote
        # with `schedule --lifetime-for`: instead of a resolved worklist, the
        # agent is handed a SPEC and finds the lifecycle primitives itself.
        # Rides in `syms` as a mode marker so the prompt has one input to read.
        lifetime_for: str | None = None,
        # What the agent is being asked to DO with this batch, handed straight
        # to the prompt as `{objective}`: "wrap" | "port" | "review". Lifetime
        # discovery is identified by its `raw-lifetime` route, not by a fourth
        # objective; normal discovery emits wrapping strategies, while an
        # explicit review remains review.
        objective: str = "wrap",
        # Usually the same as `objective`. A raw-lifetime marker in a port
        # campaign deliberately has task objective `wrap` while retaining
        # campaign objective `port`, so discovery uses the targeted scope.
        campaign_objective: str | None = None,
        prompt_capabilities: tuple[str, ...] | None = None,
        repo_root: Path | None = None,         # worktree root in an isolated wave
    ) -> None:
        super().__init__(target, repo_root=repo_root)
        self._batch_kind = batch_kind
        self._objective = objective
        self._campaign_objective = campaign_objective or objective
        self._prompt_capabilities = (
            tuple(prompt_capabilities) if prompt_capabilities is not None
            else self.configured_capabilities(self.layout)
        )
        self._deps = list(deps or [])
        self._tags = list(tags or [])
        self._kinds = list(kinds or [])
        self._entry_files = list(entry_files or [])
        self._rs_outs = list(rs_outs or [])
        self._fields_per = list(fields_per or [])
        self._ops_per = list(ops_per or [])
        # Per-op output file, parallel to ``ops_per``. An op whose module
        # differs from its type's file gets its ``impl`` block emitted there.
        self._op_rs_outs_per = list(op_rs_outs_per or [])
        self._fallback_deps = list(fallback_deps or [])
        self._naked_users = list(naked_users or [])
        # syms batch:
        self._rs_out = rs_out or (self._rs_outs[0] if self._rs_outs else "")
        self._lifetime_for = lifetime_for
        self._syms = ([{"lifetime_for": lifetime_for}] if lifetime_for
                      else list(syms or []))

        if self._batch_kind not in ("type", "syms"):
            raise ValueError(f"TranslateAgent: unsupported batch kind {self._batch_kind!r}")
        if self._batch_kind == "type":
            unsupported = sorted(set(self._kinds) - set(_TYPE_KINDS))
            if unsupported:
                raise ValueError(
                    f"TranslateAgent: unsupported type kind(s) {unsupported}; "
                    f"expected one of {list(_TYPE_KINDS)}")

    @staticmethod
    def configured_capabilities(layout) -> tuple[str, ...]:
        """Read the translator's prompt-only capability set once."""
        p = layout.repo_config
        cfg = json.loads(p.read_text()) if p.exists() else {}
        block = cfg.get("prompt_capabilities") or {}
        names = block.get("translator") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise SystemExit(
                "cli-config.json: prompt_capabilities.translator must be a list of names")
        unknown = sorted(set(names) - set(_CAPABILITY_SKILLS))
        if unknown:
            raise SystemExit(
                "cli-config.json: unknown translator prompt capability: "
                + ", ".join(unknown))
        # Preserve authored order in the prompt; collapse accidental repeats.
        return tuple(dict.fromkeys(names))

    def skill_specs(self) -> tuple[SkillSpec, ...]:
        return _CORE_SKILLS + tuple(
            _CAPABILITY_SKILLS[name] for name in self._prompt_capabilities
        )

    @property
    def stage(self) -> str:  # type: ignore[override]
        """The per-agent log stem: ``<objective>-<kind>_<key>``, e.g.
        ``port-type_git_delta_index`` / ``wrap-symbol_access``.

        Both halves are known HERE and only here: the objective is the campaign's,
        but the kind is this batch's, and one invocation can carry both (a
        wave's steps can batch types and symbols alike). So the wave-level
        identity (`Stage.verb` -> session branch, worktree dirs) tags with the
        objective alone, and the full pair lands on the agent's own files —
        which is where it pays, since `crustify-log-cost` buckets by this
        prefix and can now price `wrap-type` against `port-type` directly."""
        if self._batch_kind == "type":
            key = self._tags[0] if self._tags else "batch"
        elif self._lifetime_for:
            key = f"lifetime_for__{self._lifetime_for}"
        else:
            key = self._syms[0]["name"] if self._syms else "syms"
        unit = "type" if self._batch_kind == "type" else "symbol"
        return (f"{self._objective}-{unit}_"
                f"{re.sub(r'[^A-Za-z0-9_]+', '_', key or 'batch')}")

    def _prompt(self) -> str:
        return (_PKG_ROOT / "prompts" / "translator.md").read_text()

    def _arguments(self) -> dict:
        common = {
            # Base first: `target`, `repo_root`, and `git_base` (the wave's
            # session branch). Building this dict from scratch silently dropped
            # every key the base adds — a template naming one dies with KeyError
            # before the agent issues a request.
            **super()._arguments(),
            "task_objective": self._objective,
            "workspace_root": str(self.layout.rust),
            "build_json":     str(self.layout.build_json),
            # NOTE: no `conventions` key. The conventions doc and skill index are
            # no longer a `.format` slot — they go to the backend's system slot
            # via `system_preamble()`, out of reach of context compaction.
        }
        common["campaign_objective"] = self._campaign_objective

        if self._lifetime_for:
            route = "raw-lifetime"
            items = self._syms
        elif self._batch_kind == "syms":
            route = "symbol"
            items = self._syms
        else:
            route = "type"
            files = self._entry_files or [None] * len(self._tags)
            items = [
                {"name": n, "defined_in": f, "kind": k}
                for n, f, k in zip(self._tags, files, self._kinds)
            ]
        common["worklist"] = json.dumps({"route": route, "items": items})
        return common
