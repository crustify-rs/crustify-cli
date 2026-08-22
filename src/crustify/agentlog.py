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

The console receives the same text as ``<stage>.log``. Both files live
under ``campaigns/<campaign>/logs/<session>/`` so they stay co-located with
the campaign that produced them.

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


def _iso(epoch: float) -> str:
    """UTC ISO-8601, seconds resolution — sortable and unambiguous."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s" if s >= 3600 \
        else f"{s // 60}m{s % 60:02d}s"


class AgentLog:
    """Owns one agent's sinks for the lifetime of its subprocess.

    Use as a context manager so the file handle closes on the way out,
    including when the agent raises.
    """

    def __init__(self, log_dir: Path | None, stem: str, *, console: bool) -> None:
        self.console = console
        self.path: Path | None = None
        self.usage_path: Path | None = None
        self._fh: IO[str] | None = None

        # Wall clock is OURS to measure, not the provider's. claude's terminal
        # event carries a `duration_ms` and codex carries none, so harvesting it
        # per-backend would leave half the fleet unmeasured -- which is exactly
        # what happened: every codex agent has an empty wall column. Bracketing
        # the subprocess here is one implementation for every backend, and it
        # measures what the scheduler actually pays (crustify's own setup and
        # drain included) rather than what the provider chose to count.
        # `monotonic` for the span so a clock step cannot produce a negative
        # duration; wall clock only for the human-facing stamps.
        self._t0 = time.monotonic()
        self._started = time.time()

        if log_dir is not None:
            # RESOLVED, and that is load-bearing. An isolated agent's log dir is
            # `Layout(<worktree>).logs(target)`, which only reaches the real
            # directory through the `crustify/campaigns` symlink `link_shared`
            # plants in the worktree. The agent PURGES its worktree as the last
            # step of landing, taking that symlink with it — and `usage()`
            # writes by PATH, after the agent returns. Unresolved, that write
            # raised ENOENT on a run that had just succeeded: the exception
            # surfaced as `agent failed`, the wave recorded a failure, and the
            # verb exited non-zero with the work correctly landed. Resolving
            # once here pins both files to the shared tree, which outlives the
            # worktree. (`.log` survived the purge either way — its handle is
            # opened below and an open fd keeps its inode after the directory
            # entry goes. That asymmetry is why a failed run still had a log but
            # never a usage record.)
            log_dir = log_dir.resolve()
            log_dir.mkdir(parents=True, exist_ok=True)
            self.path = log_dir / f"{stem}.log"
            self.usage_path = log_dir / f"{stem}.usage.json"
            self._fh = open(self.path, "w")  # noqa: SIM115

    # ------------------------------------------------------------------ sinks

    def line(self, text: str) -> None:
        """Emit one rendered line to the console and the text log."""
        if self.console:
            print(text, flush=True)
        if self._fh is not None:
            self._fh.write(text + "\n")
            self._fh.flush()

    def usage(self, record: dict) -> None:
        """Record this run's accounting, once, at the end.

        The shape is crustify's, not a provider's - the backend derives it
        by summing the CLI's session transcript, so there is no verbatim
        provider object to preserve::

            {"provider": "anthropic",          # which service billed it
             "model":    "claude-haiku-4-5",   # its id with that service
             "requests": [                     # ONE entry per API request
               {"input_tokens": 566,           # all buckets EXCLUSIVE
                "output_tokens": 456,
                "cache_read_tokens": 26472,
                "cache_write_tokens": 0,
                "cache_write_1h_tokens": 5258},
               ...
             ]}

        ``requests`` is a list rather than a total because rates are
        tiered: several models bill more once a *single* request's context
        crosses a threshold (OpenAI's 272k tier, Anthropic's 200k tier).
        Summing first and pricing once would charge a session of many
        modest requests at a tier none of them reached.

        ``provider`` is required because rates are per-service - the same
        model id priced against the wrong service's table is simply a
        wrong number. ``model`` is required because the CLIs do not
        reliably report it: codex's JSON never names the model at all, so
        the invoker is the only thing that knows.

        Buckets are exclusive, matching Anthropic's reporting. A provider
        whose input count is inclusive of cached reads (codex) must
        subtract before recording, or the cached tokens bill twice.

        Timing is stamped HERE rather than supplied by the caller, so every
        backend reports it identically::

            "started_at":  "2026-08-03T08:45:38+00:00",
            "ended_at":    "2026-08-03T09:13:13+00:00",
            "duration_ms": 1655275

        The two stamps are absolute on purpose: a duration alone gives chain
        and serial totals but not OVERLAP, so it cannot say how many agents
        were live at once, where a dependency-layer barrier fell, or how much
        of a wave's elapsed time no agent was charged for. With intervals all
        three are arithmetic over the records.
        """
        if self.usage_path is not None:
            record = {**record,
                      "started_at": _iso(self._started),
                      "ended_at": _iso(time.time()),
                      "duration_ms": round((time.monotonic() - self._t0) * 1000)}
            self.usage_path.write_text(json.dumps(record) + "\n")

    def stderr(self, text: str) -> None:
        """Record subprocess stderr.

        Always surfaced on the operator's stderr, even when
        ``LOG_TO_CONSOLE`` is off - a provider error is the one thing that
        must not be silent. Also appended to the log, where a failed run's
        explanation belongs next to what led up to it.
        """
        text = text.rstrip("\n")
        if not text:
            return
        print(text, file=sys.stderr, flush=True)
        if self._fh is not None:
            self._fh.write(text + "\n")
            self._fh.flush()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Close the text log, trailing it with this agent's wall clock.

        Written through the open handle, so it lands even for an isolated
        agent that has already purged the worktree its log dir was reached
        through (the same reason ``.log`` survived that purge when
        ``usage.json`` did not — see ``__init__``).

        Runs slightly longer than the ``duration_ms`` in ``usage.json``:
        that is stamped at the terminal usage event, this at the end of the
        drain. The gap is crustify's own teardown, and it belongs to the
        agent.
        """
        if self._fh is not None:
            self._fh.write(
                f"[crustify] wall {_hms(time.monotonic() - self._t0)}"
                f"  {_iso(self._started)} -> {_iso(time.time())}\n")
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "AgentLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


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
