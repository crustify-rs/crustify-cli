"""The `claude` CLI backend."""
from __future__ import annotations

import json
import shutil
import os
import subprocess
from pathlib import Path

from crustify.core.usage import _read_usage, _transcript_path

from crustify_audit.agentlog import AgentLog

#: Appended to, not replacing, the CLI's own system prompt -- claude offers an
#: append slot, so crustify-audit's role text sits beneath the CLI's defaults
#: rather than discarding them.
_BASE = (
    "You are running non-interactively. Work autonomously to completion; "
    "there is nobody to ask. Prefer reading and reasoning over guessing."
)


class ClaudeCliBackend:
    def run(self, *, name, model, provider, prompt_template, arguments,
            system_preamble, work_dir, log: AgentLog,
            billing: str = "subscription", effort: str = "high") -> None:
        if provider != "anthropic":
            raise SystemExit(
                f"the claude backend cannot use provider {provider!r}.")
        if shutil.which("claude") is None:
            raise SystemExit(
                "the `claude` CLI is not on PATH. Install it, or pick an "
                "openai/openrouter model to drive the codex backend instead.")
        prompt = prompt_template.format(**arguments)
        # `timeout` rather than a watchdog thread: the streaming read below
        # blocks, so killing from Python means racing our own loop. Letting the
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--model", model,
            "--append-system-prompt", f"{_BASE}\n\n{system_preamble}",
            "--output-format", "stream-json", "--verbose",
        ]
        if billing == "api":
            # `--bare` is the only switch that makes the CLI authenticate by API
            # key: exporting ANTHROPIC_API_KEY alone does not — it keeps sending
            # the stored OAuth token. It also strips hooks, LSP and CLAUDE.md
            # discovery, which suits a pipeline invocation.
            cmd.append("--bare")
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit(
                    "--billing api needs ANTHROPIC_API_KEY in the environment; "
                    "without it the CLI issues no request and exits 0 having "
                    "done nothing.")
        cmd += ["-p", prompt]
        proc = subprocess.Popen(
            cmd, cwd=work_dir, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        session = ""
        for line in proc.stdout or ():
            log.line(line.rstrip("\n"))
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            session = evt.get("session_id") or session
        proc.wait()
        # Usage comes from the CLI's own transcript, not from the stream: the
        # stream's `result` events are one AGGREGATE per run, and rates are
        # tiered, so pricing an aggregate charges a session of many modest
        # requests at a tier none of them reached. The transcript also repeats
        # records, which the reader dedupes by message id.
        requests: list[dict] = []
        seen_model = ""
        if session:
            transcript = _transcript_path(session, Path(work_dir))
            if transcript is not None:
                requests, seen_model = _read_usage(transcript)
        log.usage({"provider": provider, "model": seen_model or model,
                   "session_id": session, "requests": requests})
