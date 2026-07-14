"""OpenAI Agents SDK backend.

Drives one crustify stage with the OpenAI Agents SDK. Claude models route
through LiteLLM to the Anthropic API (the same key/endpoint kiss's
``AnthropicModel`` uses); OpenAI models (``gpt-*``, ``o*``) run natively via
the SDK's default provider. The four generic file/shell tools are exposed as
SDK function tools bound to the agent's work dir, and the SDK's streaming
event feed is adapted onto crustify's existing kiss ``Printer`` so the
console/file logging surface is unchanged.

No cost/budget cap is enforced (crustify's design choice); ``_MAX_TURNS`` is a
generous runaway guard against an infinite tool loop, not a budget.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from agents import Agent, ModelSettings, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from openai.types.shared import Reasoning

from kiss.core.printer import Printer

# Running Claude via LiteLLM means we are off the OpenAI tracing backend;
# disable trace export so the SDK does not try to upload traces to OpenAI.
set_tracing_disabled(True)


# ---------------------------------------------------------------------------
# Cost / cache-token collection (LiteLLM path only)
# ---------------------------------------------------------------------------
#
# LiteLLM already COMPUTES the dollar cost (cache-aware, from its own pricing
# table) and attaches it to each response as `_hidden_params["response_cost"]`;
# the provider's cache-token split (`cache_read_input_tokens` /
# `cache_creation_input_tokens`) rides on the usage. The Agents SDK's wrapped
# `ModelResponse` drops both (it keeps only `input/output/total` + `cached`), so
# we harvest them where they still exist: a LiteLLM success callback. This is a
# pure fetch of LiteLLM's numbers -- no pricing math here. It fires ONLY for the
# LiteLLM (Claude) path; the native OpenAI path never routes through LiteLLM, so
# for it the accumulator stays empty and cost is reported as N/A.
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402

# LiteLLM dispatches success callbacks on a shared WORKER thread, so per-agent
# attribution can't use thread-local state. crustify's default analyze runs ONE
# agent per process (single agent over all dirs), so a module-global accumulator
# reset at each `run()` is correct for that case. Under `--parallel` (several
# agents in one process) the numbers interleave -- cost is then only meaningful
# per-process, which is called out at the report site.
_COST_LOCK = _threading.Lock()
_COST: dict = {}


def _cost_reset() -> None:
    with _COST_LOCK:
        _COST.clear()
        _COST.update(cost=0.0, input=0, output=0,
                     cache_read=0, cache_creation=0, calls=0,
                     t0=_time.monotonic())


def _cost_acc() -> dict:
    with _COST_LOCK:
        return dict(_COST)


def _cost_collect(kwargs, response_obj) -> None:
    """Harvest LiteLLM's own `response_cost` (cache-aware, from `kwargs`) plus the
    cache-token split off the response usage. Pure fetch; no pricing math."""
    try:
        cost = kwargs.get("response_cost")
        u = getattr(response_obj, "usage", None)
        with _COST_LOCK:
            if not _COST:
                return
            _COST["cost"] += float(cost or 0.0)
            _COST["calls"] += 1
            if u is not None:
                _COST["input"] += int(getattr(u, "prompt_tokens", 0) or 0)
                _COST["output"] += int(getattr(u, "completion_tokens", 0) or 0)
                _COST["cache_read"] += int(getattr(u, "cache_read_input_tokens", 0) or 0)
                _COST["cache_creation"] += int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    except Exception:  # pragma: no cover - never let logging break a run
        pass


try:
    from litellm.integrations.custom_logger import CustomLogger as _LLLogger
except Exception:  # pragma: no cover - litellm shape drift
    _LLLogger = object


class _CostCollector(_LLLogger):
    """LiteLLM success logger. The SDK drives Claude via async `acompletion`, for
    which LiteLLM invokes `async_log_success_event` (the sync `success_callback`
    function form does NOT fire); the sync method covers any non-streaming path.
    Arg names must match LiteLLM's keyword call exactly."""

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _cost_collect(kwargs, response_obj)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _cost_collect(kwargs, response_obj)


def _register_cost_collector() -> None:
    try:
        import litellm
        if not any(isinstance(cb, _CostCollector) for cb in (litellm.callbacks or [])):
            litellm.callbacks = list(litellm.callbacks or []) + [_CostCollector()]
    except Exception:  # pragma: no cover
        pass


