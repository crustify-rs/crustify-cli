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
from pathlib import Path

from crustify.layout import Layout

from crustify.core.pricing import (  # noqa: F401 - re-exported
    DEFAULT_PRICE_CACHE,
    LITELLM_PRICES,
    OPENROUTER_MODELS,
    load_prices,
    price_request,
)


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
    campaigns = Layout(Path(args.repo_root)).campaigns
    log_glob = os.path.join(str(campaigns), tdir, "logs", "**", "*.usage.json")

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
