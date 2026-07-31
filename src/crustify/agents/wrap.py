"""CrustifyWrap — the merged wrap-stage codegen agent (agent half).

One agent for every wrap unit the ``--name`` scheduler produces:

  * a **type** batch (a struct / union / enum + a budget slice of its field
    accessors) — the lifecycle + field-accessor recipe in ``type_wrapper.md``;
    synthetic string / array clusters use their own prompts, and
  * a **free-symbol** batch (functions / globals not bound to one type) —
    ``symbol_wrapper.md``: a thin safe view over the FFI surface, no
    field-access discipline (the symbol stays in C; you only add the safe
    wrapper).

Wrap-scope *macros* are **not** handled here — they are header-defined, so
bindgen owns their `-sys` shims (see scaffold/bindgen). Port-scope (TU) macros
are the port agent's job.

The deterministic :mod:`crustify._schedule` resolves *which* units, *what
order*, and *which `.rs`*; this agent fills the scaffolded anchors for one
batch. Idempotency is the per-item ``// crustify:todo`` placeholder (filled →
removed), not a whole-file sentinel.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT

# struct/union/enum share the type recipe.
_PROMPT_BY_KIND: dict[str, str] = {
    "struct":           "type_wrapper.md",
    "union":            "type_wrapper.md",
    "enum":             "type_wrapper.md",
}
# Type kinds with no wrapper codegen yet. Callbacks are NOT here: they route as
# sym-units (see _schedule.form_units) to symbol_wrapper.md §3.
_NOT_YET: tuple[str, ...] = ()


class CrustifyWrap(CrustifyAgent):
    """Emit safe Rust wrapper(s) for one scheduled wrap batch (type or syms)."""

    name = "CrustifyWrap"
    model = "anthropic/claude-opus-4-8"
    prompt_dir = "wrapper"
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
        # `crustify-cli query`/`scaffold`; the scheduler hands only the field window.
        fields_range: list[int] | None = None,          # [lo, hi) into the field list
        # syms batch:
        rs_out: str | None = None,
        syms: list[dict] | None = None,
        # Lifetime-discovery mode (mirrors `analyze symbols --lifetime-for`):
        # instead of a resolved worklist, the agent is handed a SPEC and finds
        # the lifecycle primitives itself. Rides in `syms` as a mode marker so
        # the prompt has one input to read, exactly as the analyzer's rides in
        # `manifests`.
        lifetime_for: str | None = None,
        repo_root: Path | None = None,         # worktree root in an isolated wave
    ) -> None:
        super().__init__(target, repo_root=repo_root)
        self._batch_kind = batch_kind
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
        self._fields_range = list(fields_range) if fields_range is not None else None
        # syms batch:
        self._rs_out = rs_out or (self._rs_outs[0] if self._rs_outs else "")
        self._lifetime_for = lifetime_for
        self._syms = ([{"lifetime_for": lifetime_for}] if lifetime_for
                      else list(syms or []))

    @property
    def stage(self) -> str:  # type: ignore[override]
        if self._batch_kind == "type":
            key = self._tags[0] if self._tags else "batch"
        elif self._lifetime_for:
            key = f"lifetime_for__{self._lifetime_for}"
        else:
            key = self._syms[0]["name"] if self._syms else "syms"
        return f"wrap_{re.sub(r'[^A-Za-z0-9_]+', '_', key or 'batch')}"

    @property
    def _kind(self) -> str:
        """Family is homogeneous in kind; dispatch the prompt on the first tag."""
        return self._kinds[0] if self._kinds else ""

    def _prompt(self) -> str:
        if self._batch_kind == "syms":
            return (_PKG_ROOT / "prompts" / "wrapper" / "symbol_wrapper.md").read_text()
        if self._kind in _NOT_YET:
            raise NotImplementedError(
                f"CrustifyWrap: kind {self._kind!r} ({self._tags}) — "
                f"wrapper codegen not implemented for this kind yet.")
        prompt_file = _PROMPT_BY_KIND.get(self._kind)
        if prompt_file is None:
            raise ValueError(
                f"CrustifyWrap: unsupported manifest kind {self._kind!r} for "
                f"{self._tags!r}. Expected one of {sorted(_PROMPT_BY_KIND)}.")
        return (_PKG_ROOT / "prompts" / "wrapper" / prompt_file).read_text()

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        common = {
            # Base first: `target`, `repo_root`, and `git_base` (the wave's
            # session branch). Building this dict from scratch silently dropped
            # every key the base adds — a template naming one dies with KeyError
            # before the agent issues a request.
            **super()._arguments(),
            "workspace_root": str(self.layout.rust),
            "analysis_root":  str(self.layout.analysis),
            "build_json":     str(self.layout.build_json),
            "rs_out":         self._rs_out,
            "deps":           json.dumps(self._deps),
            "discipline":     str(crustify_root / "docs" / "DISCIPLINE.md"),
            "crustify_prim": str(
                crustify_root / ".." / "crustify-prim" / "src" / "lib.rs"),
            # Always-on principles preamble (AGENTS.md), with the role-scoped
            # skill index spliced into its `<!-- SKILLS_INDEX -->` sentinel.
            # Inlined as `{principles}` (type_wrapper.md; harmless if unused).
            "principles":     self._render_principles(),
        }
        # All wrap paths are now PULL: the agent discovers its job via `crustify
        # query`/`query dag`/`query sym`. The scheduler hands only identity.
        if self._batch_kind == "syms":
            # syms-pull: the pooled symbol names (+ defined_in for collisions).
            common["syms"] = json.dumps(self._syms)
            return common
        # type_wrapper.md handles a single struct / union / enum: one tag + its
        # field-accessor window. The lifecycle (reverse-derived droppers /
        # disposers / cloners) and the cast graph are pulled from the record;
        # field accessors are the only windowed surface.
        if self._fields_range is not None:
            common.update({
                "tag":          self._tags[0],
                "kind":         self._kind,
                "fields_range": f"{self._fields_range[0]}:{self._fields_range[1]}",
            })
            return common
        common.update({
            "tags": json.dumps(self._tags),
            "kind": self._kind,
        })
        return common