_register_cost_collector()


async def _flush_cost_callbacks() -> None:
    """LiteLLM schedules its async success callback as a loop task that lands
    just AFTER each `acompletion` returns; the final call's callback races the
    stream end. Yield until the accumulator's `calls` stops growing (stable for
    two 0.1s ticks) so the last cost is captured, bounded to ~3s."""
    prev, stable = -1, 0
    for _ in range(30):
        await asyncio.sleep(0.1)
        cur = _cost_acc().get("calls", 0)
        if cur and cur == prev:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        prev = cur

# Runaway guard (NOT a budget cap): the SDK defaults to 10 turns, which would
# truncate crustify tasks mid-run, so we set it high enough never to bind in
# practice while still stopping a stuck agent from looping forever.
_MAX_TURNS = 1000
_BASH_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

# Anthropic beta that makes the model emit between-tool-call reasoning as
# `thinking` content blocks (rather than plain text blocks).
_ANTHROPIC_INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"
# Thinking needs headroom under max_tokens; mirror kiss's per-family defaults.
_CLAUDE_MAX_TOKENS_OPUS = 65536
_CLAUDE_MAX_TOKENS_OTHER = 64000
_CLAUDE_THINKING_BUDGET = 10000


def _normalize(model_name: str) -> str:
    """Strip kiss's ``codex/`` prefix -- this backend calls the OpenAI API
    directly, it has no Codex CLI."""
    return model_name[len("codex/"):] if model_name.startswith("codex/") else model_name


def _is_claude(model_name: str) -> bool:
    return model_name.startswith(("claude", "anthropic/"))


def _opus_minor(model_name: str) -> int | None:
    """The N in ``claude-opus-4-N``, else None."""
    bare = model_name.split("/", 1)[-1]
    prefix = "claude-opus-4-"
    if not bare.startswith(prefix):
        return None
    try:
        return int(bare[len(prefix):].split("-", 1)[0])
    except ValueError:
        return None


def _claude_thinking(model_name: str) -> dict:
    """Anthropic extended-thinking config for a Claude model.

    Opus 4.6+ dropped ``thinking.type="enabled"`` in favour of ``"adaptive"``.
    Opus 4.7+ additionally default ``thinking.display`` to ``"omitted"``: the
    response then carries a signature-only thinking block, so the reasoning is
    *billed but never shown*. Opt back in with ``display="summarized"`` --
    without it the printer's thinking panel stays empty on those models.
    """
    minor = _opus_minor(model_name)
    if minor is not None and minor >= 6:
        cfg: dict = {"type": "adaptive"}
        if minor >= 7:
            cfg["display"] = "summarized"
        return cfg
    return {"type": "enabled", "budget_tokens": _CLAUDE_THINKING_BUDGET}


