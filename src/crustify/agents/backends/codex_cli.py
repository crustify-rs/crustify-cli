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

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from crustify.agentlog import AgentLog

# Replaces codex's own ~20k-character base instructions when
# ``config.OVERRIDE_BASE_PROMPT`` is set (the default).
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


def _writable_roots(repo_root: Path) -> list[str]:
    """Directories the sandbox must let the agent write, beyond its work dir.

    ``-s workspace-write`` grants exactly ``[workdir, /tmp, $TMPDIR]``, and the
    work dir is the *target* directory (``<repo>/ssl``) — so out of the box an
    agent cannot write the Rust it is asked to emit, cannot commit, and cannot
    submit a record. Three roots are needed:

    * ``repo_root`` — in an isolated wave this is the agent's worktree: the C
      tree it reads, ``crustify/rust/`` it writes, and cargo's ``target/``.
    * the **main checkout** — for two independent reasons. A worktree's ``.git``
      is a *file* pointing at ``<main>/.git/worktrees/<slug>``, so every commit
      and the landing push write there; and :func:`crustify.worktree.link_shared`
      symlinks ``analysis`` / ``targets`` / ``.providers`` / ``tmp`` / the
      repo-tier JSON stores back to the main checkout, which is where the
      agent's record submissions and codex's own session rollout land.
    * ``CARGO_HOME`` — cargo writes its registry cache and lockfiles there.

    The main checkout is derived rather than passed: ``--git-common-dir`` is
    ``<main>/.git`` from any worktree and ``<repo>/.git`` from a plain checkout,
    so its parent is the right root either way.

    Seatbelt canonicalizes before matching — verified: with write access granted
    to a directory only, a write *through* a symlink in it to a target outside
    still fails ``Operation not permitted``. So granting ``repo_root`` does NOT
    reach anything ``link_shared`` symlinked out; the main checkout must be
    named explicitly.
    """
    roots = [repo_root]
    common = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if common.returncode == 0 and common.stdout.strip():
        git_dir = Path(common.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (repo_root / git_dir).resolve()
        roots.append(git_dir.parent)
    cargo_home = os.environ.get("CARGO_HOME") or str(Path.home() / ".cargo")
    roots.append(Path(cargo_home))

    seen, out = set(), []
    for r in roots:
        r = Path(r).resolve()
        if r.is_dir() and str(r) not in seen:
            seen.add(str(r))
            out.append(str(r))
    return out


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

        repo_root = Path(arguments.get("repo_root", wd)).resolve()
        roots = json.dumps(_writable_roots(repo_root))

        cmd = [exe, "exec", "--skip-git-repo-check",
               "-C", str(wd),
               "-s", "workspace-write",
               "-c", f"sandbox_workspace_write.writable_roots={roots}",
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
            codex_home = Layout(
                Path(arguments.get("repo_root", wd))).providers("codex")
            env["CODEX_HOME"] = str(codex_home)
        else:
            codex_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
        if cfg.BILLING == "api" and route.provider == "openai" \
                and not env.get("OPENAI_API_KEY"):
            raise SystemExit(
                "codex_cli backend: --billing api needs OPENAI_API_KEY in the "
                "environment."
            )

        if cfg.OVERRIDE_BASE_PROMPT:
            prompt_file = codex_home / "crustify-base-prompt.md"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(_BASE_PROMPT)
            cmd += ["-c", f'model_instructions_file="{prompt_file}"',
                    "-c", "include_environment_context=false",
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
