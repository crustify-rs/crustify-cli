"""Recover per-request usage from a provider CLI's own session transcript.

Read from DISK rather than scraped from stdout, for three reasons that are
not robustness hand-waving:

* per REQUEST, not per session. Rates are tiered, so one aggregate record
  for a whole run is priced at a tier no single request reached.
* deduplicated by message id. The transcript repeats records; summing what
  a stream emits double-counts them.
* it survives a lost stdout -- a killed terminal, a truncated pipe, an OOM
  that takes the session but not the container.

This is NOT format-independent: it trades the stream's schema for the
transcript's, which is no more public. It is simply the better of the two,
and it is per-provider -- a codex transcript needs its own reader.
"""
from __future__ import annotations

import json
from pathlib import Path

def _transcript_path(session_id: str, work_dir: Path) -> Path | None:
    """Locate the session transcript for ``session_id``.

    The CLI keys transcripts by working directory with ``/`` flattened to
    ``-``. Note this tree is **not** relocated by ``ANTHROPIC_CONFIG_DIR``
    (verified: the transcript lands in the real ``~/.claude`` even when
    that variable points elsewhere), so the path is computed rather than
    owned. A glob fallback covers any change to that escaping scheme.
    """
    projects = Path.home() / ".claude" / "projects"
    direct = projects / str(work_dir).replace("/", "-") / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    hits = sorted(projects.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _read_usage(transcript: Path) -> tuple[list[dict], str]:
    """Extract one usage record per API request from a session transcript.

    Returns ``(requests, model)``. Per-request rather than summed because
    rate tiers key off a single request's context (see
    :meth:`crustify.agentlog.AgentLog.usage`).

    Buckets are exclusive as recorded - the CLI reports ``input_tokens``
    already net of cached reads - and the 5m/1h cache-write split is kept
    because the two bill at different rates. Records are deduplicated by
    message id: the transcript repeats them, and summing naively
    double-counts.
    """
    requests: list[dict] = []
    model = ""
    seen: set[str] = set()
    with open(transcript, errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            mid = msg.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            model = msg.get("model") or model
            u = msg.get("usage") or {}
            cc = u.get("cache_creation") or {}
            requests.append({
                "input_tokens": int(u.get("input_tokens") or 0),
                "output_tokens": int(u.get("output_tokens") or 0),
                "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                "cache_write_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
                "cache_write_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
            })
    return requests, model
