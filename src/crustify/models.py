"""Model routing. Thin wrapper over :mod:`crustify.core.models`.

The shared module raises ``ValueError``; a CLI turns that into an exit.
"""
from __future__ import annotations

from crustify.core.models import Route, resolve as _resolve

__all__ = ["Route", "resolve"]


def resolve(model: str) -> Route:
    try:
        return _resolve(model)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
