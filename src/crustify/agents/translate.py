"""TranslateAgent — the merged wrap-stage codegen agent (agent half).

One agent for every wrap unit the ``--name`` scheduler produces:

  * a **type** batch (a struct / union / enum + a budget slice of its field
    accessors) — the lifecycle + field-accessor recipe in ``types.md``;
    synthetic string / array clusters use their own prompts, and
  * a **free-symbol** batch (functions / globals not bound to one type) —
    ``symbols.md``: a thin safe view over the FFI surface, no
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
    "struct":           "types.md",
    "union":            "types.md",
    "enum":             "types.md",
    # A type-minting macro (`generates` non-empty) — the GENERATOR of a template
    # family. Its deliverable is a struct, so it takes the type recipe: the
    # generic its instances alias, with the `CCell` impl, the layout gate and
    # the field accessors that only `types.md` describes.
    #
    # Every macro that reaches this table is a generator: `wrap._is_macro`
    # excludes all others from the selection, so `subkind == "macro"` here
    # already means "minting". `_schedule.form_units` is what puts one in a type
    # batch; this entry is the other half of that routing.
    "macro":            "types.md",
}
# Type kinds with no wrapper codegen yet. Callbacks are NOT here: they route as
# sym-units (see _schedule.form_units) to symbols.md §3.
_NOT_YET: tuple[str, ...] = ()


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
        # `crustify-cli query`/`scaffold`; the scheduler hands only the field window.
        # syms batch:
        rs_out: str | None = None,
        syms: list[dict] | None = None,
        # Lifetime-discovery mode (`translate --lifetime-for`):
        # instead of a resolved worklist, the agent is handed a SPEC and finds
        # the lifecycle primitives itself. Rides in `syms` as a mode marker so
        # the prompt has one input to read, exactly as the retired analyzer's rode in
        # `manifests`.
        lifetime_for: str | None = None,
        # What the agent is being asked to DO with this batch, handed straight
        # to the prompt as `{objective}`: "wrap" | "port" | "review" | "raw".
        # The last is the lifetime-tier discovery arm and rides with
        # `lifetime_for`, which injects it -- no --objective selects it. The
        # scheduler always supplies one, so the prompt never branches on an
        # empty slot -- and the agent no longer has to INFER review mode from
        # finding a filled anchor on disk, which was indistinguishable from
        # being asked to nativize one.
        objective: str = "wrap",
        repo_root: Path | None = None,         # worktree root in an isolated wave
    ) -> None:
        super().__init__(target, repo_root=repo_root)
        self._batch_kind = batch_kind
        self._objective = objective
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

    @property
    def stage(self) -> str:  # type: ignore[override]
        """The per-agent log stem: ``<objective>-<kind>_<key>``, e.g.
        ``port-type_git_delta_index`` / ``wrap-symbol_access``.

        Both halves are known HERE and only here: the objective is the wave's,
        but the kind is this batch's, and one invocation can carry both (a
        `--dag-layer` selects types and symbols alike). So the wave-level
        identity (`Stage.verb` -> session branch, worktree dirs) tags with the
        objective alone, and the full pair lands on the agent's own files —
        which is where it pays, since `utils/log_cost.py` buckets by this
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

    @property
    def _kind(self) -> str:
        """Family is homogeneous in kind; dispatch the prompt on the first tag."""
        return self._kinds[0] if self._kinds else ""

    def _prompt(self) -> str:
        if self._batch_kind == "syms":
            return (_PKG_ROOT / "prompts" / "symbols.md").read_text()
        if self._kind in _NOT_YET:
            raise NotImplementedError(
                f"TranslateAgent: kind {self._kind!r} ({self._tags}) — "
                f"wrapper codegen not implemented for this kind yet.")
        prompt_file = _PROMPT_BY_KIND.get(self._kind)
        if prompt_file is None:
            raise ValueError(
                f"TranslateAgent: unsupported manifest kind {self._kind!r} for "
                f"{self._tags!r}. Expected one of {sorted(_PROMPT_BY_KIND)}.")
        return (_PKG_ROOT / "prompts" / prompt_file).read_text()

    def _arguments(self) -> dict:
        common = {
            # Base first: `target`, `repo_root`, and `git_base` (the wave's
            # session branch). Building this dict from scratch silently dropped
            # every key the base adds — a template naming one dies with KeyError
            # before the agent issues a request.
            **super()._arguments(),
            "objective":      self._objective,
            "workspace_root": str(self.layout.rust),
            "build_json":     str(self.layout.build_json),
            # NOTE: no `principles` key. The principles doc and skill index are
            # no longer a `.format` slot — they go to the backend's system slot
            # via `system_preamble()`, out of reach of context compaction.
        }
        # All wrap paths are now PULL: the agent discovers its job via `crustify
        # query`/`query dag`/`query sym`. The scheduler hands only identity.
        if self._batch_kind == "syms":
            # syms-pull: the pooled symbol names (+ defined_in for collisions).
            common["syms"] = json.dumps(self._syms)
            return common
        # types-pull, shaped exactly like syms-pull: the batch's type names
        # (+ defined_in for collisions). `_schedule.pack` pools up to
        # `max_types` of them, so this is a LIST — handing only `tags[0]` here
        # silently dropped the rest of a pooled batch.
        #
        # No `kind`: the agent reads it off the record it fetches anyway, and
        # every kind routes to this one prompt, so passing it added a slot the
        # scheduler had to keep true. It stays on `self._kinds` for the prompt
        # dispatch above, which is internal.
        #
        # The agent pulls the field set, the reverse-derived lifecycle and the
        # cast graph from each record itself; nothing about the accessor
        # surface is handed over, since an import type already carries only
        # its target-touched fields.
        files = self._entry_files or [None] * len(self._tags)
        common["types"] = json.dumps(
            [{"name": n, "defined_in": f}
             for n, f in zip(self._tags, files)])
        return common
