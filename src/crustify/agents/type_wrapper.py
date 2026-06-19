"""CrustifyTypeWrapper — per-type wrap-stage codegen agent (agent half).

The deterministic scheduler (``crustify.wrap``) resolves *which* types to
wrap, *in what order*, and *into which `.rs` file(s)*. This agent does the
codegen for one scheduled job: it reads the type's manifest entry, the
generated ``bindings.rs``, ``DISCIPLINE.md``, and the ``crustify`` crate,
then emits the safe wrapper per ``docs/WRAP_STAGE_PLAN.md`` §4.

A job is usually a single type, but may be an SCC cluster of mutually-
recursive types (``tags`` / ``rs_outs`` carry >1 entry) that must be
emitted together.

Contract with the scheduler (both load-bearing):

  - every emitted file starts with the ``//! crustify:wrap`` **sentinel**
    (the scheduler's idempotency check), and
  - keeps a ``//! Replaces: <tag>`` line (the scheduler resolves each
    type's file by scanning for it — collision-safe).

Output lives in the Rust workspace (``rust/crates/...``), not under
``crustify/``, so ``output`` is ``None``: the scheduler gates invocation
(sentinel skip / ``--redo``); the agent always runs when called.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


# Map manifest ``kind`` → prompt filename under ``prompts/wrapper/``.
# struct/union/enum share the lifecycle+accessors+ops recipe; the synthetic
# string/array clusters and the ``typegen`` *generator* have their own
# kind-dispatched prompts on the same job contract. ``typegen_instance`` is not
# a standalone wrap job (a wrap-scope instance folds into its element type's
# file; here every instance is port-scope anyway); ``callback`` is pending.
_PROMPT_BY_KIND: dict[str, str] = {
    "struct":  "type_wrapper.md",
    "union":   "type_wrapper.md",
    "enum":    "type_wrapper.md",
    "string":  "strings_wrapper.md",
    "array":   "arrays_wrapper.md",
    "typegen": "generics_wrapper.md",
}

_NOT_YET = ("callback", "typegen_instance")


class CrustifyTypeWrapper(CrustifyAgent):
    """Emit the safe Rust wrapper(s) for one scheduled wrap job."""

    name = "CrustifyTypeWrapper"
    model = "claude-opus-4-8"
    prompt_dir = "wrapper"
    output = None  # scheduler gates via the sentinel; agent runs when called.

    def __init__(
        self,
        target: Path,
        *,
        tags: list[str],
        kind: str,
        entry_files: list[str],
        rs_outs: list[str],
        deps: list[str],
        linked_in: list[str],
        fields: list[list[str]] | None = None,
        ops: list[list[str]] | None = None,
    ) -> None:
        super().__init__(target)
        self._tags = list(tags)
        self._kind = kind
        self._entry_files = list(entry_files)
        self._rs_outs = list(rs_outs)
        self._deps = list(deps)
        self._linked_in = list(linked_in)
        # Per-tag fixed worklists, parallel to ``tags`` — the orchestrator has
        # already sliced each type's surface to the effort budget. ``None``
        # (standalone/legacy callers) means "no orchestrator slice"; emit an
        # empty list so the prompt's budget contract degrades gracefully.
        self._fields = [list(x) for x in (fields or [[] for _ in tags])]
        self._ops = [list(x) for x in (ops or [[] for _ in tags])]

    @property
    def stage(self) -> str:  # type: ignore[override]
        safe = "_".join(
            re.sub(r"[^A-Za-z0-9_]+", "_", t) for t in self._tags
        )
        return f"wrap_{safe}"

    def _prompt(self) -> str:
        if self._kind in _NOT_YET:
            raise NotImplementedError(
                f"CrustifyTypeWrapper: kind {self._kind!r} "
                f"({'+'.join(self._tags)}) — synthetic/callback wrapping is "
                f"not yet refit to the vanilla-path contract. See "
                f"docs/WRAP_STAGE_PLAN.md §4.7."
            )
        prompt_file = _PROMPT_BY_KIND.get(self._kind)
        if prompt_file is None:
            raise ValueError(
                f"CrustifyTypeWrapper: unsupported manifest kind "
                f"{self._kind!r} for {self._tags!r}. Expected one of "
                f"{sorted(_PROMPT_BY_KIND)} (synthetic kinds pending refit)."
            )
        return (_PKG_ROOT / "prompts" / "wrapper" / prompt_file).read_text()

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        return {
            "target":         self.target_rel,
            "repo_root":      str(self.repo_root),
            "workspace_root": str(self.layout.rust),          # crustify/rust (shared)
            "analysis_root":  str(self.layout.analysis),      # crustify/analysis

            # Job payload (JSON lists; tags/entry_files/rs_outs are parallel).
            "tags":           json.dumps(self._tags),
            "kind":           self._kind,
            "entry_files":    json.dumps(self._entry_files),
            "rs_outs":        json.dumps(self._rs_outs),
            "deps":           json.dumps(self._deps),
            "linked_in":      json.dumps(self._linked_in),

            # Fixed per-tag worklists (parallel to `tags`), already sliced to
            # the effort budget by the orchestrator. The wrapper is agnostic
            # of the budget *numbers* — it just honours these final lists.
            "fields":         json.dumps(self._fields),
            "ops":            json.dumps(self._ops),

            # Authorities.
            "discipline":     str(crustify_root / "docs" / "DISCIPLINE.md"),
            "crustify_crate": str(
                crustify_root / ".." / "crustify-crate" / "src" / "lib.rs"
            ),
        }
