"""Pluggable agent backends for a crustify pipeline stage.

A ``Backend`` abstracts the single call site in
:meth:`crustify.agents.base.CrustifyAgent.run` that actually drives an LLM
agent to fill a prompt.

Each backend shells out to a provider CLI, one subprocess per agent, and
streams its stdout into the agent's :class:`~crustify.agentlog.AgentLog`.
Running the agent out-of-process is what makes per-agent accounting exact:
the provider reports usage for that invocation and nothing else, so
concurrent agents under ``--parallel`` cannot interleave their numbers.

The contract is deliberately thin, matching what crustify actually needs:
render a prompt, run an agent confined to a shell tool in a work dir, and
stream its output to a log. Stage completion is judged by the caller via
on-disk artifacts, so a backend's return value is discarded.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from crustify.agentlog import AgentLog


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
        log: AgentLog,
    ) -> None:
        """Drive one agent to completion.

        The prompt is ``prompt_template.format(**arguments)``; ``model`` is the
        resolved model name (``config.MODEL_OVERRIDE`` or the agent default);
        the agent's shell tool runs in ``work_dir``. The return value is
        intentionally unused -- success is judged by on-disk artifacts.
        """
        ...


def get_backend(name: str) -> Backend:
    """Resolve a backend by its ``config.BACKEND`` value."""
    if name == "claude_cli":
        from crustify.agents.backends.claude_cli import ClaudeCliBackend
        return ClaudeCliBackend()
    if name == "codex_cli":
        from crustify.agents.backends.codex_cli import CodexCliBackend
        return CodexCliBackend()
    raise ValueError(
        f"Unknown backend {name!r}; expected one of: claude_cli, codex_cli."
    )
