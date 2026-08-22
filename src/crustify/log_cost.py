#!/usr/bin/env python3
"""Cost + wall-clock analysis of crustify agent logs.

Reads the per-agent ``<stage>.usage.json`` files written by
:mod:`crustify.agentlog`: a crustify-shaped record of one agent run,
holding its per-request token counts. Each agent is its own process, so
its file covers exactly one invocation and the numbers never interleave,
even with concurrent agents. (The sibling ``<stage>.log`` is the provider
CLI's own human output; nothing here parses it.)

Every provider is priced the same way, from per-request counts the backend
recovers from the CLI's session transcript. Provider-reported dollars are
deliberately unused even where available (claude's ``total_cost_usd``):
under subscription auth that figure is the API-equivalent price rather
than the billed one, so mixing it with computed figures would make runs
incomparable across providers.

Rates are per-service, so a model id is only priceable together with the
service that billed it, and each service is read from its own
authoritative source:

  ``openrouter``  OpenRouter's live catalogue (``/api/v1/models``).
  ``openai``      LiteLLM's community price table. OpenAI publishes rates
                  as a docs page, not a machine-readable endpoint -
                  ``/v1/models`` carries no pricing - so there is nothing
                  first-party to query.

Cross-sourcing the two is an error rather than a shortcut: LiteLLM's
OpenRouter entries lag the live catalogue and omit models outright, while
OpenRouter cannot speak for what OpenAI charges to bill a run directly.
A model priced from the wrong service's table yields a plausible wrong
number, which is worse than none — so an unrecognised service or model is
counted and reported under ``no-price`` instead.

Both tables are fetched once and cached to ``--price-cache``.

Two views:
  * per agent KIND  (port / wrap / merge / setup) — kind from
    the log filename prefix; wall-clock = the record's ``duration_ms``,
    counted under ``no-wall`` when the record predates that stamp.
  * per WAVE        — session dirs mapped to the wave whose commit
    immediately follows the dir's merge mtime; cost split by agent kind.

Usage:  crustify-log-cost <repo_root> [--target ssl/statem] [--offline]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
# OpenAI publishes pricing as a docs page, not a machine-readable endpoint
# (`/v1/models` carries no rates), so its numbers come from LiteLLM's
# community-maintained table — the same source crustify used to price the
# native path through `litellm.cost_per_token`, now fetched as plain JSON
# rather than pulled in as a dependency.
LITELLM_PRICES = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                  "model_prices_and_context_window.json")
DEFAULT_PRICE_CACHE = os.path.expanduser("~/.cache/crustify/model-prices.json")


# ------------------------------------------------------------------ pricing
#
# Rates are per-token but not flat: several models bill a higher rate once
# a *single request's* input context crosses a threshold (OpenAI's 272k
# tier on gpt-5.5/5.6, Anthropic's 200k tier on Sonnet), and Anthropic
# charges more for a 1-hour cache write than a 5-minute one. Thresholds
# apply per request, so a session's tokens must be priced request by
# request and the costs summed - pricing the summed tokens would push a
# session of many small requests into a tier none of them reached.

_TIER_RE = re.compile(r"_above_(\d+)k_tokens$")


def _rate_set(entry):
    """Normalise one LiteLLM entry into base rates plus context tiers.

    Returns ``{"base": {...}, "tiers": [(threshold_tokens, {...}), ...]}``
    where each rate map holds ``input`` / ``output`` / ``cache_read`` /
    ``cache_write`` / ``cache_write_1h`` in $/token, absent keys meaning
    "not published".
    """
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


def _fetch_openrouter():
    """Rate sets from OpenRouter's live catalogue. OpenRouter publishes one
    flat rate per model, so these carry no context tiers."""
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


def _from_litellm(provider):
    """Rate sets for one ``litellm_provider``, tiers included."""
    def fetch():
        with urllib.request.urlopen(LITELLM_PRICES, timeout=60) as r:
            data = json.load(r)
        return {mid: _rate_set(v) for mid, v in data.items()
                if isinstance(v, dict) and v.get("litellm_provider") == provider}
    return fetch


# Each service is priced from its own authoritative source. Cross-sourcing
# them is not a shortcut but an error: LiteLLM's OpenRouter entries lag
# OpenRouter's live catalogue (and omit models entirely), while OpenRouter
# cannot speak for what OpenAI or Anthropic charge to bill a run directly.
_SOURCES = {
    "openrouter": _fetch_openrouter,
    "openai": _from_litellm("openai"),
    "anthropic": _from_litellm("anthropic"),
}


def load_prices(cache_path, offline=False):
    """``{provider: {model_id: rate_set}}`` — see :func:`_rate_set`.

    Cached on disk because rates only move when a provider changes them;
    ``--offline`` refuses to fetch and uses whatever is cached (empty if
    nothing is, which reports those rows unpriced rather than guessing).
    """
    if os.path.exists(cache_path):
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
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump(prices, fh)
    return prices


def price_request(rate_set, req):
    """Cost of ONE request, tier selected by that request's input context.

    Unpublished cache rates fall back to the full input rate rather than
    to zero: assuming a cache read is free understates, and silently.
    """
    ctx = (req.get("input_tokens", 0) + req.get("cache_read_tokens", 0)
           + req.get("cache_write_tokens", 0) + req.get("cache_write_1h_tokens", 0))
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


# ------------------------------------------------------------- log parsing

def parse_usage(path, prices):
    """(cost_usd, tokens, model) for one agent, or None if the file is
    missing or malformed (agent died before reporting).

    Prices request by request and sums the costs — never the reverse. Tier
    thresholds apply to a single request's context, so a session of many
    modest requests must not be charged as one large one.

    ``cost_usd`` is None when the service or model is unknown to the price
    tables — distinct from 0.0, which means free.
    """
    try:
        with open(path, errors="replace") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None

    reqs = d.get("requests") or []
    tokens = sum(r.get("input_tokens", 0) + r.get("output_tokens", 0)
                 + r.get("cache_read_tokens", 0)
                 + r.get("cache_write_tokens", 0)
                 + r.get("cache_write_1h_tokens", 0) for r in reqs)
    model = d.get("model", "")
    rate_set = (prices.get(d.get("provider") or "") or {}).get(model)
    if rate_set is None:
        return None, tokens, model
    return sum(price_request(rate_set, r) for r in reqs), tokens, model


def kind(fn):
    for p, k in (# `translate` tags each agent <objective>-<unit>_<key>, so the
                 # bucket is the pair — which is the point: it prices wrap-type
                 # against port-type directly, the calibration question a first
                 # port wave exists to answer. Longest prefixes first.
                 ("wrap-type", "wrap-type"), ("wrap-symbol", "wrap-symbol"),
                 ("port-type", "port-type"), ("port-symbol", "port-symbol"),
                 ("review-type", "review-type"),
                 ("review-symbol", "review-symbol"),
                 # Historical pre-`translate` log prefixes remain bucketed so
                 # existing campaign measurements stay readable.
                 ("port_", "port"), ("wrap_", "wrap"), ("merge", "merge"),
                 # Historical setup-agent prefixes share one compatibility
                 # bucket so old campaign measurements remain readable.
                 ("scaffolder", "setup"),
                 ("type_analyzer", "setup"),
                 ("symbol_analyzer", "setup"), ("buffer", "setup"),
                 ("bindgen", "setup")):
        if fn.startswith(p):
            return k
    return "other"


def stat(path, fmt):  # %W birth, %Y mtime
    out = subprocess.run(["stat", "-f" if sys.platform == "darwin" else "-c",
                          fmt, path], capture_output=True, text=True).stdout
    try:
        return int(out.strip())
    except ValueError:
        return 0


def wall_seconds(usage_path):
    """This agent's wall clock in seconds, or ``None`` when unrecorded.

    ``duration_ms`` is stamped by :mod:`crustify.agentlog` around the
    subprocess. Records written before that stamp existed carry no timing and
    return ``None`` -- counted as unmeasured rather than folded in as zero,
    which would understate every total silently. (This column read ``0h00m``
    for every run in exactly that way: it used to derive the span from this
    file's own birth -> mtime, but ``usage.json`` is written once at the end,
    so the two are the same instant.)
    """
    try:
        with open(usage_path, errors="replace") as fh:
            ms = json.load(fh).get("duration_ms")
    except (OSError, ValueError, AttributeError):
        return None
    return ms / 1000.0 if isinstance(ms, (int, float)) and ms >= 0 else None


def hm(s):
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# ------------------------------------------------------------------- views

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_root")
    ap.add_argument("--target", default=None,
                    help="Repo-relative target (default: every target found).")
    ap.add_argument("--offline", action="store_true",
                    help="Never fetch OpenRouter prices; use the cache only.")
    ap.add_argument("--price-cache", default=DEFAULT_PRICE_CACHE)
    args = ap.parse_args()

    prices = load_prices(args.price_cache, offline=args.offline)

    tdir = args.target if args.target else "**"
    log_glob = os.path.join(args.repo_root, "crustify", "targets", tdir,
                            "logs", "**", "*.usage.json")

    kc = defaultdict(float); kr = defaultdict(int)
    kw = defaultdict(float); kn = defaultdict(int)  # kn = rows with no priceable cost
    kx = defaultdict(int)                           # kx = rows with no recorded wall
    logs = sorted(glob.glob(log_glob, recursive=True))
    if not logs:
        print(f"no agent logs under {log_glob}", file=sys.stderr)
        return 1

    for p in logs:
        parsed = parse_usage(p, prices)
        if parsed is None:
            continue
        cost, tokens, _model = parsed
        k = kind(os.path.basename(p))
        kr[k] += 1
        if cost is None:
            kn[k] += 1
        else:
            kc[k] += cost
        w = wall_seconds(p)
        if w is None:
            kx[k] += 1
        else:
            kw[k] += w

    print("=== PER AGENT KIND ===")
    print(f"{'kind':<15}{'runs':>5}{'no-price':>10}{'no-wall':>9}"
          f"{'$ total':>10}{'$/run':>8}{'Σwall':>9}")
    tc = tr = tw = 0
    # Print every bucket `kind()` produced, not a fixed list of them. The
    # historical names come first for a stable reading order, then anything
    # else in sorted order, then `other` last. A list here goes stale the
    # moment a new agent kind is classified -- which is how every
    # `translate`-era bucket (`wrap-type`, `review-symbol`, ...) came to be
    # counted into `kr` and then silently dropped from both its row and the
    # Sigma, under-reporting a campaign by whatever those agents cost.
    _FIRST = ["port", "wrap", "merge", "setup"]
    order = [k for k in _FIRST if kr.get(k)]
    order += sorted(k for k in kr if k not in _FIRST and k != "other")
    if kr.get("other"):
        order.append("other")
    for k in order:
        if not kr.get(k):
            continue
        per = kc[k] / kr[k] if kr[k] else 0.0
        print(f"{k:<15}{kr[k]:>5}{kn[k]:>10}{kx[k]:>9}"
              f"{kc[k]:>10.2f}{per:>8.2f}{hm(kw[k]):>9}")
        tc += kc[k]; tr += kr[k]; tw += kw[k]
    print(f"{'Σ':<15}{tr:>5}{sum(kn.values()):>10}{sum(kx.values()):>9}"
          f"{tc:>10.2f}{'':>8}{hm(tw):>9}")

    # ---- per-wave (map session dirs between consecutive wave commits) ----
    out = subprocess.run(["git", "-C", args.repo_root, "log", "--all",
                          "--format=%ct %s"], capture_output=True, text=True).stdout
    waves = {}
    for line in out.splitlines():
        m = re.search(r"crustify: L(\d+) ", line)
        if m:
            waves.setdefault(int(m.group(1)), int(line.split()[0]))
    if not waves:
        return 0
    ct = sorted((waves[layer], layer) for layer in waves)

    bywave = defaultdict(lambda: defaultdict(float))
    seen_dirs = {os.path.dirname(p) for p in logs}
    for d in sorted(seen_dirs):
        if not any(glob.glob(f"{d}/{pat}.usage.json")
                   for pat in ("port_*", "wrap_*", "port-*", "wrap-*", "review-*")):
            continue
        mg = glob.glob(f"{d}/merge*.usage.json")
        mt = stat(mg[0] if mg else d, "%Y")
        cand = [layer for t, layer in ct if t >= mt - 60]
        if not cand:
            continue
        layer = min(cand, key=lambda x: waves[x])
        for p in glob.glob(f"{d}/*.usage.json"):
            # Fold `wrap-type` / `review-symbol` / ... onto their family, so a
            # wave's cost is not split across per-subject buckets here.
            k = kind(os.path.basename(p)).split("-")[0]
            if k not in ("port", "wrap", "merge", "review"):
                continue
            parsed = parse_usage(p, prices)
            if parsed and parsed[0] is not None:
                bywave[layer][k] += parsed[0]

    print("\n=== PER WAVE (port/wrap/merge/review $) ===")
    grand = 0.0
    for layer in sorted(bywave):
        b = bywave[layer]
        t = b["wrap"] + b["port"] + b["merge"] + b["review"]
        grand += t
        print(f"  L{layer:<3} wrap={b['wrap']:6.1f} port={b['port']:6.1f} "
              f"merge={b['merge']:5.1f} review={b['review']:6.1f}  total={t:6.1f}")
    print(f"  WAVE Σ = ${grand:.2f}  | + setup ${kc['setup']:.2f} = "
          f"${grand + kc['setup']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
