"""Drive one crustify stage with the ``codex`` CLI.

Mirrors :mod:`crustify.agents.backends.claude_cli`: one subprocess per
agent, invoked in the CLI's own text mode so its human-readable output
lands in ``<stage>.log`` unaltered, with accounting recovered afterwards
from the session rollout the CLI persists independently of stdout format.

Codex never reports cost - not in text mode, not under ``--json``, and its
JSON stream does not even name the model - so ``<stage>.usage.json`` is
built from the rollout's ``token_count`` events and priced by
``utils/log_cost.py`` against the billing service's rates.

Two codex-specific traps, both handled below: its ``input_tokens`` is
*inclusive* of cached reads (Anthropic's is not), and its tool surface is
a denylist, so tools must be switched off by name rather than allowed by
name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from crustify.agentlog import AgentLog

# Role framing, prepended to the agent's `system_preamble`. Always replaces
# codex's own ~20k-character base instructions: `model_instructions_file` is
# the only system slot codex offers and it has no append mode, so writing the
# preamble at all means displacing them. `OVERRIDE_BASE_PROMPT` additionally
# strips the context codex injects around those instructions.
_BASE_PROMPT = (
    "You are a code-translation agent in the crustify C-to-Rust pipeline. "
    "Work through the shell. Follow the task prompt exactly and stop when "
    "its stated completion condition is met.\n"
)

_SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})")

# Tools codex offers that crustify has no use for. It has no allowlist -
# unlike claude's `--tools`, these must be named off individually, so a
# future codex release can reintroduce a tool here without crustify
# noticing. `exec_command` / `write_stdin` are the shell pair and stay.
_TOOL_OFF = [
    "--disable", "multi_agent",
    "--disable", "goals",
    "-c", "tools.update_plan.enabled=false",
    "-c", "tools.experimental_request_user_input.enabled=false",
    "-c", "tools.web_search=false",
    "-c", "tools.view_image=false",
]

def _provider_block(key: str, name: str, base_url: str, env_key: str) -> list[str]:
    """`-c` flags defining a model provider authenticated by an env var.

    ``wire_api`` must be ``responses``: codex removed Chat Completions
    support in Feb 2026 and now rejects ``wire_api = "chat"`` at config
    load.
    """
    return [
        "-c", f"model_provider={key}",
        "-c", f'model_providers.{key}.name="{name}"',
        "-c", f'model_providers.{key}.base_url="{base_url}"',
        "-c", f'model_providers.{key}.env_key="{env_key}"',
        "-c", f'model_providers.{key}.wire_api="responses"',
    ]


_OPENROUTER = _provider_block(
    "openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
    "OPENROUTER_API_KEY")

# Codex's *built-in* openai provider authenticates from `auth.json` in
# CODEX_HOME (what `codex login` writes) and ignores OPENAI_API_KEY in the
# environment - it fails 401 "Missing bearer or basic authentication".
# Declaring OpenAI as an explicit env-key provider is what makes an API
# key usable without a stored login.
_OPENAI_APIKEY = _provider_block(
    "openai_apikey", "OpenAI", "https://api.openai.com/v1",
    "OPENAI_API_KEY")


# Reasoning effort per model id: codex never infers one, and its fallback is low
# either way — `none` for a model missing from its catalog, and the catalog's own
# `default_reasoning_level` (`low` for the gpt-5.6 family) for one that is in it.
# Neither suits discovery or codegen. codex does NOT validate
# the value locally (an unknown one is printed in its banner and fails at the
# API), so this table is the whitelist.
#
# `codex debug models` reports the accepted set per model; for the gpt-5.6
# family it is low | medium | high | xhigh | max | ultra. Note that codex's
# catalog names them `gpt-5.6-{sol,terra,luna}` — a bare `gpt-5.6` reaches the
# API fine but warns "Model metadata not found. Defaulting to fallback
# metadata", so its context window and auto-compact limits are guesses.
# Unlisted models keep codex's own default.
_REASONING_EFFORT = {
    "gpt-5.6": "high",
    "gpt-5.6-sol": "high",
    "gpt-5.6-terra": "high",
    "gpt-5.6-luna": "high",
}


# The paths a narrower sandbox had to be told about, kept as the record of what
# `-s danger-full-access` now covers implicitly (see the sandbox comment in
# `run`). Should the mode ever be narrowed again, these are the three roots:
#
#   * the worktree              — the C tree it reads, `crustify/rust/` it
#                                 writes, cargo's `target/`
#   * the MAIN checkout         — a worktree's `.git` is a file pointing at
#                                 `<main>/.git/worktrees/<slug>`, so commits and
#                                 the landing push write there; and
#                                 `worktree.link_shared` symlinks analysis /
#                                 targets / .providers / tmp / the repo-tier
#                                 JSON stores back to it, which is where record
#                                 submissions and codex's session rollout land.
#                                 Derived via `--git-common-dir`, whose parent is
#                                 the right root from a worktree or a plain
#                                 checkout alike.
#   * CARGO_HOME                — cargo's registry cache and lockfiles
#
# Naming the main checkout was NOT redundant with the worktree: seatbelt
# canonicalizes before matching, so a write *through* a `link_shared` symlink to
# a target outside the granted dir failed `Operation not permitted` (verified).


def _rollout_path(codex_home: Path, session_id: str) -> Path | None:
    """Locate the session rollout for ``session_id``.

    Codex files rollouts by date (``sessions/YYYY/MM/DD/rollout-<ts>-<id>``),
    so the id is matched by glob rather than by computing the path.
    """
    hits = sorted(codex_home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"))
    return hits[-1] if hits else None


def _read_usage(rollout: Path) -> list[dict]:
    """One usage record per turn, from the rollout's ``token_count`` events.

    Each event reports both a running ``total_token_usage`` and that turn's
    ``last_token_usage``. The per-turn figure is taken, but only when the
    running total has actually moved: codex re-emits ``token_count``
    without a new request, and counting those would inflate every session.

    Counts are converted to crustify's exclusive buckets - codex's
    ``input_tokens`` includes ``cached_input_tokens``, so cached reads
    would otherwise be billed twice, once at the input rate and once at
    the cache rate.
    """
    requests: list[dict] = []
    prev_total = None
    with open(rollout, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            payload = d.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            marker = total.get("total_tokens")
            if marker is None or marker == prev_total:
                continue
            prev_total = marker
            cached = int(last.get("cached_input_tokens") or 0)
            inp = int(last.get("input_tokens") or 0)
            requests.append({
                "input_tokens": max(inp - cached, 0),
                "output_tokens": int(last.get("output_tokens") or 0),
                "cache_read_tokens": cached,
                "cache_write_tokens": int(last.get("cache_write_input_tokens") or 0),
                "cache_write_1h_tokens": 0,
            })
    return requests


class CodexCliBackend:
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

        exe = shutil.which("codex")
        if exe is None:
            raise SystemExit(
                "codex_cli backend: the `codex` CLI is not on PATH."
            )

        wd = Path(work_dir).resolve()
        route = resolve(model)
        prompt = prompt_template.format(**arguments)

        # Sandbox: FULL ACCESS, hard-coded.
        #
        # `-s workspace-write` grants exactly [workdir, /tmp, $TMPDIR], and the
        # work dir is the TARGET directory (`<repo>/ssl`) — so out of the box
        # the agent could not write the Rust it is asked to emit, commit, or
        # submit a record. It was widened by hand to three roots: the worktree;
        # the MAIN checkout (a worktree's `.git` is a *file* pointing at
        # `<main>/.git/worktrees/<slug>`, so every commit and the landing push
        # write there, and `worktree.link_shared` symlinks analysis / targets /
        # .providers / the repo-tier JSON stores back to it); and CARGO_HOME.
        # Enumerating write roots still left the agent unable to navigate the
        # workspace freely, so the mode is now global.
        #
        # This REMOVES the sandbox rather than widening it: the agent can write
        # anywhere the invoking user can — outside the repo, and into a
        # concurrent run's worktree or suffixed manifests, which is the
        # isolation an `--out-suffix` model comparison otherwise relies on.
        cmd = [exe, "exec", "--skip-git-repo-check",
               "-C", str(wd),
               "-s", "danger-full-access",
               "-m", route.model,
               "--ignore-user-config",
               *_TOOL_OFF]
        effort = _REASONING_EFFORT.get(route.model)
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if route.provider == "openrouter":
            cmd += _OPENROUTER
            if not os.environ.get("OPENROUTER_API_KEY"):
                raise SystemExit(
                    "codex_cli backend: routing via OpenRouter needs "
                    "OPENROUTER_API_KEY in the environment."
                )
        elif cfg.BILLING == "api":
            cmd += _OPENAI_APIKEY

        env = dict(os.environ)
        # CODEX_HOME holds auth.json as well as config, so relocating it
        # discards a ChatGPT-subscription login. Safe to relocate when auth
        # comes from an env key (OpenRouter, or --billing api); otherwise
        # leave the operator's CODEX_HOME in place and rely on
        # --ignore-user-config for config hermeticity.
        env_key_auth = route.provider == "openrouter" or cfg.BILLING == "api"
        if env_key_auth:
            # RESOLVED, not the raw path. An isolated agent's `repo_root` is its
            # worktree, where `crustify/.providers` is a symlink into the main
            # checkout (worktree.link_shared) — so the raw path is only valid
            # while the worktree exists. The agent PURGES its worktree as the
            # last step of landing, before this backend does its accounting, so
            # `_rollout_path` globbing the unresolved path finds nothing and the
            # run is reported unaccounted even though codex wrote the rollout
            # safely into the shared tree. Resolving pins both CODEX_HOME and
            # the later lookup to the real directory, which outlives the wave.
            codex_home = Layout(
                Path(arguments.get("repo_root", wd))).providers("codex").resolve()
            env["CODEX_HOME"] = str(codex_home)
        else:
            codex_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
        if cfg.BILLING == "api" and route.provider == "openai" \
                and not env.get("OPENAI_API_KEY"):
            raise SystemExit(
                "codex_cli backend: --billing api needs OPENAI_API_KEY in the "
                "environment."
            )

        # Same system text as the claude backend, placed the only way codex
        # allows: `model_instructions_file` is a REPLACE slot, so the preamble
        # always goes through it and codex's own model instructions give way.
        # Unconditional, because the preamble carries the principles doc and
        # skill index that every agent needs beyond compaction's reach.
        #
        # The filename is content-addressed. `codex_home` is shared across a
        # wave's worktrees, so a fixed name would have N concurrent agents
        # writing one path; hashing means identical preambles collide on
        # identical bytes (harmless) and differing ones never collide at all.
        system = f"{_BASE_PROMPT}\n\n{system_preamble}".rstrip()
        digest = hashlib.sha256(system.encode()).hexdigest()[:12]
        prompt_file = codex_home / f"crustify-base-prompt-{digest}.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(system)
        cmd += ["-c", f'model_instructions_file="{prompt_file}"']
        if cfg.OVERRIDE_BASE_PROMPT:
            # Strips codex's OWN injected context on top of the replaced
            # instructions — the rest of what "override the base prompt" means.
            cmd += ["-c", "include_environment_context=false",
                    "-c", "include_apps_instructions=false",
                    "-c", "include_collaboration_mode_instructions=false",
                    "-c", "include_permissions_instructions=false"]

        cmd.append(prompt)

        proc = subprocess.Popen(
            cmd, cwd=str(wd), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        # codex has no --session-id: it picks one and prints it in its own
        # banner, which is the only handle on the rollout file. That banner
        # goes to STDERR while the agent's replies go to stdout, so both
        # streams have to be scanned for it.
        found: list[str] = []

        def _scan(line: str) -> None:
            if not found:
                m = _SESSION_RE.search(line)
                if m:
                    found.append(m.group(1))

        def _drain_err() -> None:
            for ln in proc.stderr:
                ln = ln.rstrip("\n")
                _scan(ln)
                log.stderr(ln)

        err_thread = threading.Thread(target=_drain_err, daemon=True)
        err_thread.start()

        for line in proc.stdout:
            line = line.rstrip("\n")
            _scan(line)
            log.line(line)
        rc = proc.wait()
        err_thread.join(timeout=5)
        session_id = found[0] if found else ""

        rollout = _rollout_path(codex_home, session_id) if session_id else None
        if rollout is not None:
            log.usage({
                "provider": route.provider,
                "model": route.model,
                "requests": _read_usage(rollout),
            })
        else:
            log.line(f"[crustify] {name}: no session rollout found"
                     f"{' for ' + session_id if session_id else ''}; "
                     f"this run is unaccounted.")

        if rc != 0:
            raise SystemExit(
                f"codex_cli backend: `codex` exited {rc} for {name}. "
                f"See the agent log for its output."
            )
