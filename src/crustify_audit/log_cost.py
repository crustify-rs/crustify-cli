"""log_cost.py — price agent runs from token counts.

The port of crustify-cli's `log_cost.py` that `agentlog` already claimed to
follow. Until it existed, nothing here priced anything: the dollar figures in a
campaign's `results.md` were whatever rate card the reporting agent happened to
remember, and a model released after its training data was priced at the
previous generation's rates. The git2-rs campaign reported $235.78 for a run
that cost $78.60, uniformly 3x, because it used Opus-4-era rates.

So rates are FETCHED, never written down here. A number in this file would go
stale the same way, just more slowly and with more authority. Anthropic and
OpenAI come from LiteLLM's community table -- neither publishes a
machine-readable price endpoint -- and OpenRouter from its own live catalogue.
Cross-sourcing them is an error rather than a shortcut: LiteLLM's OpenRouter
entries lag and omit models, and OpenRouter cannot speak for what Anthropic
charges to bill a run directly.

An unknown service or model is reported UNPRICED, never zero and never guessed.
A plausible wrong number is worse than a missing one -- that is the whole
lesson of the campaigns this module exists to correct.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

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


def normalize(record: dict) -> dict:
    """One Anthropic-shaped usage record -> the fields pricing needs.

    The 5m/1h split matters and is not cosmetic: a 1-hour cache write bills at
    roughly twice a 5-minute one. Records predating the `cache_creation`
    breakdown carry only the flat total, which is charged at the 5m rate --
    the same assumption the provider made when it wrote them.
    """
    cc = record.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", 0)
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    if not (w5 or w1h):
        w5 = record.get("cache_creation_input_tokens", 0)
    return {
        "input_tokens": record.get("input_tokens", 0),
        "output_tokens": record.get("output_tokens", 0),
        "cache_read_tokens": record.get("cache_read_input_tokens", 0),
        "cache_write_tokens": w5,
        "cache_write_1h_tokens": w1h,
    }


def price_request(rate_set: dict, req: dict) -> float:
    """Cost of ONE request, tier chosen by that request's own input context.

    Tiers are per request, so a session must be priced request by request and
    the costs summed. Pricing the summed tokens would push a session of many
    modest requests into a tier none of them ever reached.

    An unpublished cache rate falls back to the full input rate, not to zero:
    assuming a cache read is free understates, and does so silently.
    """
    ctx = (req["input_tokens"] + req["cache_read_tokens"]
           + req["cache_write_tokens"] + req["cache_write_1h_tokens"])
    r = dict(rate_set["base"])
    for threshold, over in rate_set["tiers"]:
        if ctx > threshold:
            r.update(over)
    inp = r.get("input", 0.0)
    return (req["input_tokens"] * inp
            + req["output_tokens"] * r.get("output", 0.0)
            + req["cache_read_tokens"] * r.get("cache_read", inp)
            + req["cache_write_tokens"] * r.get("cache_write", inp)
            + req["cache_write_1h_tokens"]
              * r.get("cache_write_1h", r.get("cache_write", inp)))


def price_agent(path: str | Path, prices: dict,
                provider: str = "", model: str = "") -> tuple[float | None, int, str]:
    """(cost_usd, tokens, model) for one `*.usage.json`.

    `cost_usd` is None when the service or model is unknown to the tables --
    deliberately distinct from 0.0, which means free. Older files carry no
    `model`/`provider`, so the caller may supply them; a file that names its
    own wins over the argument, because the file is evidence and the argument
    is an assumption.
    """
    try:
        with open(path, errors="replace") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None, 0, ""
    if not isinstance(d, dict):
        return None, 0, ""
    reqs = [normalize(r) for r in (d.get("records") or [])]
    tokens = sum(sum(r.values()) for r in reqs)
    model = d.get("model") or model
    provider = d.get("provider") or provider
    rate_set = (prices.get(provider) or {}).get(model)
    if rate_set is None:
        return None, tokens, model
    return sum(price_request(rate_set, r) for r in reqs), tokens, model


def price_logs(logs_dir: str | Path, prices: dict,
               provider: str = "", model: str = "") -> dict:
    """Aggregate one `logs/` directory: total, per-agent, and what went unpriced."""
    agents, total, tokens, unpriced = [], 0.0, 0, 0
    for f in sorted(glob.glob(os.path.join(str(logs_dir), "*.usage.json"))):
        cost, tok, mid = price_agent(f, prices, provider, model)
        agents.append({"file": os.path.basename(f), "cost_usd": cost,
                       "tokens": tok, "model": mid})
        tokens += tok
        if cost is None:
            unpriced += 1
        else:
            total += cost
    return {"total_usd": total, "tokens": tokens, "agents": agents,
            "unpriced": unpriced}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crustify-audit-cost",
        description="Price agent runs from their recorded token counts.")
    ap.add_argument("logs", nargs="+",
                    help="one or more directories holding *.usage.json")
    ap.add_argument("--model", default="claude-opus-5",
                    help="model id for files that do not name one")
    ap.add_argument("--provider", default="anthropic",
                    help="billing service for files that do not name one")
    ap.add_argument("--price-cache", default=DEFAULT_PRICE_CACHE)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch rates even if the cache is warm")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    prices = load_prices(a.price_cache, offline=a.offline, refresh=a.refresh)
    out = {}
    for d in a.logs:
        out[d] = price_logs(d, prices, a.provider, a.model)
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    grand = 0.0
    for d, r in out.items():
        grand += r["total_usd"]
        note = f"  ({r['unpriced']} UNPRICED)" if r["unpriced"] else ""
        print(f"{d}\n  ${r['total_usd']:.2f}  "
              f"{len(r['agents'])} agents  {r['tokens']/1e6:.1f} Mtok{note}")
    if len(out) > 1:
        print(f"\nTOTAL ${grand:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
