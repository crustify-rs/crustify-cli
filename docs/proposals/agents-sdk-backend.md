# Proposal: Pluggable agent backend -- OpenAI Agents SDK

Status: draft
Scope: add the OpenAI Agents SDK as a second, selectable agent backend.
The Claude CLI backend is explicitly OUT OF SCOPE here (separate proposal).

## 1. Motivation

Crustify binds directly to kiss `RelentlessAgent` at a single call site,
`src/crustify/agents/base.py:167-178`. Auditing that contract shows it is
deliberately thin:

- Tools: every agent uses exactly `[Bash, Read, Edit, Write]`
  (`base.py:266`). No subclass overrides `_tools()`; no bespoke tools exist
  anywhere in crustify.
- No hooks: no `pre_step_hook`, no mid-run message injection.
- Return discarded: `agent.run(...)` at `base.py:168` is never assigned.
  Nothing consumes the `finish()` YAML summary.
- Completion is data-driven: `_is_done()` (`base.py:222`) judges success by
  on-disk artifacts, not by the agent's self-reported result.

So the real contract is: render prompt -> string, hand it 4 generic tools,
run in a work_dir, stream output to a Printer, throw the result away, judge
by artifacts on disk. That contract maps cleanly onto the OpenAI Agents SDK.

Why the Agents SDK specifically:

- Pure in-process Python library -- no CLI subprocess dependency.
- Provider-agnostic: routes to Claude via LiteLLM today, other providers
  later, with no code change at the agent-role layer.
- Orchestration primitives (handoffs, guardrails, sessions) available for
  future use, though not required by the current single-agent-per-stage model.

Auth/cost note: this backend uses API-key (metered) billing via
`ANTHROPIC_API_KEY`. It does NOT run on a Claude subscription -- that path
requires the Claude CLI backend, deferred to a separate proposal.

## 2. Scope

In scope:

- A `Backend` protocol that abstracts the `base.py:167-178` call site.
- `RelentlessBackend`: today's kiss code, moved behind the protocol, no
  behavior change (pure refactor).
- `AgentsSdkBackend`: an OpenAI Agents SDK implementation.
- An events -> Printer adapter so existing `ConsolePrinter` / `MultiPrinter`
  / file logging keep working unchanged.
- Model routing to Claude via LiteLLM.
- Backend selection + `ANTHROPIC_API_KEY` wiring in `config.py`.

Out of scope (call out, do not build):

- Claude CLI / subscription-auth backend.
- Custom / MCP tools (crustify has none today).
- Handoffs between crustify stages (each stage stays a single agent).
- Guardrails.
- Replicating the relentless multi-session summarizer loop verbatim
  (see risks).

## 3. Design

### 3.1 The seam

Extract a protocol; the call site at `base.py:167-178` becomes one call.

```
class Backend(Protocol):
    def run(
        self,
        *,
        name: str,
        model: str,
        prompt: str,
        arguments: dict,
        tools: list,        # abstract tool descriptors, not kiss callables
        work_dir: str,
        printer: Printer | None,
    ) -> None: ...
```

`CrustifyAgent.run()` selects the backend from config and calls
`backend.run(...)`. `RelentlessBackend` wraps the current kiss code verbatim.

### 3.2 Tools

The four tools become Agents SDK function tools (`@function_tool`), each with
a typed signature + docstring so the SDK can generate the JSON schema. The
implementations can wrap the existing kiss `UsefulTools` callables (same
in-process execution model crustify uses today) or be thin re-implementations
bound to `work_dir`. Either way the tools run client-side, unsandboxed --
identical risk profile to today. No `finish` tool is needed: the SDK's run
loop terminates on the model's final message, and crustify gates on disk
artifacts anyway.

### 3.3 Model routing

`MODEL_OVERRIDE or self.model` maps to a LiteLLM model string via a small
name table (crustify model id -> `anthropic/<id>`), then:

```
from agents.extensions.models.litellm_model import LitellmModel
model = LitellmModel(model=litellm_id, api_key=os.environ["ANTHROPIC_API_KEY"])
```

