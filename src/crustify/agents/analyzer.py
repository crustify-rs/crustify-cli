from __future__ import annotations

import json
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyAllocAnalyzer(CrustifyAgent):
    """Allocator-surface analyzer. Emits the project's allocator
    universe as a structured JSON catalogue at
    `<target>/.crustify/alloc.json` — three top-level keys (`families`,
    `refcounts`, `locks`), where each `families` entry is one untyped
    deallocator plus the flat `allocators` / `copies` lists it owns
    (resize / duplicator read off the qualifier flags, not an `op` tag).
    Field meaning lives in `docs/schemas/alloc.md`. The schema mirrors
    `templates/syms.json` conventions so any primitive resolves against
    the per-stem syms tree.

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
    the `macro` block on every macro entry (reads the `#define` body
    from source — it isn't in the manifest), pointer-arg/ret semantic
    fields on every function/callback entry, and `linked_in` resolution.

    Two modes, one prompt (`prompts/analyzer/symbol_analyzer.md`), both
    signalled through the `manifests` worklist:

      - PER-SYMBOL (default) — the orchestrator passes a `manifests` list,
        each record `{symbols: [{name, file}]}` directing the agent to a
        batch of identity tuples it resolves through `crustify query syms`.
        No scope tag rides along: symbol analysis is a uniform judgement
        about the C code, independent of whether the symbol is later ported
        or wrapped. The agent does no tree walking; the composer +
        orchestrator have already done both.
      - LIFETIME DISCOVERY — a single cross-cutting pass whose worklist is
        the sentinel record `{symbols: [{name: LIFETIMES_TAG, file: None}]}`.
        Seeing that tag, the agent scouts source for every lifecycle
        primitive (allocator / free / clone / refcount / lock), composes any
        missing entry on demand (`analyze symbols --compose-only --name …`),
        and tags each with a `lifetime` block. Invoked by `run_lifetime_pass`
        / `analyze symbols --lifetimes`.
    """

    name = "CrustifySymbolAnalyzer"
    model = "claude-opus-4-8"
    stage = "symbol_analyzer"
    prompt_dir = "analyzer"

    # Sentinel worklist tag that flips the agent into lifetime discovery
    # mode (see the LIFETIME DISCOVERY note above). It is a reserved name,
    # never a real C symbol, carried with `file: None`.
    LIFETIMES_TAG = "lifetimes"

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
        # The manifests-list contract is the only input vehicle. Both modes
        # ride it: a per-symbol batch, or the single `LIFETIMES_TAG` sentinel
        # record for the discovery pass. An empty/missing list is tolerated
        # to keep construction side-effect free for tests that just
        # introspect `_arguments()`.
        self._manifests = manifests or []
        self.stage_suffix = stage_suffix

    def _arguments(self) -> dict:
        root_dir = self.root_store.root
        # Query-oracle agent: it reads/writes every symbol through `crustify
        # query syms` (which owns the schema + file layout), so it needs only
        # its identity-tuple worklist, the repo root (for C source), and the
        # CodeQL DB. Scope rides on no path — symbol analysis is scope-agnostic.
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