"""Drive one crustify stage with the ``claude`` CLI.

One subprocess per agent, invoked with ``--output-format stream-json`` so
each turn reaches ``<stage>.log`` **as it happens**. The CLI's default text
mode buffers: it prints the final result and nothing else, so a run that
takes twenty minutes shows a 0-byte log for twenty minutes and then
everything at once. That is indistinguishable from a hung agent, and it was
- watching manifest mtimes was the only way to tell a working run from a
stalled one. Streaming costs crustify a renderer (:func:`_render`), which
is the trade the module previously declined; observability during the run
turned out to be worth more than not owning one.

Accounting still comes from the session transcript the CLI persists
independently of stdout format, priced by ``crustify-log-cost``.

Why not ``--output-format json``, which reports ``total_cost_usd``
directly: it is the same buffering problem (one JSON blob at exit), and
under subscription auth ``total_cost_usd`` is the API-equivalent price
rather than what is billed - so pricing every provider by one method makes
the numbers comparable across providers, which mixing provider-reported and
self-computed dollars would not.

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

# Tool-result payloads can be a whole file; keep the log readable.
_RESULT_CLIP = 2000


def _render(evt: dict) -> list[str]:
    """One ``stream-json`` event -> the lines to log, mirroring the shape the
    codex backend's text mode produces (a tool call, then its output).

    Unknown event types render as nothing rather than raw JSON: the stream
    carries bookkeeping (init handshakes, partial deltas) that is noise in a
    log meant to be read. Anything genuinely unparseable is passed through by
    the caller instead.
    """
    out: list[str] = []
    kind = evt.get("type")

    if kind == "system" and evt.get("subtype") == "init":
        out.append(f"model: {evt.get('model')}  session: {evt.get('session_id')}"
                   f"  cwd: {evt.get('cwd')}")
        return out

    if kind == "assistant":
        for b in (evt.get("message") or {}).get("content") or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(b["text"].rstrip())
            elif b.get("type") == "thinking" and b.get("thinking", "").strip():
                out.append("[thinking] " + b["thinking"].strip()[:400])
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                # Bash is the only tool the backend allowlists; show the command
                # itself rather than a JSON blob.
                cmd = inp.get("command")
                out.append(f"exec {cmd}" if cmd
                           else f"tool {b.get('name')} {json.dumps(inp)[:300]}")
        return out

    if kind == "user":
        for b in (evt.get("message") or {}).get("content") or []:
            if b.get("type") != "tool_result":
                continue
            c = b.get("content")
            if isinstance(c, list):
                c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
            c = (c or "").rstrip()
            if not c:
                continue
            flag = " (error)" if b.get("is_error") else ""
            clipped = c[:_RESULT_CLIP]
            out.append(f"  ->{flag} {clipped}"
                       + ("… [clipped]" if len(c) > _RESULT_CLIP else ""))
        return out

    if kind == "result":
        # On success `result` merely repeats the final assistant message, which
        # has already been logged — print it only when the run ended some other
        # way, where it carries the reason and nothing else has.
        if evt.get("subtype") != "success":
            out.append(f"[result: {evt.get('subtype')}]")
            if (txt := (evt.get("result") or "").strip()):
                out.append(txt)
        out.append(f"[turns: {evt.get('num_turns')}  "
                   f"duration: {evt.get('duration_ms')}ms]")
    return out

# Role framing, prepended to the agent's `system_preamble` and sent on every
# run. crustify's stage prompt arrives as the user message either way; this
# governs only what sits underneath it, and `OVERRIDE_BASE_PROMPT` decides
# whether the CLI's own prompt is appended to or replaced outright.
#
# Kept thin because it is role framing, not instructions - those live in
# conventions.md. Length here is not the cost it once looked like: this text is
# byte-identical across a wave, so it is a cacheable prefix billed at ~0.1x on
# every run after the first that writes it.
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
        system_preamble: str,
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
            # Stream each turn as it happens; `--verbose` is what the CLI
            # requires to emit the per-turn events in print mode rather than
            # only the final result.
            "--output-format", "stream-json",
            "--verbose",
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
        # The system text is unconditional now: it carries the conventions doc
        # and the skill index, which every agent needs and which must sit where
        # context compaction cannot reach. `OVERRIDE_BASE_PROMPT` no longer
        # decides *whether* we write here, only whether the CLI's own prompt
        # survives underneath — append by default, replace when asked.
        system = f"{_BASE_PROMPT}\n\n{system_preamble}".rstrip()
        cmd += (["--system-prompt", system] if cfg.OVERRIDE_BASE_PROMPT
                else ["--append-system-prompt", system])
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
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                log.line(line)          # not an event — pass through verbatim
                continue
            for out in _render(evt):
                log.line(out)
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
