"""Model name -> (backend, model id, billing provider).

Lifted from crustify-cli unchanged, because the reasoning is unchanged: every
model is named ``<provider>/<model id>``, the provider is what bills the run,
and the model id passes to the provider CLI verbatim. The prefix is mandatory
rather than inferred -- the same weights are reachable through more than one
service at different prices, and guessing wrong silently prices a run against
the wrong rate table. Naming the service is also what selects the backend,
since a model can only be driven by the CLI that speaks its API.
"""
from __future__ import annotations

from typing import NamedTuple


class Route(NamedTuple):
    backend: str    # registered name in crustify_audit.agents.backends
    model: str      # id handed to the provider CLI, verbatim
    provider: str   # billing service


_BACKENDS = {
    "anthropic": "claude_cli",
    "openai": "codex_cli",
    "openrouter": "codex_cli",
}


def resolve(model: str) -> Route:
    name = (model or "").strip()
    provider, sep, model_id = name.partition("/")
    if not sep or not model_id:
        raise SystemExit(
            f"model {model!r} must be named <provider>/<model>, e.g. "
            f"anthropic/claude-opus-5, openai/gpt-5.6, "
            f"openrouter/z-ai/glm-4.6. Known providers: "
            f"{', '.join(sorted(_BACKENDS))}.")
    backend = _BACKENDS.get(provider)
    if backend is None:
        raise SystemExit(
            f"unknown provider {provider!r} in model {model!r}; "
            f"expected one of: {', '.join(sorted(_BACKENDS))}.")
    return Route(backend, model_id, provider)
