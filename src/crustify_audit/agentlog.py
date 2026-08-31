"""Per-agent log files for an audit run.

The writer itself is :class:`crustify.core.agentlog.AgentLog`; only the
naming convention is audit's. Timestamps are second-resolution, so `tag`
distinguishes AGENTS RUNNING CONCURRENTLY -- two spawned in the same second
would otherwise share a stem and one would lose its transcript entirely.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

from crustify.core.agentlog import AgentLog

__all__ = ["AgentLog", "open_agent_log"]


@contextlib.contextmanager
def open_agent_log(logs_dir: Path, stage: str, tag: str | None = None):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{stage}-{stamp}" + (f"-{tag}" if tag else "")
    log = AgentLog(Path(logs_dir), stem, console=False)
    try:
        yield log
    finally:
        log.close()
