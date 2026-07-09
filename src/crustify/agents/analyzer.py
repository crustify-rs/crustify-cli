from __future__ import annotations

import json
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyAllocAnalyzer(CrustifyAgent):
    """Allocator-surface analyzer. Emits the project's allocator
    universe as a structured JSON catalogue at
    `<target>/.crustify/alloc.json` — four categories (`allocators`,
    `duplicators`, `refcounts`, `locks`) plus a small `cleansers`
    list. The schema mirrors `templates/syms.json` conventions so any
    primitive resolves against the per-stem syms tree.

    Downstream consumers:
      - The buffer pass of `CrustifyTypeAnalyzer` reads alloc.json to
        partition the allocator universe into `string` / `array`
        cluster entries in the per-stem types.json manifests.
      - The strings/arrays type-wrapper prompts read it to pick the
        right `(alloc, free)` pair per cluster.
      - Lifecycle classification recognises refcount fields and lock
        fields against the `refcounts` and `locks` categories.

    This agent emits **only** the JSON catalogue. Narrative wrapper-
    implications text (Rust ownership / RAII / Send / Sync guidance)
    is not produced here — downstream porting prompts that need such
    text generate it inline from the JSON facts. There is no
    per-target markdown companion artefact.
    """

    name = "CrustifyAllocAnalyzer"
    model = "claude-sonnet-4-6"
    stage = "alloc"
    output = "alloc.json"

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        return {
            "target":           self.target_rel,
            "repo_root":        str(self.repo_root),

            # Schema authority — the JSON contract the agent emits to.
            "alloc_template":   str(crustify_root / "templates" / "alloc.json"),
        }


# ---------------------------------------------------------------------------
# Analyze pipeline
#
# Stages produced by the composer (mechanical, no agent):
#
#   1. compose.scope_manifest    → <target>/.crustify/scope.json
#   2. compose.syms_manifest     → <repo_root>/.crustify/analysis/<dir>/<stem>/syms.json
#   3. compose.types_manifest    → <repo_root>/.crustify/analysis/<dir>/<stem>/types.json
#
# Stages annotated by analyzer agents (CrustifySymbolAnalyzer +
# CrustifyTypeAnalyzer): the agent reads the composer-emitted
# skeleton for a given manifest dir and overlays the semantic fields
# (mutability decisions, ops verdicts, classification rationales).
# The merge primitive preserves both composer-filled and
# agent-annotated fields across re-runs and cross-target evolution.
# ---------------------------------------------------------------------------


class CrustifySymbolAnalyzer(CrustifyAgent):
    """Symbol-side analyze agent. Annotates per-stem `syms.json`
    manifests the composer has emitted in the repo-root analysis tree:
    macro kind classification (reads source for wrap-scope macros
    whose body isn't in the manifest), pointer-arg/ret semantic fields
    on every function entry, and `linked_in` resolution.

    Input contract: the orchestrator passes a `manifests` list — each
    record `{path, names, scope}` directs the agent to one syms.json
    file with the (subset of) entries to process and the port/wrap
    scope tag that applies to them. The agent does no selection
    parsing and no tree walking; the composer + orchestrator have
    already done both. See `prompts/analyzer/symbol_analyzer.md` §1.
    """

    name = "CrustifySymbolAnalyzer"
    model = "claude-opus-4-8"
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
        # The manifests-list contract is the only input vehicle. An
        # empty/missing list is a programmer error at the call site
        # (the orchestrator always derives a non-empty list from
        # composer output); we still tolerate it to keep agent
        # construction side-effect free for tests that just want to
        # introspect `_arguments()`.
        self._manifests = manifests or []
        self.stage_suffix = stage_suffix

    def _arguments(self) -> dict:
        root_dir = self.root_store.root
        # Query-oracle agent: it reads/writes every symbol through `crustify
        # query syms` (which owns the schema + file layout), so it needs only
        # its identity-tuple worklist, the repo root (for C source), and the
        # CodeQL DB. Scope rides on each manifest record, not a scope.json path.
        return {
            "target":               self.target_rel,
            "repo_root":            str(self.repo_root),
            "manifests":            json.dumps(self._manifests, indent=2),
            "codeql_db":            str(root_dir / "codeql" / "db"),
        }


class CrustifyTypeAnalyzer(CrustifyAgent):
    """Type-side analyze agent. Annotates per-stem `types.json`
    manifests with lifecycle classification (ctors / dtor /
    up_ref / clones), per-field accessors, locking, conditional_drop.

    One class, two prompts (selected via the `stage` arg, under
    `prompts/analyzer/`):

      - `type_analyzer` (default) — the per-struct analyzer (worklist is
        structs only; enums/unions are composer-deterministic, callbacks are
        symbols). A generated-container instance is just an ordinary struct,
        analyzed the same way (no special path).
      - `buffer_analyzer` — cross-cutting `string`/`array` allocator-
        cluster synthesis (single run; alloc.json-gated).

    Input contract: the orchestrator passes a `manifests` list — each
    record `{path, names, scope}` directs the agent to one types.json
    file with the (subset of) entries to process and the port/wrap
    scope tag that applies to them. The agent does no selection
    parsing and no tree walking; the composer + orchestrator have
    already done both. See `prompts/analyzer/type_analyzer.md` §1.
    The whole-tree buffer pass invokes the agent with an empty
    `manifests` list + a `selection`, and relies on its prompt-specific
    walk instructions.
    """

    name = "CrustifyTypeAnalyzer"
    model = "claude-opus-4-8"
    stage = "type_analyzer"
    prompt_dir = "analyzer"

    def __init__(
        self,
        target: Path,
        *,
        manifests: list[dict] | None = None,
        selection: str | None = None,
        stage: str | None = None,
        stage_suffix: str | None = None,
    ) -> None:
        super().__init__(target)
        # Per-dir contract input. Empty when this is the whole-tree buffer
        # synthesis pass; that pass uses `selection` below and the agent
        # walks the analysis tree itself per the synthesis prompt's
        # instructions.
        self._manifests = manifests or []
        # Selection string used ONLY by the buffer synthesis prompt
        # (e.g. "strings; arrays"). The per-dir `type_analyzer` prompt does
        # not interpolate it.
        self._selection = selection or ""
        # `stage` selects the prompt (prompts/analyzer/<stage>.md) and the
        # log filename; defaults to the per-dir type_analyzer prompt.
        if stage is not None:
            self.stage = stage
        self.stage_suffix = stage_suffix

    def _arguments(self) -> dict:
        root_dir = self.root_store.root
        alloc_json = str(root_dir / "alloc.json")
        # Both stages are query-oracle agents (read/write through `crustify
        # query types`, which owns the schema + file layout). The `type_analyzer`
        # stage discovers the allocator universe via the oracle (`query mem`), so
        # it no longer takes an `alloc_manifest` input. The `buffer_analyzer`
        # stage still reads alloc.json directly (as `alloc_doc`) plus its
        # `selection`. Nothing else is referenced.
        return {
            "target":               self.target_rel,
            "repo_root":            str(self.repo_root),
            "manifests":            json.dumps(self._manifests, indent=2),
            "codeql_db":            str(root_dir / "codeql" / "db"),
            # buffer_analyzer only:
            "selection":            self._selection,
            "alloc_doc":            alloc_json,    # buffer_analyzer
        }