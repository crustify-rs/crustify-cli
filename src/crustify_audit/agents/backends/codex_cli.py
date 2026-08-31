"""The `codex` CLI backend.

Thinner than the claude one for a structural reason: codex has no
append-to-system-prompt slot, only a replace. So the base text and the role text
are concatenated here and handed over as one block -- same content, different
placement, which is exactly what the Backend docstring warns about.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from crustify_audit.agentlog import AgentLog

# Codex's built-in OpenAI provider authenticates from `auth.json` in CODEX_HOME
# (what `codex login` writes) and ignores an API key in the environment. API
# billing therefore declares an explicit env-key provider. OpenRouter uses the
# same Responses wire protocol and needs its own base URL and key. `wire_api`
# must be `responses`: current Codex rejects the removed `chat` value.
_API_PROVIDERS = {
    "openai": (
        "OpenAI",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
    ),
    "openrouter": (
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
    ),
}


def _api_provider_args(provider: str) -> list[str]:
    try:
        name, base_url, env_key = _API_PROVIDERS[provider]
    except KeyError:
        raise SystemExit(
            f"the codex backend cannot use provider {provider!r}.") from None
    provider_id = f"{provider}_apikey"
    return [
        "-c", f"model_provider={provider_id}",
        "-c", f'model_providers.{provider_id}.name="{name}"',
        "-c", f'model_providers.{provider_id}.base_url="{base_url}"',
        "-c", f'model_providers.{provider_id}.env_key="{env_key}"',
        "-c", f'model_providers.{provider_id}.wire_api="responses"',
    ]


_BASE = (
    "You are running non-interactively. Work autonomously to completion; "
    "there is nobody to ask."
)


class CodexCliBackend:
    def run(self, *, name, model, provider, prompt_template, arguments,
            system_preamble, work_dir, log: AgentLog,
            billing: str = "subscription", effort: str = "high") -> None:
        if shutil.which("codex") is None:
            raise SystemExit(
                "the `codex` CLI is not on PATH. Install it, or pick an "
                "anthropic model to drive the claude backend instead.")
        prompt = prompt_template.format(**arguments)
        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", model,
            "-c", f"instructions={_BASE}\n\n{system_preamble}",
        ]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if billing == "api":
            try:
                env_key = _API_PROVIDERS[provider][2]
            except KeyError:
                raise SystemExit(
                    f"the codex backend cannot use provider {provider!r}.") from None
            if not os.environ.get(env_key):
                raise SystemExit(
                    f"--billing api with {provider} needs {env_key} "
                    "in the environment.")
            cmd += _api_provider_args(provider)
        elif provider == "openrouter":
            raise SystemExit(
                "openrouter models require --billing api; Codex has no "
                "OpenRouter subscription login.")
        cmd.append(prompt)
        proc = subprocess.Popen(
            cmd, cwd=work_dir, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout or ():
            log.write(line)
        proc.wait()
        log.record_usage([], "", provider, model)
