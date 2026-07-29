"""Drive one crustify stage with the ``claude`` CLI.

One subprocess per agent, invoked in the CLI's own **text** mode so its
human-readable output lands in ``<stage>.log`` unaltered - crustify writes
no renderer. Accounting is recovered afterwards from the session
transcript the CLI persists independently of stdout format, and priced by
``utils/log_cost.py``.

Why not ``--output-format json``, which reports ``total_cost_usd``
directly: a process emits one stream in one format, so taking the machine
format means owning the rendering. The transcript carries the same token
counts, and under subscription auth ``total_cost_usd`` is the
API-equivalent price rather than what is billed anyway - so pricing every
provider by one method makes the numbers comparable across providers,
which mixing provider-reported and self-computed dollars would not.

The transcript's one blind spot: auxiliary requests the CLI makes on the
side (session-title generation) bill to the account but are never written
as ``assistant`` entries, so summing undercounts by their fixed cost -
measured at 540 input / 14 output tokens, i.e. $0.0006 on a Haiku run.
That is a per-session constant, negligible against an agent that burns
six figures of tokens, and it is a floor rather than a drift.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from crustify.agentlog import AgentLog

# Replaces the CLI's own base prompt when ``config.OVERRIDE_BASE_PROMPT``
# is set (the default). crustify's stage prompt arrives as the user
# message either way; this governs only what sits underneath it. Kept
# deliberately thin - the stage prompts carry the actual instructions, and
# every token here is re-sent on every agent invocation.
_BASE_PROMPT = (
    "You are a code-translation agent in the crustify C-to-Rust pipeline. "
    "You have exactly one tool: Bash. Read files, search, and inspect the "
    "tree through it. Follow the task prompt exactly and stop when its "
    "stated completion condition is met."
)


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


class ClaudeCliBackend:
    def run(
        self,
        *,
        name: str,
        model: str,
        prompt_template: str,
        arguments: dict,
        work_dir: str,
        log: AgentLog,
    ) -> None:
        from crustify import config as cfg
        from crustify.layout import Layout
        from crustify.models import resolve

        exe = shutil.which("claude")
        if exe is None:
            raise SystemExit(
                "claude_cli backend: the `claude` CLI is not on PATH."
            )

        wd = Path(work_dir).resolve()
        route = resolve(model)
        session_id = str(uuid.uuid4())
        prompt = prompt_template.format(**arguments)

        cmd = [
            exe, "-p", prompt,
            "--model", route.model,
            "--session-id", session_id,
            # One tool. `--tools` is an allowlist, so anything the CLI gains
            # in a later version stays excluded by default.
            "--tools", "Bash",
            "--disable-slash-commands",
            # Hermeticity: ignore the operator's settings files and every MCP
            # server not passed explicitly here.
            "--setting-sources", "",
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(wd),
        ]
        if cfg.OVERRIDE_BASE_PROMPT:
            cmd += ["--system-prompt", _BASE_PROMPT]
        if cfg.BILLING == "api":
            # `--bare` is the only switch that makes the CLI authenticate by
            # API key: exporting ANTHROPIC_API_KEY alone does not - it keeps
            # sending the stored OAuth token (verified on the wire). It also
            # strips hooks, LSP and CLAUDE.md discovery, which suits a
            # pipeline invocation.
            cmd.append("--bare")
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit(
                    "claude_cli backend: --billing api needs ANTHROPIC_API_KEY "
                    "in the environment; without it the CLI issues no request "
                    "and exits 0 having done nothing."
                )

        env = dict(os.environ)
        env["ANTHROPIC_CONFIG_DIR"] = str(
            Layout(Path(arguments.get("repo_root", wd))).providers("claude")
        )

        proc = subprocess.Popen(
            cmd, cwd=str(wd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        # Drain stderr on its own thread: reading the two pipes in sequence
        # deadlocks as soon as either fills its buffer.
        err_thread = threading.Thread(
            target=lambda: [log.stderr(ln) for ln in proc.stderr],
            daemon=True,
        )
        err_thread.start()
        for line in proc.stdout:
            log.line(line.rstrip("\n"))
        rc = proc.wait()
        err_thread.join(timeout=5)

        transcript = _transcript_path(session_id, wd)
        if transcript is not None:
            requests, transcript_model = _read_usage(transcript)
            log.usage({
                "provider": "anthropic",
                "model": transcript_model or route.model,
                "requests": requests,
            })
        else:
            log.line(f"[crustify] {name}: no session transcript for "
                     f"{session_id}; this run is unaccounted.")

        if rc != 0:
            raise SystemExit(
                f"claude_cli backend: `claude` exited {rc} for {name}. "
                f"See the agent log for its output."
            )
