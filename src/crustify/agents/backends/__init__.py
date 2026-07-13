"""Pluggable agent backends for a crustify pipeline stage.

A ``Backend`` abstracts the single call site in
:meth:`crustify.agents.base.CrustifyAgent.run` that actually drives an LLM
agent to fill a prompt. Two implementations exist:

  - ``agents_sdk`` (default) -- the OpenAI Agents SDK, routing to Claude via
    LiteLLM or to OpenAI models natively. Pure in-process, API-key auth.
  - ``relentless`` -- the original kiss ``RelentlessAgent`` path, kept as a
    selectable fallback (``config.BACKEND = "relentless"``).

The contract is deliberately thin, matching what crustify actually needs:
render a prompt, run an agent with the four generic file/shell tools in a
work dir, and stream output to a Printer. Stage completion is judged by the
caller via on-disk artifacts, so a backend's return value is discarded.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from kiss.core.printer import Printer


@runtime_checkable
class Backend(Protocol):
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
        """Drive one agent to completion.

        The prompt is ``prompt_template.format(**arguments)``; ``model`` is the
        resolved model name (``config.MODEL_OVERRIDE`` or the agent default);
        the four generic tools run against ``work_dir``. The return value is
        intentionally unused -- success is judged by on-disk artifacts.
        """
        ...


def get_backend(name: str) -> Backend:
    """Resolve a backend by its ``config.BACKEND`` value."""
    if name == "agents_sdk":
        from crustify.agents.backends.agents_sdk import AgentsSdkBackend
        return AgentsSdkBackend()
    if name == "relentless":
        from crustify.agents.backends.relentless import RelentlessBackend
        return RelentlessBackend()
    raise ValueError(
        f"Unknown backend {name!r}; expected 'agents_sdk' or 'relentless'."
    )
