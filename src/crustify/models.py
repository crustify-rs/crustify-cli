"""Model name -> (backend, model id, billing service).

Every model is named ``<provider>/<model id>``. The provider is the
service that bills the run; the model id is passed to the provider CLI
**verbatim**, exactly as that service names it::

    anthropic/claude-opus-4-8      -> claude CLI, `--model claude-opus-4-8`
    openai/gpt-5.6                 -> codex CLI,  `-m gpt-5.6`
    openrouter/z-ai/glm-4.6        -> codex CLI,  `-m z-ai/glm-4.6`

Only the first segment is interpreted, so ids containing slashes
(OpenRouter's ``vendor/model`` convention) pass through untouched.

The prefix is mandatory rather than inferred. A bare ``gpt-5.6`` or
``z-ai/glm-4.6`` is ambiguous - the same weights are reachable through
more than one service at different prices, and guessing wrong silently
prices a run against the wrong rate table. Naming the service is also
what selects the backend, since a model can only be driven by the CLI
that speaks its API.
"""

from __future__ import annotations

from typing import NamedTuple


class Route(NamedTuple):
    backend: str    # registered name in crustify.agents.backends
    model: str      # id handed to the provider CLI, verbatim
    provider: str   # billing service; selects crustify-log-cost's rate table


# provider -> backend that can drive it.
_BACKENDS = {
    "anthropic": "claude_cli",
    "openai": "codex_cli",
    "openrouter": "codex_cli",
}


def resolve(model: str) -> Route:
    """Route a ``<provider>/<model id>`` name.

    Raises ``ValueError`` when the prefix is missing or names a provider
    crustify has no backend for.
    """
    name = (model or "").strip()
    provider, sep, model_id = name.partition("/")
    if not sep or not model_id:
        raise ValueError(
            f"model {model!r} must be named <provider>/<model>, e.g. "
            f"anthropic/claude-opus-4-8, openai/gpt-5.6, "
            f"openrouter/z-ai/glm-4.6. Known providers: "
            f"{', '.join(sorted(_BACKENDS))}."
        )
    backend = _BACKENDS.get(provider)
    if backend is None:
        raise ValueError(
            f"unknown provider {provider!r} in model {model!r}; "
            f"expected one of: {', '.join(sorted(_BACKENDS))}."
        )
    return Route(backend, model_id, provider)
