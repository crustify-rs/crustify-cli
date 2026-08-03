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

  ``<stage>.usage.json`` the provider's terminal usage event, as emitted -
                         claude's ``result`` (carrying ``total_cost_usd``),
                         codex's ``turn.completed`` (tokens only). The one
                         file anything parses; ``utils/log_cost.py`` reads it.

The raw stream itself is never stored. Tool-result events embed whole file
bodies and command output, so a port wave's raw streams would outweigh
every other artifact crustify writes - and everything worth keeping from
them is already in one of the two files above.

The console receives the same text as ``<stage>.log``. Both files live
under the *target* tier (``targets/<t>/logs/<session>/``) so they stay
co-located with the invocation that produced them, regardless of which
tier the agent's artifact belongs to.

``LOG_TO_FILE`` gates the files and ``LOG_TO_CONSOLE`` the console; with
both off the loop still runs (the subprocess still has to be drained) and
every sink is a no-op.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO


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

        if log_dir is not None:
            # RESOLVED, and that is load-bearing. An isolated agent's log dir is
            # `Layout(<worktree>).logs(target)`, which only reaches the real
            # directory through the `crustify/targets` symlink `link_shared`
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
        """
        if self.usage_path is not None:
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
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "AgentLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


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
