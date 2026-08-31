"""Rates and per-request pricing, shared by both tools.

Rates are FETCHED, never written down here. A constant in this file would go
stale exactly the way a model's recollection does, only slower and with more
authority -- which is how three campaigns published figures 3x too high. An
unknown service or model is left UNPRICED by the caller: never zero, never
guessed, because a plausible wrong number is worse than a missing one.

Reconciled from the two copies that had grown apart. `_rate_set`,
`_fetch_openrouter` and `_from_litellm` were the same on both sides. The other
two were not, and each side had the better half: `load_prices` here is the
audit copy, which adds `refresh` and stops `dirname("")` raising on a bare
filename; `price_request` keeps the translation copy's tolerant `.get`, because
its caller reads raw request records that need not carry every key. Neither
difference was deliberate -- which is the argument for one copy.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
LITELLM_PRICES = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                  "model_prices_and_context_window.json")
DEFAULT_PRICE_CACHE = os.path.expanduser("~/.cache/crustify/model-prices.json")

_TIER_RE = re.compile(r"_above_(\d+)k_tokens$")


def _rate_set(entry: dict) -> dict:
    """One LiteLLM entry -> base rates plus context tiers, in $/token."""
    field = {
        "input_cost_per_token": "input",
        "output_cost_per_token": "output",
        "cache_read_input_token_cost": "cache_read",
        "cache_creation_input_token_cost": "cache_write",
        "cache_creation_input_token_cost_above_1hr": "cache_write_1h",
    }
    base, tiers = {}, {}
    for key, val in entry.items():
        if not isinstance(val, (int, float)):
            continue
        m = _TIER_RE.search(key)
        stem = key[:m.start()] if m else key
        name = field.get(stem)
        if name is None:
            continue
        if m:
            tiers.setdefault(int(m.group(1)) * 1000, {})[name] = float(val)
        else:
            base[name] = float(val)
    return {"base": base, "tiers": sorted(tiers.items())}


def _fetch_openrouter() -> dict:
    with urllib.request.urlopen(OPENROUTER_MODELS, timeout=30) as r:
        data = json.load(r)["data"]
    out = {}
    for m in data:
        p = m.get("pricing") or {}
        base = {}
        for src, name in (("prompt", "input"), ("completion", "output"),
                          ("input_cache_read", "cache_read"),
                          ("input_cache_write", "cache_write")):
            try:
                v = float(p.get(src) or 0.0)
            except (TypeError, ValueError):
                continue
            if v:
                base[name] = v
        if base:
            out[m["id"]] = {"base": base, "tiers": []}
    return out


def _from_litellm(provider: str):
    def fetch():
        with urllib.request.urlopen(LITELLM_PRICES, timeout=60) as r:
            data = json.load(r)
        return {mid: _rate_set(v) for mid, v in data.items()
                if isinstance(v, dict) and v.get("litellm_provider") == provider}
    return fetch


_SOURCES = {
    "openrouter": _fetch_openrouter,
    "openai": _from_litellm("openai"),
    "anthropic": _from_litellm("anthropic"),
}


def load_prices(cache_path: str = DEFAULT_PRICE_CACHE,
                offline: bool = False, refresh: bool = False) -> dict:
    """`{provider: {model_id: rate_set}}`, cached on disk.

    Rates only move when a provider changes them, so the cache is the normal
    path. `--refresh` is how you pick up a newly released model; `--offline`
    refuses to fetch and leaves unknown models unpriced rather than guessing.
    """
    if os.path.exists(cache_path) and not refresh:
        try:
            with open(cache_path) as fh:
                cached = json.load(fh)
            return {prov: {m: {"base": rs["base"],
                               "tiers": [(int(k), v) for k, v in rs["tiers"]]}
                           for m, rs in table.items()}
                    for prov, table in cached.items()}
        except Exception:
            pass
    if offline:
        return {}
    prices = {}
    for prov, fetch in _SOURCES.items():
        try:
            prices[prov] = fetch()
        except Exception as exc:
            print(f"warning: could not fetch {prov} prices ({exc}); "
                  f"{prov}-billed runs will be unpriced", file=sys.stderr)
            prices[prov] = {}
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(prices, fh)
    return prices


def price_request(rate_set: dict, req: dict) -> float:
    """Cost of ONE request, tier chosen by that request's own input context.

    Tiers are per request, so a session must be priced request by request and
    the costs summed. Pricing the summed tokens would push a session of many
    modest requests into a tier none of them ever reached.

    An unpublished cache rate falls back to the full input rate, not to zero:
    assuming a cache read is free understates, and does so silently.
    """
    ctx = (req.get("input_tokens", 0) + req.get("cache_read_tokens", 0)
           + req.get("cache_write_tokens", 0)
           + req.get("cache_write_1h_tokens", 0))
    r = dict(rate_set["base"])
    for threshold, over in rate_set["tiers"]:
        if ctx > threshold:
            r.update(over)
    inp = r.get("input", 0.0)
    return (req.get("input_tokens", 0) * inp
            + req.get("output_tokens", 0) * r.get("output", 0.0)
            + req.get("cache_read_tokens", 0) * r.get("cache_read", inp)
            + req.get("cache_write_tokens", 0) * r.get("cache_write", inp)
            + req.get("cache_write_1h_tokens", 0)
              * r.get("cache_write_1h", r.get("cache_write", inp)))
