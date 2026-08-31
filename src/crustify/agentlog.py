"""Per-agent output sinks.

A CLI-backed agent is a subprocess whose stdout crustify owns, so there is
no printer abstraction and no rendered-then-reparsed format.

The providers are invoked in their streaming-JSON mode (claude
``--output-format stream-json``, codex ``--json``) rather than their
human-readable default. Both offer a text mode, but a process emits one
stream in one format, and the text modes do not carry what crustify needs
to account for a run: claude's is the final message and nothing else,
codex's reports tokens but never cost. So crustify takes the machine
format and splits the parsed stream two ways:

  ``<stage>.log``        a compact human rendering - what ran, what it
                         called, what it said. This is what you read when
                         an agent fails. Nothing parses it, so its shape is
                         free to change.

  ``<stage>.usage.json`` this run's accounting in CRUSTIFY's shape, built by
                         the backend from the CLI's session transcript - not a
                         provider object passed through. The one file anything
                         parses; ``crustify-log-cost`` reads it.

The raw stream itself is never stored. Tool-result events embed whole file
bodies and command output, so a port wave's raw streams would outweigh
every other artifact crustify writes - and everything worth keeping from
them is already in one of the two files above.

The console receives the same text as ``<stage>.log``. Both files live under
``campaigns/<target>/logs/<session>/`` so all waves for one target share a
session namespace without mixing logs from other targets.

``LOG_TO_FILE`` gates the files and ``LOG_TO_CONSOLE`` the console; with
both off the loop still runs (the subprocess still has to be drained) and
every sink is a no-op.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


from crustify.core.agentlog import AgentLog, _hms, _iso  # noqa: F401


class SessionLog:
    """One text log for a whole scheduled run, beside its agents' logs.

    A session's wall clock belongs to no single agent: it also covers the
    worktree forking, the dependency-layer barriers, and the rebase-and-land
    tail, none of which any agent is charged for. Deriving it from the agent
    records instead (``max(ended_at) - min(started_at)``) is bounded by the
    first and last AGENT and silently drops exactly that overhead -- on one
    15-agent wave, 18 of 70 minutes.

    Text, not JSON, and deliberately so: ``usage.json`` stays the one parsed
    file. Nothing reads this, so its shape is free to change, the same
    contract ``<stage>.log`` has.

    Checkpoints are flushed as each layer completes, so a run that is killed
    mid-flight still accounts for the layers that finished.
    """

    def __init__(self, log_dir: Path | None, verb: str, session: str,
                 *, console: bool) -> None:
        self.console = console
        self.path: Path | None = None
        self._fh: IO[str] | None = None
        self._t0 = time.monotonic()
        self._started = time.time()

        if log_dir is not None:
            log_dir = log_dir.resolve()
            log_dir.mkdir(parents=True, exist_ok=True)
            self.path = log_dir / "session.log"
            self._fh = open(self.path, "w")  # noqa: SIM115
        self.line(f"[crustify] {verb} session {session}"
                  f"  started {_iso(self._started)}")

    def line(self, text: str) -> None:
        if self.console:
            print(text, flush=True)
        if self._fh is not None:
            self._fh.write(text + "\n")
            self._fh.flush()

    def checkpoint(self, text: str) -> None:
        """One completed stage of the run, with elapsed-so-far."""
        self.line(f"[crustify] {text}"
                  f"  (+{_hms(time.monotonic() - self._t0)})")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.write(
                f"[crustify] wall {_hms(time.monotonic() - self._t0)}"
                f"  {_iso(self._started)} -> {_iso(time.time())}\n")
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SessionLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_session_log(log_root: Path | None, verb: str) -> SessionLog:
    """Build a :class:`SessionLog` for this invocation, honouring the flags.

    ``log_root`` is the per-session log dir — the same one the agents' logs
    land in — or ``None`` when the caller cannot resolve one (a test double
    with no layout), which makes every sink a no-op.
    """
    from crustify import config as _cfg

    return SessionLog(
        log_root if (log_root is not None and _cfg.LOG_TO_FILE) else None,
        verb, _cfg.SESSION_ID,
        console=False,          # the scheduler already narrates to the console
    )


def open_agent_log(log_root: Path, stem: str) -> AgentLog:
    """Build an :class:`AgentLog` for ``stem`` honouring the runtime flags.

    ``log_root`` is the per-session log dir; it is only created when
    ``LOG_TO_FILE`` is set.
    """
    from crustify import config as _cfg

    return AgentLog(
        log_root if _cfg.LOG_TO_FILE else None,
        stem,
        console=_cfg.LOG_TO_CONSOLE,
    )
