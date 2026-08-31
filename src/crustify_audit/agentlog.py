"""agentlog.py — per-agent transcript + usage.

Minimal by comparison with crustify-cli's, which has to reconcile usage across
a wave of concurrent agents. One agent means one file and one usage record, so
this is a context manager over two paths and nothing more.

Cost is computed from token counts by a rate table, never from
provider-reported dollars -- the same rule crustify-cli's log_cost.py follows,
for the same reason: providers report post-discount figures that are not
comparable across runs.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path


class AgentLog:
    def __init__(self, stem: Path) -> None:
        self.stem = stem
        self.transcript = stem.with_suffix(".log")
        self.usage = stem.with_suffix(".usage.json")
        self._fh = None
        self._started = time.time()

    def write(self, text: str) -> None:
        if self._fh is not None:
            self._fh.write(text)
            self._fh.flush()

    def record_usage(self, rows: list[dict], session_id: str = "",
                     provider: str = "", model: str = "") -> None:
        """Write the token record, NAMING what it should be priced against.

        A rate is only meaningful together with the service and model that
        billed it, and a file that does not say which is a file someone later
        has to guess about. That guess is what priced a $78 run at $235.
        """
        self.usage.write_text(json.dumps({
            "session_id": session_id,
            "provider": provider,
            "model": model,
            "started_at": self._started,
            "ended_at": time.time(),
            "records": rows,
        }, indent=2) + "\n")


@contextlib.contextmanager
def open_agent_log(logs_dir: Path, stage: str, tag: str | None = None):
    """Open a transcript. `tag` distinguishes AGENTS RUNNING CONCURRENTLY.

    The stamp is second-resolution, so two agents spawned in the same second
    would otherwise write the same file and one would lose its transcript
    entirely.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stage}-{stamp}" + (f"-{tag}" if tag else "")
    log = AgentLog(logs_dir / name)
    log._fh = log.transcript.open("w")
    try:
        yield log
    finally:
        if log._fh:
            log._fh.close()
