"""The AI sandbox boundary (batch 1): urbanlens_ai stays Django-free, and every
provider call is either policy-checked and routed through it, or explicitly
refused.

Mirrors test_sandbox_isolation.py's shape for the same reason: several
separable claims, each of which fails differently.

1. urbanlens_ai never imports django or urbanlens, even transitively
   (:class:`UrbanlensAiIsolationTests`) - the whole point of the split.
2. check_direct_inference enforces the role/policy matrix
   (:class:`DirectInferenceGuardTests`), and LocalInferenceClient actually
   calls it before ever touching a provider key
   (:class:`LocalInferenceClientGuardTests`).
3. policy.py refuses a server-side tool, an unlisted model, and an
   over-cap max_tokens (:class:`PolicyTests`) - the mechanical checks inside
   ai-inference, independent of the egress-proxy allowlist that is the real
   network boundary.
4. docker-compose.yml's topology matches what this batch actually wires up
   (:class:`ComposeTopologyTests`) - ai-inference holds no DB/cache/secret
   credential and no volumes, and egress-proxy is never reachable from
   app_network.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from django.test import override_settings
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.sandbox import DirectInferenceError, DirectInferencePolicy, check_direct_inference, current_direct_inference_policy


class UrbanlensAiIsolationTests(SimpleTestCase):
    """urbanlens_ai is importable with no Django, no urbanlens, in the process running it."""

    def test_package_imports_without_django_or_urbanlens(self) -> None:
        # A subprocess, not an in-process import check: this test itself runs
        # inside a fully-configured Django process, where django/urbanlens are
        # already in sys.modules for unrelated reasons - the only way to prove
        # urbanlens_ai doesn't *cause* that import is to check from a process
        # that starts clean.
        script = (
            "import sys\n"
            "import urbanlens_ai, urbanlens_ai.schema, urbanlens_ai.policy, urbanlens_ai.config, urbanlens_ai.providers\n"
            "assert 'django' not in sys.modules, 'django leaked into urbanlens_ai'\n"
            "assert 'urbanlens' not in sys.modules, 'urbanlens leaked into urbanlens_ai'\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=_subprocess_env(), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_wsgi_module_also_stays_django_free(self) -> None:
        """The WSGI entrypoint itself (not just the package) must not pull Django in."""
        script = (
            "import sys\n"
            "import urbanlens_ai.wsgi\n"
            "assert 'django' not in sys.modules, 'django leaked into urbanlens_ai.wsgi'\n"
            "assert 'urbanlens' not in sys.modules, 'urbanlens leaked into urbanlens_ai.wsgi'\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=_subprocess_env(), check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


def _subprocess_env() -> dict[str, str]:
    """The current environment, plus what the subprocess needs to import urbanlens_ai.

    Starting from a *copy* of the real environment (not a minimal replacement)
    so Python's own startup - DLL search, temp dirs, and the rest of what a
    bare interpreter needs on Windows - still works; only PYTHONPATH and the
    inference token are added.
    """
    import os
    import pathlib

    env = os.environ.copy()
    # parents[5] from src/urbanlens/dashboard/tests/hypothesis/ is the repo root.
    src_root = str(pathlib.Path(__file__).resolve().parents[5] / "src")
    env["PYTHONPATH"] = src_root
    env["UL_AI_INFERENCE_TOKEN"] = "test-token"  # noqa: S105 -- fixture value, not a credential
    return env


@override_settings(UL_DIRECT_INFERENCE_POLICY="deny", UL_PROCESS_ROLE="web")
class DirectInferenceGuardTests(SimpleTestCase):
    """``check_direct_inference`` enforces the role/policy matrix."""

    def test_denies_a_deployed_role(self) -> None:
        with self.assertRaises(DirectInferenceError) as ctx:
            check_direct_inference()
        self.assertIn("web", str(ctx.exception))

    @override_settings(UL_PROCESS_ROLE="unspecified")
    def test_allows_an_unspecified_role(self) -> None:
        check_direct_inference()

    @override_settings(UL_PROCESS_ROLE="")
    def test_allows_when_no_role_is_set_at_all(self) -> None:
        check_direct_inference()

    @override_settings(UL_DIRECT_INFERENCE_POLICY="warn")
    def test_warn_logs_and_proceeds(self) -> None:
        with self.assertLogs("urbanlens.dashboard.services.sandbox.guard", level="WARNING") as logs:
            check_direct_inference()
        self.assertIn("web", "\n".join(logs.output))

    @override_settings(UL_DIRECT_INFERENCE_POLICY="allow")
    def test_allow_does_not_check_the_role_at_all(self) -> None:
        check_direct_inference()

    @override_settings(UL_DIRECT_INFERENCE_POLICY="nonsense")
    def test_unknown_policy_falls_back_to_warn(self) -> None:
        self.assertIs(current_direct_inference_policy(), DirectInferencePolicy.WARN)
        with self.assertLogs("urbanlens.dashboard.services.sandbox.guard", level="WARNING"):
            check_direct_inference()

    @override_settings(UL_PROCESS_ROLE="ai")
    def test_denies_the_ai_worker_role_too(self) -> None:
        # ai-worker (batch 2) is exactly the container this guard exists to
        # stop from quietly falling back to a direct provider call.
        with self.assertRaises(DirectInferenceError):
            check_direct_inference()


@override_settings(UL_DIRECT_INFERENCE_POLICY="allow")
class LocalInferenceClientGuardTests(SimpleTestCase):
    """LocalInferenceClient actually calls check_direct_inference before anything else."""

    @override_settings(UL_DIRECT_INFERENCE_POLICY="deny", UL_PROCESS_ROLE="worker")
    def test_a_deployed_role_never_reaches_the_provider_config(self) -> None:
        from urbanlens.dashboard.services.ai.inference_client import InferenceRequest, LocalInferenceClient, Message

        request = InferenceRequest(provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=100)
        with self.assertRaises(DirectInferenceError):
            LocalInferenceClient().send(request)

    def test_unspecified_role_reaches_past_the_guard(self) -> None:
        from unittest.mock import patch

        from urbanlens.dashboard.services.ai.inference_client import InferenceError, InferenceRequest, LocalInferenceClient, Message

        request = InferenceRequest(provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=100)
        # No provider key configured under test settings - proves the guard
        # passed by reaching the *next* failure (no key configured) instead
        # of being stopped at the guard itself.
        with patch("urbanlens.UrbanLens.settings.app.settings.anthropic_api_key", None), self.assertRaises(InferenceError) as ctx:
            LocalInferenceClient().send(request)
        self.assertIn("Anthropic", str(ctx.exception))


class PolicyTests(SimpleTestCase):
    """urbanlens_ai.policy rejects what it's supposed to, before any provider client exists."""

    def test_unlisted_anthropic_model_is_rejected(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_model

        with self.assertRaises(PolicyError):
            validate_model("anthropic", "claude-a-model-that-does-not-exist")

    def test_unlisted_openai_model_is_rejected(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_model

        with self.assertRaises(PolicyError):
            validate_model("openai", "gpt-a-model-that-does-not-exist")

    def test_cloudflare_model_shape_is_still_checked(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_model

        validate_model("cloudflare", "@cf/meta/llama-3.1-8b-instruct")  # does not raise
        with self.assertRaises(PolicyError):
            validate_model("cloudflare", "not-a-workers-ai-model-id")

    def test_server_side_tool_is_rejected(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_tools
        from urbanlens_ai.schema import ToolSpec

        with self.assertRaises(PolicyError):
            validate_tools([ToolSpec(name="web_search", description="search the web", input_schema={})])

    def test_ordinary_tool_is_accepted(self) -> None:
        from urbanlens_ai.policy import validate_tools
        from urbanlens_ai.schema import ToolSpec

        validate_tools([ToolSpec(name="search_pins", description="search the requester's own pins", input_schema={"type": "object", "properties": {}})])

    def test_over_cap_max_tokens_is_rejected(self) -> None:
        from urbanlens_ai.policy import MAX_ALLOWED_TOKENS, PolicyError, validate_request
        from urbanlens_ai.schema import InferenceRequest, Message

        request = InferenceRequest(provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=MAX_ALLOWED_TOKENS + 1)
        with self.assertRaises(PolicyError):
            validate_request(request)

    def test_at_cap_max_tokens_is_accepted(self) -> None:
        from urbanlens_ai.policy import MAX_ALLOWED_TOKENS, validate_request
        from urbanlens_ai.schema import InferenceRequest, Message

        request = InferenceRequest(provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=MAX_ALLOWED_TOKENS)
        validate_request(request)  # does not raise

    def test_cloudflare_never_gets_tools(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_request
        from urbanlens_ai.schema import InferenceRequest, Message, ToolSpec

        request = InferenceRequest(
            provider="cloudflare",
            model="@cf/meta/llama-3.1-8b-instruct",
            messages=[Message(role="user", content="hi")],
            tools=[ToolSpec(name="search_pins", description="d", input_schema={})],
            max_tokens=100,
        )
        with self.assertRaises(PolicyError):
            validate_request(request)

    def test_cloudflare_endpoint_must_be_a_cloudflare_host(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_cloudflare_endpoint

        validate_cloudflare_endpoint("https://api.cloudflare.com/client/v4/accounts/x/ai/run/")  # does not raise
        with self.assertRaises(PolicyError):
            validate_cloudflare_endpoint("http://api.cloudflare.com/")  # not HTTPS
        with self.assertRaises(PolicyError):
            validate_cloudflare_endpoint("https://api.cloudflare.com.evil.example/")  # not actually cloudflare.com


def _compose() -> dict[str, Any]:
    import pathlib

    # parents[5] from src/urbanlens/dashboard/tests/hypothesis/ is the repo root.
    path = pathlib.Path(__file__).resolve().parents[5] / "docker-compose.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class ComposeTopologyTests(SimpleTestCase):
    """docker-compose.yml's AI sandbox topology matches what batch 1 actually wires up.

    PyYAML resolves the ``<<:`` merge-key anchors (x-app-env, x-app-build,
    ...) the same way Compose does; ``${VAR}`` stays a literal string in
    both, so these assertions check structure and literal values, never
    values Compose would only fill in at runtime.
    """

    def test_ai_inference_has_no_env_file(self) -> None:
        compose = _compose()
        self.assertNotIn("env_file", compose["services"]["ai-inference"])

    def test_ai_inference_has_no_volumes(self) -> None:
        compose = _compose()
        self.assertNotIn("volumes", compose["services"]["ai-inference"])

    def test_ai_inference_env_carries_no_db_cache_or_secret_credential(self) -> None:
        compose = _compose()
        env = compose["services"]["ai-inference"]["environment"]
        forbidden_prefixes = ("UL_DB_", "UL_FIELD_ENCRYPTION_KEY", "UL_VALKEY_URL", "DJANGO_SECRET_KEY", "UL_REDATA_", "UL_GOOGLE_", "UL_DISCORD_")
        leaked = [key for key in env if any(key.startswith(prefix) for prefix in forbidden_prefixes)]
        self.assertEqual(leaked, [])

    def test_ai_inference_process_role_is_inference(self) -> None:
        compose = _compose()
        self.assertEqual(compose["services"]["ai-inference"]["environment"]["UL_PROCESS_ROLE"], "inference")

    def test_ai_inference_is_not_on_app_network(self) -> None:
        # This is the one that must never regress silently: app_network has a
        # route to db/valkey/REData, and ai-inference must never gain one.
        compose = _compose()
        self.assertNotIn("app_network", compose["services"]["ai-inference"]["networks"])

    def test_egress_proxy_is_never_on_app_network(self) -> None:
        compose = _compose()
        self.assertNotIn("app_network", compose["services"]["egress-proxy"]["networks"])

    def test_egress_proxy_networks_are_exactly_ai_inference_and_egress(self) -> None:
        compose = _compose()
        self.assertEqual(set(compose["services"]["egress-proxy"]["networks"]), {"ai_network", "inference_network", "ai_egress_network"})

    def test_ai_egress_network_has_no_other_member(self) -> None:
        compose = _compose()
        members = {name for name, service in compose["services"].items() if "ai_egress_network" in service.get("networks", {})}
        self.assertEqual(members, {"egress-proxy"})

    def test_inference_network_is_internal(self) -> None:
        compose = _compose()
        self.assertTrue(compose["networks"]["inference_network"]["internal"])

    def test_ai_egress_network_is_not_internal(self) -> None:
        # The one network with a real gateway in this tier - deliberately not
        # `internal: true`, or egress-proxy could not reach the internet either.
        compose = _compose()
        self.assertNotIn("internal", compose["networks"]["ai_egress_network"])

    def test_app_env_no_longer_carries_the_anthropic_key(self) -> None:
        # AnthropicGateway (pinned for the assistant) now calls providers
        # through ai-inference - nothing on app needs this key directly.
        compose = _compose()
        self.assertNotIn("UL_ANTHROPIC_API_KEY", compose["x-app-env"])

    def test_app_env_still_carries_openai_and_cloudflare_keys(self) -> None:
        # Deferred to the vision.py follow-up (plan Out of scope) - not this batch.
        compose = _compose()
        self.assertIn("UL_OPENAI_API_KEY", compose["x-app-env"])

    def test_ai_inference_env_carries_all_three_provider_keys(self) -> None:
        compose = _compose()
        env = compose["services"]["ai-inference"]["environment"]
        for key in ("UL_ANTHROPIC_API_KEY", "UL_OPENAI_API_KEY", "UL_CLOUDFLARE_AI_API_KEY", "UL_CLOUDFLARE_WORKER_AI_ENDPOINT"):
            self.assertIn(key, env)

    def test_ai_inference_depends_on_egress_proxy_being_healthy(self) -> None:
        compose = _compose()
        self.assertIn("egress-proxy", compose["services"]["ai-inference"]["depends_on"])

    def test_ai_network_is_internal(self) -> None:
        compose = _compose()
        self.assertTrue(compose["networks"]["ai_network"]["internal"])

    def test_db_and_valkey_are_reachable_from_ai_network(self) -> None:
        # ai-worker needs both without joining app_network (which would give
        # it a route to the internet, REData, OAuth).
        compose = _compose()
        self.assertIn("ai_network", compose["services"]["db"]["networks"])
        self.assertIn("ai_network", compose["services"]["valkey"]["networks"])

    def test_ai_worker_has_no_env_file(self) -> None:
        compose = _compose()
        self.assertNotIn("env_file", compose["services"]["ai-worker"])

    def test_ai_worker_has_no_media_volume(self) -> None:
        # logs are fine (every worker gets them); media_volume is the one
        # this container has no business touching - it never handles an upload.
        compose = _compose()
        mounts = " ".join(compose["services"]["ai-worker"]["volumes"])
        self.assertNotIn("media_volume", mounts)

    def test_ai_worker_env_keys_are_an_explicit_allowlist(self) -> None:
        # A future "just add this one var" to x-ai-env is a failing test, not
        # a silent widening of what this container can reach or hold.
        compose = _compose()
        allowed = {
            "UL_DB_USER",
            "UL_DB_NAME",
            "UL_DB_PASS",
            "UL_DB_HOST",
            "UL_DB_PORT",
            "UL_ENVIRONMENT",
            "UL_SITE_URL",
            "UL_VALKEY_URL",
            "DJANGO_SECRET_KEY",
            "UL_FIELD_ENCRYPTION_KEY",
            "UL_FIELD_ENCRYPTION_KEY_FALLBACKS",
            "UL_OPENWEATHERMAP_API_KEY",
            "UL_AI_INFERENCE_URL",
            "UL_AI_INFERENCE_TOKEN",
            "UL_AI_INFERENCE_TIMEOUT_SECONDS",
            "UL_AI_WORKER_ENABLED",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "UL_PROCESS_ROLE",
            "UL_UNTRUSTED_PARSE_POLICY",
        }
        env = compose["services"]["ai-worker"]["environment"]
        leaked = set(env) - allowed
        self.assertEqual(leaked, set())

    def test_ai_worker_env_carries_no_redata_oauth_or_provider_credential(self) -> None:
        compose = _compose()
        env = compose["services"]["ai-worker"]["environment"]
        forbidden_prefixes = ("UL_REDATA_", "UL_GOOGLE_", "UL_DISCORD_", "UL_ANTHROPIC_", "UL_OPENAI_", "UL_CLOUDFLARE_")
        leaked = [key for key in env if any(key.startswith(prefix) for prefix in forbidden_prefixes)]
        self.assertEqual(leaked, [])

    def test_ai_worker_process_role_is_the_literal_ai(self) -> None:
        compose = _compose()
        self.assertEqual(compose["services"]["ai-worker"]["environment"]["UL_PROCESS_ROLE"], "ai")

    def test_ai_worker_untrusted_parse_policy_is_the_literal_deny(self) -> None:
        # Not ${UL_UNTRUSTED_PARSE_POLICY:-warn} like every other worker - this
        # one must never decode untrusted bytes regardless of the host .env.
        compose = _compose()
        self.assertEqual(compose["services"]["ai-worker"]["environment"]["UL_UNTRUSTED_PARSE_POLICY"], "deny")

    def test_ai_worker_is_not_on_app_network(self) -> None:
        compose = _compose()
        self.assertNotIn("app_network", compose["services"]["ai-worker"]["networks"])

    def test_ai_worker_networks_are_exactly_ai_and_inference(self) -> None:
        compose = _compose()
        self.assertEqual(set(compose["services"]["ai-worker"]["networks"]), {"ai_network", "inference_network"})

    def test_ai_worker_drains_the_ai_queue(self) -> None:
        compose = _compose()
        self.assertIn("ai", compose["services"]["ai-worker"]["command"])

    def test_ai_worker_depends_on_ai_inference_being_healthy(self) -> None:
        compose = _compose()
        self.assertIn("ai-inference", compose["services"]["ai-worker"]["depends_on"])