Requests hit `api.anthropic.com` directly, in-process (LiteLLM is a
client-side translation shim, not a proxy).

### 3.4 Run loop and continuation

Each stage is one `Runner.run_streamed(agent, task, max_turns=...)`. This
replaces the relentless multi-sub-session loop. Key difference: the Agents
SDK does NOT auto-compact context the way the Claude CLI does, and it does
not run kiss's trajectory-summarize-and-restart continuation. For bounded
stages this is fine; for long stages it is a real gap (see risks).

There is no native dollar-budget cap. We enforce turn count via `max_turns`
and read token usage from the run result for logging/accounting.

### 3.5 Output / Printer adapter

`Runner.run_streamed(...)` yields a typed event stream. We map it onto the
existing `Printer` interface:

- assistant text deltas   -> `printer.token_callback(...)`
- reasoning/thinking      -> `printer.thinking_callback(...)`  (best-effort)
- tool-call items         -> `printer.print(type="tool_call", tool_input=...)`
- tool-output items       -> `printer.print(type="tool_result", ...)`
- final output + usage    -> `printer.print(type="result", step_count=...,
                                total_tokens=..., cost=...)`

Only this adapter is new. `_make_printer()` and every existing Printer are
untouched.

### 3.6 Config and tracing

- `config.py`: add `BACKEND: str = "relentless"` (values: `relentless` |
  `agents_sdk`) and read `ANTHROPIC_API_KEY` from env.
- Tracing: the Agents SDK exports traces to the OpenAI dashboard by default.
  Since we are off OpenAI, disable it (`set_tracing_disabled(True)`) or
  redirect to our own backend. Must be set explicitly.

## 4. Files touched

- `src/crustify/agents/base.py` -- replace `base.py:167-178` with a
  `backend.run(...)` call; select backend from config.
- `src/crustify/agents/backends/__init__.py` -- protocol + selector.
- `src/crustify/agents/backends/relentless.py` -- today's kiss code, moved.
- `src/crustify/agents/backends/agents_sdk.py` -- new backend.
- `src/crustify/agents/backends/agents_sdk_printer.py` -- events -> Printer.
- `src/crustify/config.py` -- `BACKEND`, `ANTHROPIC_API_KEY`.
- deps: add `openai-agents` and `litellm`.

## 5. Risks and open questions

- Long-horizon gap: no auto-compaction and no relentless summarizer loop.
  Mitigation: tune `max_turns`, rely on crustify's already-bounded stage
  sizing (WRAP_MAX_*, PORT_MAX_*), and port a thin continuation shim later
  only if stages actually overflow. NEEDS a real measurement on a large
  target before we trust it.
- No dollar-budget cap: only `max_turns`. We lose the `max_budget` ceiling
  `RelentlessAgent` enforced. Track cost from usage; add a wrapper if needed.
- Streaming/thinking fidelity through LiteLLM is uncertain -- Claude thinking
  and per-token deltas may not surface identically. Treat 3.5 as best-effort
  until verified.
- Structured-output / tool-schema quirks vary by provider through LiteLLM;
  verify the 4 tools' schemas round-trip correctly.
- Dependency weight: `openai-agents` + `litellm` are non-trivial additions.
- Metered billing only; no subscription economics (that is the CLI backend).

## 6. Milestones

1. Backend seam + `RelentlessBackend` -- pure refactor, zero behavior change.
   Verify the full pipeline still runs identically.
2. `AgentsSdkBackend` minimal: non-streaming run, LiteLLM->Claude, 4 tools,
   final-result logging only.
3. Streaming adapter for Printer parity (section 3.5).
4. Config/env + tracing-disable + docs, then a single-stage smoke test
   against one small target and a side-by-side diff vs the relentless
   backend on the same target.

## 7. Validation target

Proposed smoke-test target for milestone 4: run one stage (e.g. the
type-wrapper or a small port) against a single leaf module and compare the
produced `.crustify/` artifact and the generated `.rs` against the relentless
backend's output on the same input. Record the exact `.rs` file the
experiment writes so the run is reproducible.
