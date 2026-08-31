import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crustify_audit.agents.backends.codex_cli import CodexCliBackend
from crustify_audit.models import resolve


class _Log:
    def write(self, _line):
        pass

    def record_usage(self, rows, session_id="", provider="", model=""):
        pass


def _run(*, provider, model, billing="api", effort="high"):
    proc = SimpleNamespace(stdout=(), wait=Mock())
    key = {
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }[provider]
    with (
        patch.dict(os.environ, {key: "test-key"}, clear=True),
        patch("shutil.which", return_value="/usr/bin/codex"),
        patch("subprocess.Popen", return_value=proc) as popen,
    ):
        CodexCliBackend().run(
            name="test",
            model=model,
            provider=provider,
            prompt_template="prompt",
            arguments={},
            system_preamble="system",
            work_dir="/target",
            log=_Log(),
            billing=billing,
            effort=effort,
        )
    return popen.call_args.args[0]


class CodexProviderTests(unittest.TestCase):
    def test_openrouter_route_keeps_the_full_model_slug(self):
        route = resolve("openrouter/anthropic/claude-sonnet-5")

        self.assertEqual("codex_cli", route.backend)
        self.assertEqual("openrouter", route.provider)
        self.assertEqual("anthropic/claude-sonnet-5", route.model)

    def test_openrouter_api_key_and_responses_endpoint_reach_codex(self):
        cmd = _run(
            provider="openrouter", model="anthropic/claude-sonnet-5")

        self.assertIn("anthropic/claude-sonnet-5", cmd)
        self.assertIn("model_provider=openrouter_apikey", cmd)
        self.assertIn(
            'model_providers.openrouter_apikey.base_url="https://openrouter.ai/api/v1"',
            cmd,
        )
        self.assertIn(
            'model_providers.openrouter_apikey.env_key="OPENROUTER_API_KEY"',
            cmd,
        )
        self.assertIn(
            'model_providers.openrouter_apikey.wire_api="responses"', cmd)

    def test_kimi_k3_auditor_defaults_to_high_reasoning_effort(self):
        cmd = _run(provider="openrouter", model="moonshotai/kimi-k3")

        self.assertIn("moonshotai/kimi-k3", cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_auditor_reasoning_effort_can_be_overridden(self):
        cmd = _run(
            provider="openrouter", model="moonshotai/kimi-k3", effort="medium")

        self.assertIn('model_reasoning_effort="medium"', cmd)

    def test_openai_api_routing_is_unchanged(self):
        cmd = _run(provider="openai", model="gpt-5.6")

        self.assertIn("model_provider=openai_apikey", cmd)
        self.assertIn(
            'model_providers.openai_apikey.env_key="OPENAI_API_KEY"', cmd)

    def test_openrouter_api_key_is_required(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", return_value="/usr/bin/codex"),
            self.assertRaisesRegex(SystemExit, "OPENROUTER_API_KEY"),
        ):
            CodexCliBackend().run(
                name="test",
                model="anthropic/claude-sonnet-5",
                provider="openrouter",
                prompt_template="prompt",
                arguments={},
                system_preamble="system",
                work_dir="/target",
                log=_Log(),
                billing="api",
                effort="high",
            )

    def test_openrouter_does_not_claim_subscription_auth(self):
        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            self.assertRaisesRegex(SystemExit, "require --billing api"),
        ):
            CodexCliBackend().run(
                name="test",
                model="anthropic/claude-sonnet-5",
                provider="openrouter",
                prompt_template="prompt",
                arguments={},
                system_preamble="system",
                work_dir="/target",
                log=_Log(),
                billing="subscription",
                effort="high",
            )


if __name__ == "__main__":
    unittest.main()
