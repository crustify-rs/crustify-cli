"""One agent's log and usage record. Shared by both tools.

The destination is INJECTED, never inferred, and that is the whole seam
between the two tools. A translation agent writes from inside a git worktree
that it purges as its last act, so its directory must resolve through the
symlink into the main checkout; an audit agent has no worktree and writes
where it stands. Neither fact belongs in here -- the caller already knows
which world it is in, and this class does not need to.

`usage()` writes crustify's shape, not a provider's, and writes it PER
REQUEST. Rates are tiered: several models bill more once a single request's
context crosses a threshold, so summing first and pricing once charges a
session of many modest requests at a tier none of them reached.
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