def _resolve_model(model_name: str) -> Any:
    """Map a normalized model name onto an Agents SDK model.

    Claude names route through LiteLLM to the Anthropic API; OpenAI names run
    natively via the SDK's default provider (``OPENAI_API_KEY``).
    """
    if _is_claude(model_name):
        litellm_id = (
            model_name if model_name.startswith("anthropic/") else f"anthropic/{model_name}"
        )
        # `should_replay_reasoning_content` is left unset: it takes a *predicate*
        # (not a bool), and the SDK's default already replays a model's own
        # thinking blocks back across tool turns, which is what Anthropic wants.
        return LitellmModel(
            model=litellm_id,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
    return model_name


def _model_settings(model_name: str) -> ModelSettings:
    """Per-provider settings, chiefly how each surfaces its reasoning.

    Anthropic wants an explicit ``thinking`` block (passed through LiteLLM via
    ``extra_args``); OpenAI wants ``reasoning.summary``. Both opt into
    streaming usage so token counts are reported.
    """
    if _is_claude(model_name):
        bare = model_name.split("/", 1)[-1]
        max_tokens = (
            _CLAUDE_MAX_TOKENS_OPUS if bare.startswith("claude-opus-4")
            else _CLAUDE_MAX_TOKENS_OTHER
        )
        return ModelSettings(
            max_tokens=max_tokens,
            include_usage=True,
            extra_args={
                "thinking": _claude_thinking(model_name),
                # Anthropic never caches implicitly (unlike OpenAI): without an
                # explicit breakpoint the whole prompt is re-billed at full rate
                # every turn. Crustify prompts inline AGENTS.md + the skill index,
                # so the resent prefix dominates cost; cache reads bill at ~10%.
                #
                # Two breakpoints, both required, and well inside Anthropic's
                # limit of four:
                #
                #  index 1  -- the task prompt (index 0 is the short `instructions`
                #     system turn). Never changes, so every later turn re-reads
                #     this same cached prefix. A bare top-level `cache_control`
                #     would instead mark the whole *growing* prompt, writing a
                #     fresh entry each turn and never reading one.
                #
                #  index -1 -- the last message, i.e. the newest tool result. This
                #     rolls forward each turn so the accumulated conversation
                #     (tool outputs, which dominate a long wrap) is cached too.
                #     Without it, uncached input grows linearly with every tool
                #     call. Only two breakpoints are needed even across many
                #     turns because Anthropic matches the longest previously
                #     cached prefix, whether or not that exact block still
                #     carries a breakpoint.
                #
                # Targeting by `role` instead would mark every user message and
                # blow the four-breakpoint limit.
                "cache_control_injection_points": [
                    {"location": "message", "index": 1,
                     "control": {"type": "ephemeral"}},
                    {"location": "message", "index": -1,
                     "control": {"type": "ephemeral"}},
                ],
            },
            extra_headers={"anthropic-beta": _ANTHROPIC_INTERLEAVED_BETA},
        )
    return ModelSettings(
        # OpenAI exposes a reasoning *summary*, never raw reasoning tokens.
        reasoning=Reasoning(effort="medium", summary="auto"),
        include_usage=True,
    )


# ---------------------------------------------------------------------------
# Tools -- the four generic file/shell tools, bound to work_dir
# ---------------------------------------------------------------------------

def _build_tools(work_dir: str) -> list:
    """The four generic tools bound to *work_dir*, as SDK function tools.

    Tool names match the kiss tool names (Bash/Read/Edit/Write) so prompts
    that reference tools by name stay accurate. Relative paths resolve against
    the work dir; the Read output mirrors the ``cat -n`` line-numbered format
    crustify prompts expect, and Edit does a single exact-match replacement.
    """
    root = Path(work_dir)

    def _resolve(file_path: str) -> Path:
        p = Path(file_path)
        return p if p.is_absolute() else root / p

    @function_tool(name_override="Bash")
    def bash(command: str) -> str:
        """Run a shell command in the agent's working directory and return its
        combined stdout+stderr.

        Args:
            command: The shell command to execute.
        """
        try:
            proc = subprocess.run(
                command, shell=True, cwd=work_dir,
                capture_output=True, text=True, timeout=_BASH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"(command timed out after {_BASH_TIMEOUT_S}s)"
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            out += f"\n(exit code {proc.returncode})"
        return out or "(no output)"

    @function_tool(name_override="Read")
    def read(file_path: str) -> str:
        """Read a file and return its contents with 1-based line numbers.

        Args:
            file_path: File path (absolute, or relative to the work dir).
        """
        p = _resolve(file_path)
        try:
            text = p.read_text()
        except Exception as exc:
            return f"(error reading {file_path}: {exc})"
        lines = text.splitlines()
        width = len(str(len(lines))) if lines else 1
        numbered = "\n".join(f"{i:>{width}}\t{ln}" for i, ln in enumerate(lines, 1))
        return numbered or "(empty file)"

    @function_tool(name_override="Write")
    def write(file_path: str, content: str) -> str:
        """Write content to a file, creating parent directories as needed and
        overwriting any existing file.

        Args:
            file_path: File path (absolute, or relative to the work dir).
            content: The full file content to write.
        """
        p = _resolve(file_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        except Exception as exc:
            return f"(error writing {file_path}: {exc})"
        return f"Wrote {len(content)} bytes to {file_path}"

    @function_tool(name_override="Edit")
    def edit(file_path: str, old_string: str, new_string: str) -> str:
        """Replace the first exact occurrence of old_string with new_string.

        Args:
            file_path: File path (absolute, or relative to the work dir).
            old_string: Exact text to find (should match a single location).
            new_string: Replacement text.
        """
        p = _resolve(file_path)
        try:
            text = p.read_text()
        except Exception as exc:
            return f"(error reading {file_path}: {exc})"
        if old_string not in text:
            return f"(old_string not found in {file_path})"
        p.write_text(text.replace(old_string, new_string, 1))
        return f"Edited {file_path}"

    return [bash, read, write, edit]


# ---------------------------------------------------------------------------
# Event -> Printer adapter
# ---------------------------------------------------------------------------

def _describe_tool_call(item: Any) -> tuple[str, dict]:
    """Extract (tool_name, tool_input dict) from a ToolCallItem."""
    raw = getattr(item, "raw_item", None)
    tool_name = getattr(raw, "name", None) or "tool"
    args_raw = getattr(raw, "arguments", None)
    if isinstance(args_raw, dict):
        return tool_name, args_raw
    if isinstance(args_raw, str):
        try:
            return tool_name, json.loads(args_raw)
        except Exception:
            return tool_name, {"arguments": args_raw}
    return tool_name, {}


class AgentsSdkBackend:
    def run(
        self,
        *,
        name: str,
        model: str,
        prompt_template: str,
        arguments: dict,
        work_dir: str,
        printer: Printer | None,
    ) -> None:
        task = prompt_template.format(**arguments) if arguments else prompt_template
        _cost_reset()  # fresh per-agent cost/cache accumulator (LiteLLM path)
        model_id = _normalize(model)
        agent = Agent(
            name=name,
            # kiss keeps the crustify prompt as the user turn and puts only a
            # work-dir note in the system prompt; mirror that split here.
            instructions=(
                f"Your working directory is {work_dir}. Use the Bash, Read, "
                f"Write, and Edit tools to inspect and modify files there."
            ),
            model=_resolve_model(model_id),
            model_settings=_model_settings(model_id),
            tools=_build_tools(work_dir),
        )
        asyncio.run(self._drive(agent, task, printer, model_id))

    # Raw-event types whose `.delta` carries reasoning/thinking text (OpenAI
    # emits the summary variant; Anthropic-via-LiteLLM the text variant).
    _REASONING_DELTA_TYPES = (
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    )

    async def _drive(self, agent: Agent, task: str, printer: Printer | None,
                     model_id: str = "") -> None:
        self._in_thinking = False
        if printer:
            printer.print(task, type="prompt")
        result = Runner.run_streamed(agent, task, max_turns=_MAX_TURNS)
        async for ev in result.stream_events():
            if printer:
                self._handle_event(ev, printer)
        if _is_claude(model_id):
            await _flush_cost_callbacks()
        if printer:
            self._set_thinking(printer, False)
            self._emit_result(result, printer, model_id)

    def _set_thinking(self, printer: Printer, on: bool) -> None:
        """Open/close a thinking block on the printer, only on state change.

        The ConsolePrinter routes ``token_callback`` text to its thinking or
        message panel based on these boundaries, so reasoning and answer text
        stay visually separated.
        """
        if on != self._in_thinking:
            self._in_thinking = on
            printer.thinking_callback(on)

    def _handle_event(self, ev: Any, printer: Printer) -> None:
        etype = getattr(ev, "type", "")
        if etype == "raw_response_event":
            data = getattr(ev, "data", None)
            dtype = getattr(data, "type", "")
            # Assistant answer text -- close any open thinking block first.
            if dtype == "response.output_text.delta":
                delta = getattr(data, "delta", "")
                if delta:
                    self._set_thinking(printer, False)
                    printer.token_callback(delta)
            # Reasoning/thinking text -- open a thinking block.
            elif dtype in self._REASONING_DELTA_TYPES:
                delta = getattr(data, "delta", "")
                if delta:
                    self._set_thinking(printer, True)
                    printer.token_callback(delta)
            return
        if etype == "run_item_stream_event":
            # A tool call or completed message ends any open thinking block.
            self._set_thinking(printer, False)
            name = getattr(ev, "name", "")
            item = getattr(ev, "item", None)
            if name == "tool_called":
                tool_name, tool_input = _describe_tool_call(item)
                printer.print(tool_name, type="tool_call", tool_input=tool_input)
            elif name == "tool_output":
                printer.print(str(getattr(item, "output", "")), type="tool_result")

    @staticmethod
    def _usage_tokens(usage: Any) -> int:
        if not usage:
            return 0
        total = int(getattr(usage, "total_tokens", 0) or 0)
        if not total:
            # Some providers (e.g. Anthropic via LiteLLM) report input/output
            # separately without a rolled-up total.
            total = int(getattr(usage, "input_tokens", 0) or 0) + int(
                getattr(usage, "output_tokens", 0) or 0
            )
        return total

    @classmethod
    def _emit_result(cls, result: Any, printer: Printer, model: str = "") -> None:
        total_tokens = 0
        try:
            total_tokens = cls._usage_tokens(result.context_wrapper.usage)
        except Exception:
            pass
        if not total_tokens:
            # LiteLLM-backed models may not roll usage into the run context;
            # fall back to summing per-response usage.
            try:
                total_tokens = sum(
                    cls._usage_tokens(getattr(r, "usage", None))
                    for r in (getattr(result, "raw_responses", None) or [])
                )
            except Exception:
                pass
        acc = _cost_acc() or {}
        t0 = acc.get("t0")
        # Per-run wall-clock (reset at run() start). Accurate per-agent for the
        # default sequential runs; under `--parallel` it interleaves like cost.
        dur = f"  |  {_time.monotonic() - t0:.1f}s" if t0 is not None else ""
        if acc.get("calls"):
            # LiteLLM (Claude) path: LiteLLM's own response_cost -- exact and
            # cache-aware (reads AND creation). Full breakdown on its own
            # (unclipped) usage line; the result-panel subtitle is fixed-width, so
            # it carries only the short $cost.
            inp, out = acc["input"], acc["output"]
            cr, cw = acc["cache_read"], acc["cache_creation"]
            total_tokens = inp + out
            cost = f"${acc['cost']:.4f}"
            printer.print(
                f"prompt {inp:,} (cache_rd {cr:,}, cache_wr {cw:,}) + "
                f"out {out:,}  |  cost {cost}{dur}",
                type="usage_info",
            )
        else:
            # Native (OpenAI) path: no LiteLLM callback. Price the SDK usage off
            # LiteLLM's model_cost map (rates owned by LiteLLM, not maintained
            # here). OpenAI caching is reads-only, and cost_per_token subtracts
            # cache_read from prompt_tokens itself, so pass the full prompt +
            # cached count.
            cost = cls._price_from_usage(model, result, printer, dur)
            if cost == "N/A" and dur:
                printer.print(f"wall-clock{dur}", type="usage_info")
        printer.print(
            str(getattr(result, "final_output", "") or ""),
            type="result",
            total_tokens=total_tokens,
            cost=cost,
        )

    @staticmethod
    def _price_from_usage(model: str, result: Any, printer: Printer,
                          dur: str = "") -> str:
        """Cost for the native (non-LiteLLM) path, priced via LiteLLM's
        `cost_per_token`. Prices **per request** (`usage.request_usage_entries`)
        and sums -- NOT the aggregated usage: token-tier thresholds (e.g.
        gpt-5.5's >272k-token 2x rate) are per-request, so pricing the summed
        tokens would mis-apply them. Falls back to the aggregate if per-request
        entries are unavailable. Returns a `$x.xxxx` string or 'N/A'."""
        def _one(inp: int, out: int, cached: int):
            import litellm
            for cand in dict.fromkeys((model, model.rsplit("/", 1)[-1])):
                try:
                    pc, cc = litellm.cost_per_token(
                        model=cand, prompt_tokens=inp, completion_tokens=out,
                        cache_read_input_tokens=cached)
                    return pc + cc
                except Exception:
                    continue
            return None

        def _toks(e):
            inp = int(getattr(e, "input_tokens", 0) or 0)
            out = int(getattr(e, "output_tokens", 0) or 0)
            cached = int(getattr(getattr(e, "input_tokens_details", None),
                                 "cached_tokens", 0) or 0)
            return inp, out, cached

        try:
            u = result.context_wrapper.usage
            entries = getattr(u, "request_usage_entries", None) or []
            srcs = entries or [u]           # per-request, else the aggregate
            t_in = t_out = t_cached = 0
            total = 0.0
            priced = False
            for e in srcs:
                inp, out, cached = _toks(e)
                t_in += inp; t_out += out; t_cached += cached
                c = _one(inp, out, cached)
                if c is not None:
                    total += c
                    priced = True
            if not (t_in or t_out) or not priced:
                return "N/A"
            cost = f"${total:.4f}"
            printer.print(
                f"prompt {t_in:,} (cache_rd {t_cached:,}) + out {t_out:,}"
                f"  |  cost {cost}  ({len(srcs)} req){dur}",
                type="usage_info",
            )
            return cost
        except Exception:
            return "N/A"
