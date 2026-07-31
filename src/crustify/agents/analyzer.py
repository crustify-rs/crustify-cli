from __future__ import annotations

import json
from pathlib import Path

from crustify.agents.base import CrustifyAgent


class CrustifySymbolAnalyzer(CrustifyAgent):
    """Symbol-side analyze agent. Annotates per-stem `syms.json`
    manifests the composer has emitted in the repo-root analysis tree:
    the pointer-arg/ret/global ownership blocks, and the entry-level
    `lifetime` block naming which arg (if any) this symbol drops,
    disposes or clones.

    The orchestrator passes a `manifests` list, each record
    `{symbols: [{name, file}]}` directing the agent to a batch of identity
    tuples it resolves through `crustify-cli query symbols`. No scope tag rides
    along: symbol analysis is a uniform judgement about the C code,
    independent of whether the symbol is later ported or wrapped. The agent
    does no tree walking; the composer + orchestrator have already done both.
    """

    name = "CrustifySymbolAnalyzer"
    model = "anthropic/claude-opus-4-8"
    stage = "symbol_analyzer"
    prompt_dir = "analyzer"

    # `output` left as None — per-stem syms.json manifests are emitted
    # by the composer before this agent runs, so the file's existence is
    # not a valid "done" signal. There is no state.json and no done-marker
    # gating re-invocation: idempotency comes from the per-entry `names`
    # filter (untouched entries are preserved) and the fill-nulls-only
    # contract (see prompts/analyzer/symbol_analyzer.md §1).

    def __init__(
        self,
        target: Path,
        *,
        manifests: list[dict] | None = None,
        stage_suffix: str | None = None,
    ) -> None:
        super().__init__(target)
        # The manifests-list contract is the only input vehicle: a per-symbol
        # batch `{symbols: [{name, file}]}`. An empty/missing list is tolerated
        # to keep construction side-effect free for tests that just introspect
        # `_arguments()`.
        self._manifests = manifests or []
        self.stage_suffix = stage_suffix

    def _arguments(self) -> dict:
        root_dir = self.root_store.root
        # Query-oracle agent: it reads/writes every symbol through `crustify
        # query syms` (which owns the schema + file layout), so it needs only
        # its identity-tuple worklist, the repo root (for C source), and the
        # CodeQL DB. Scope rides on no path — symbol analysis is scope-agnostic.
        return {
            **super()._arguments(),
            "target":               self.target_rel,
            "repo_root":            str(self.repo_root),
            "manifests":            json.dumps(self._manifests, indent=2),
            "codeql_db":            str(root_dir / "codeql" / "db"),
        }


class CrustifyTypeAnalyzer(CrustifyAgent):
    """Type-side analyze agent. Annotates per-stem `types.json`
    manifests with each pointer field's ownership block and any guarded
    field's lock binding. A type's lifecycle is NOT recorded here — it is
    reverse-derived from the symbols that carry the role (see
    `query symbols --lifetime-for`).

    Driven by `prompts/analyzer/type_analyzer.md`: the per-struct analyzer
    (worklist is structs only; enums/unions are composer-deterministic,
    callbacks are symbols). A generated-container instance is just an ordinary
    struct, analyzed the same way (no special path).

    Input contract: the orchestrator passes a `manifests` list — each
    record `{path, names, scope}` directs the agent to one types.json
    file with the (subset of) entries to process and the port/wrap
    scope tag that applies to them. The agent does no selection
    parsing and no tree walking; the composer + orchestrator have
    already done both. See `prompts/analyzer/type_analyzer.md` §1.
    """

    name = "CrustifyTypeAnalyzer"
    model = "anthropic/claude-opus-4-8"
    stage = "type_analyzer"
    prompt_dir = "analyzer"

    def __init__(
        self,
        target: Path,
        *,
        manifests: list[dict] | None = None,
        stage: str | None = None,
        stage_suffix: str | None = None,
        unscoped: bool = False,
    ) -> None:
        super().__init__(target)
        # Scope CONTEXT for the prompt's `{scope}` input: mirrors whether the
        # caller passed --unscoped. Not a filter — the composer has already
        # gated emission; this only tells the agent how wide its workset's
        # context is (codebase-wide vs wrap-/port-scope).
        self._unscoped = bool(unscoped)
        # Per-dir contract input.
        self._manifests = manifests or []
        # `stage` selects the prompt (prompts/analyzer/<stage>.md) and the
        # log filename; defaults to the per-dir type_analyzer prompt.
        if stage is not None:
            self.stage = stage
        self.stage_suffix = stage_suffix

    def _arguments(self) -> dict:
        root_dir = self.root_store.root
        # A query-oracle agent: it reads and writes through `crustify-cli query
        # types`, which owns the schema + file layout. Nothing else is
        # referenced.
        return {
            **super()._arguments(),
            "target":               self.target_rel,
            "repo_root":            str(self.repo_root),
            "manifests":            json.dumps(self._manifests, indent=2),
            "codeql_db":            str(root_dir / "codeql" / "db"),
            "scope":                "unscoped" if self._unscoped else "scoped",
        }