"""Model name -> (backend, model id, billing service). Shared by both tools.

Every model is named ``<provider>/<model id>``. The provider is the service
that bills the run; the model id passes to the provider CLI verbatim. Only the
first segment is interpreted, so ids containing slashes (OpenRouter's
``vendor/model`` convention) pass through untouched.

The prefix is mandatory rather than inferred. The same weights are reachable
through more than one service at different prices, and guessing wrong silently
prices a run against the wrong rate table -- the failure this package exists to
prevent. Naming the service also selects the backend, since a model can only be
driven by the CLI that speaks its API.

Raises ``ValueError``, not ``SystemExit``: a library says what is wrong and the
entry point decides whether that ends the process. The two copies this replaces
disagreed on exactly that, which is how you can tell neither chose it.
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
        raise ValueError(
            f"model {model!r} must be named <provider>/<model>, e.g. "
            f"anthropic/claude-opus-5, openai/gpt-5.6, "
            f"openrouter/z-ai/glm-4.6. Known providers: "
            f"{', '.join(sorted(_BACKENDS))}.")
    backend = _BACKENDS.get(provider)
    if backend is None:
        raise ValueError(
            f"unknown provider {provider!r} in model {model!r}; "
            f"expected one of: {', '.join(sorted(_BACKENDS))}.")
    return Route(backend, model_id, provider)
