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
5. No module under ``services/ai/tools/`` imports REData, a
   ``*_resolution`` chokepoint, or a raw HTTP library
   (:class:`ToolsPackageImportTests`) - the code-level half of the "no
   REData, no web" guarantee (the network is the real boundary; this is
   the fail-fast rail that catches a violation even if a reviewer misses it
   and ``ai_network`` somehow didn't).
6. The egress-proxy allowlist (``config/egress/filter``) never carries a
   REData host, and does carry every host a shipped provider adapter or
   tool gateway actually calls (:class:`EgressFilterTests`) - the allowlist
   is the real network boundary the other five claims are defense in depth
   for, so a host silently missing here is the sandbox stack failing at
   runtime with nothing in the test suite having caught it beforehand.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys
from typing import Any

from django.test import override_settings
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.sandbox import (
    DirectInferenceError,
    DirectInferencePolicy,
    check_direct_inference,
    current_direct_inference_policy,
)


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
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
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
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
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

        request = InferenceRequest(
            provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=100
        )
        with self.assertRaises(DirectInferenceError):
            LocalInferenceClient().send(request)

    def test_unspecified_role_reaches_past_the_guard(self) -> None:
        from unittest.mock import patch

        from urbanlens.dashboard.services.ai.inference_client import (
            InferenceError,
            InferenceRequest,
            LocalInferenceClient,
            Message,
        )

        request = InferenceRequest(
            provider="anthropic", model="claude-sonnet-5", messages=[Message(role="user", content="hi")], max_tokens=100
        )
        # No provider key configured under test settings - proves the guard
        # passed by reaching the *next* failure (no key configured) instead
        # of being stopped at the guard itself.
        with (
            patch("urbanlens.UrbanLens.settings.app.settings.anthropic_api_key", None),
            self.assertRaises(InferenceError) as ctx,
        ):
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

        validate_tools(
            [
                ToolSpec(
                    name="search_pins",
                    description="search the requester's own pins",
                    input_schema={"type": "object", "properties": {}},
                )
            ]
        )

    def test_over_cap_max_tokens_is_rejected(self) -> None:
        from urbanlens_ai.policy import MAX_ALLOWED_TOKENS, PolicyError, validate_request
        from urbanlens_ai.schema import InferenceRequest, Message

        request = InferenceRequest(
            provider="anthropic",
            model="claude-sonnet-5",
            messages=[Message(role="user", content="hi")],
            max_tokens=MAX_ALLOWED_TOKENS + 1,
        )
        with self.assertRaises(PolicyError):
            validate_request(request)

    def test_at_cap_max_tokens_is_accepted(self) -> None:
        from urbanlens_ai.policy import MAX_ALLOWED_TOKENS, validate_request
        from urbanlens_ai.schema import InferenceRequest, Message

        request = InferenceRequest(
            provider="anthropic",
            model="claude-sonnet-5",
            messages=[Message(role="user", content="hi")],
            max_tokens=MAX_ALLOWED_TOKENS,
        )
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


#: The repo root - parents[5] from src/urbanlens/dashboard/tests/hypothesis/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"


def _compose() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))


class ComposeTopologyTests(SimpleTestCase):
    """docker-compose.yml's AI sandbox topology matches what batch 1 actually wires up.

    PyYAML resolves the ``<<:`` merge-key anchors (x-app-env, x-app-build,
    ...) the same way Compose does; ``${VAR}`` stays a literal string in
    both, so these assertions check structure and literal values, never
    values Compose would only fill in at runtime.
    """

    def test_ai_inference_never_loads_the_host_env_file(self) -> None:
        # It does take an env_file now - .env.ai, holding the provider keys
        # (see test_ai_inference_reads_provider_keys_from_its_own_env_file).
        # The rule that has not changed is which file: the host .env carries
        # database, OAuth and REData credentials, and this container must
        # never see any of them.
        compose = _compose()
        env_files = compose["services"]["ai-inference"].get("env_file") or []
        paths = {entry["path"] if isinstance(entry, dict) else entry for entry in env_files}
        self.assertNotIn(".env", paths)

    def test_ai_inference_has_no_volumes(self) -> None:
        compose = _compose()
        self.assertNotIn("volumes", compose["services"]["ai-inference"])

    def test_ai_inference_env_carries_no_db_cache_or_secret_credential(self) -> None:
        compose = _compose()
        env = compose["services"]["ai-inference"]["environment"]
        forbidden_prefixes = (
            "UL_DB_",
            "UL_FIELD_ENCRYPTION_KEY",
            "UL_VALKEY_URL",
            "DJANGO_SECRET_KEY",
            "UL_REDATA_",
            "UL_GOOGLE_",
            "UL_DISCORD_",
        )
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

    def test_ai_inference_networks_are_exactly_inference_and_proxy(self) -> None:
        # Its whole reachable world: the callers that send it work, and the
        # proxy it sends provider calls through. Nothing else.
        compose = _compose()
        self.assertEqual(set(compose["services"]["ai-inference"]["networks"]), {"inference_network", "proxy_network"})

    def test_egress_proxy_is_never_on_app_network(self) -> None:
        compose = _compose()
        self.assertNotIn("app_network", compose["services"]["egress-proxy"]["networks"])

    def test_egress_proxy_networks_are_exactly_its_clients_and_its_own_egress(self) -> None:
        compose = _compose()
        self.assertEqual(set(compose["services"]["egress-proxy"]["networks"]), {"proxy_network", "ai_egress_network"})

    def test_proxy_network_holds_only_the_proxy_and_its_two_clients(self) -> None:
        # The proxy is the one process in this tier that parses bytes from the
        # public internet. It gets a network with exactly the containers that
        # must call it - not ai_network (db, valkey) or inference_network
        # (app, celery-worker), which would make a proxy bug a foothold with
        # somewhere to go.
        compose = _compose()
        members = {
            name for name, service in compose["services"].items() if "proxy_network" in (service.get("networks") or {})
        }
        self.assertEqual(members, {"egress-proxy", "ai-worker", "ai-inference"})

    def test_proxy_network_is_internal(self) -> None:
        compose = _compose()
        self.assertTrue(compose["networks"]["proxy_network"]["internal"])

    def test_db_and_valkey_are_not_reachable_from_the_proxy(self) -> None:
        # The other half of the rule above, asserted from the data side: no
        # network carries both egress-proxy and a datastore.
        compose = _compose()
        proxy_networks = set(compose["services"]["egress-proxy"]["networks"])
        for service in ("db", "valkey", "app", "celery-worker"):
            shared = proxy_networks & set(compose["services"][service]["networks"])
            self.assertEqual(shared, set(), f"egress-proxy shares {shared} with {service}")

    def test_egress_proxy_starts_as_the_tinyproxy_user(self) -> None:
        # Not cosmetic: cap_drop: [ALL] means the container can't setuid/setgid
        # itself away from root at startup, so tinyproxy's own User/Group
        # directives can't do the drop either - confirmed live on chiron
        # (2026-09-02): without this, tinyproxy crash-loops with "Unable to
        # change to group", and neither this repo's tests nor a config-only
        # review catches it - only an actual `docker compose up` does.
        compose = _compose()
        self.assertEqual(compose["services"]["egress-proxy"]["user"], "tinyproxy:tinyproxy")

    def test_ai_egress_network_has_no_other_member(self) -> None:
        compose = _compose()
        members = {
            name for name, service in compose["services"].items() if "ai_egress_network" in service.get("networks", {})
        }
        self.assertEqual(members, {"egress-proxy"})

    def test_inference_network_is_internal(self) -> None:
        compose = _compose()
        self.assertTrue(compose["networks"]["inference_network"]["internal"])

    def test_ai_egress_network_is_not_internal(self) -> None:
        # The one network with a real gateway in this tier - deliberately not
        # `internal: true`, or egress-proxy could not reach the internet either.
        compose = _compose()
        self.assertNotIn("internal", compose["networks"]["ai_egress_network"])

    def test_no_provider_key_reaches_the_app_tier(self) -> None:
        # The property the whole ai-inference split exists for: a provider
        # credential must never sit in the same process environment as
        # UL_DB_PASS and UL_FIELD_ENCRYPTION_KEY. Since the vision migration,
        # nothing on app or celery-worker reads one.
        compose = _compose()
        for env_name in ("x-app-env", "x-sandbox-env", "x-ai-env", "x-beat-env"):
            keys = set(compose[env_name])
            leaked = {
                key
                for key in keys
                if any(token in key for token in ("ANTHROPIC", "OPENAI", "CLOUDFLARE_AI", "CLOUDFLARE_WORKER"))
            }
            self.assertEqual(leaked, set(), f"{env_name} carries provider credential(s): {sorted(leaked)}")

    def test_provider_keys_are_not_interpolated_from_the_host_env(self) -> None:
        # The subtle half. `${UL_OPENAI_API_KEY}` anywhere in this file means
        # the key has to live in the root .env for compose to resolve it - and
        # app/celery-worker load that whole file via env_file, so it would
        # land on them regardless of which service the ${...} appeared under.
        # ai-inference reads .env.ai directly for exactly this reason.
        raw = _COMPOSE_PATH.read_text(encoding="utf-8")
        for key in (
            "UL_ANTHROPIC_API_KEY",
            "UL_OPENAI_API_KEY",
            "UL_CLOUDFLARE_AI_API_KEY",
            "UL_CLOUDFLARE_WORKER_AI_ENDPOINT",
        ):
            self.assertNotIn(
                "${" + key + "}",
                raw,
                f"{key} is interpolated from the host .env, which puts it back on app/celery-worker - it belongs in .env.ai",
            )

    def test_ai_inference_reads_provider_keys_from_its_own_env_file(self) -> None:
        compose = _compose()
        env_files = compose["services"]["ai-inference"]["env_file"]
        paths = {entry["path"] if isinstance(entry, dict) else entry for entry in env_files}
        self.assertEqual(paths, {".env.ai"})
        # Never the host .env - that is the file carrying DB and OAuth secrets.
        self.assertNotIn(".env", paths)

    def test_ai_inference_env_block_carries_no_provider_key(self) -> None:
        # Its credentials come from .env.ai; the inline block is only the
        # bearer token, role, and proxy vars.
        compose = _compose()
        env = set(compose["services"]["ai-inference"]["environment"])
        self.assertEqual(env, {"UL_AI_INFERENCE_TOKEN", "UL_PROCESS_ROLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"})

    def test_env_ai_is_gitignored_and_has_a_sample(self) -> None:
        # A committed .env.ai would defeat the split entirely; a missing
        # sample makes the split undiscoverable for whoever deploys next.
        root = _REPO_ROOT
        self.assertIn(".env.ai", (root / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertTrue((root / ".env.ai-sample").is_file(), ".env.ai-sample is missing")

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
        forbidden_prefixes = (
            "UL_REDATA_",
            "UL_GOOGLE_",
            "UL_DISCORD_",
            "UL_ANTHROPIC_",
            "UL_OPENAI_",
            "UL_CLOUDFLARE_",
        )
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

    def test_ai_worker_networks_are_exactly_ai_inference_and_proxy(self) -> None:
        compose = _compose()
        self.assertEqual(
            set(compose["services"]["ai-worker"]["networks"]), {"ai_network", "inference_network", "proxy_network"}
        )

    def test_ai_worker_drains_the_ai_queue(self) -> None:
        compose = _compose()
        self.assertIn("ai", compose["services"]["ai-worker"]["command"])

    def test_ai_worker_depends_on_ai_inference_being_healthy(self) -> None:
        compose = _compose()
        self.assertIn("ai-inference", compose["services"]["ai-worker"]["depends_on"])


def _egress_filter_lines() -> list[str]:
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[5] / "src" / "urbanlens" / "config" / "egress" / "filter"
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class EgressFilterTests(SimpleTestCase):
    """The egress-proxy allowlist matches what ai-worker's own tools actually call.

    A host missing here isn't a code-level failure - it's ``ai-worker`` (or
    ``ai-inference``) silently unable to reach a host that's genuinely on the
    allowlist's intended set, discovered only once the sandbox stack is
    actually running with FilterDefaultDeny enforcing it. This is what
    would have caught ``distance_and_drive_time``/``get_weather`` shipping
    without their hosts ever being added here.
    """

    def test_no_redata_host_is_ever_allowed(self) -> None:
        lines = _egress_filter_lines()
        self.assertFalse([line for line in lines if "redata" in line.lower()])

    def test_every_provider_and_tool_gateway_host_is_allowed(self) -> None:
        # One entry per host a shipped provider adapter or tool gateway
        # actually calls - see urbanlens_ai/providers/ and
        # services/ai/tools/{routing,weather}.py's default base_urls.
        expected_hosts = {
            "api.anthropic.com",
            "api.openai.com",
            # Both Cloudflare Workers AI hosts. A deployment picks one with
            # UL_CLOUDFLARE_WORKER_AI_ENDPOINT, and chiron's picks the AI
            # Gateway - which this list originally missed, so the default
            # vision provider and the only image classifier were both
            # unreachable while every test here passed. That is the exact
            # failure this class's docstring describes.
            "api.cloudflare.com",
            "gateway.ai.cloudflare.com",
            "router.project-osrm.org",
            "api.open-meteo.com",
            "api.openweathermap.org",
        }
        # Each line is itself an anchored regex (FilterExtended's format) -
        # compile and match the plain hostname against it, rather than
        # string-comparing two different (both valid) escaped renderings of
        # the same host, which is what re.escape(host) vs. a hand-written
        # line would otherwise force this into.
        patterns = [re.compile(line) for line in _egress_filter_lines()]
        for host in expected_hosts:
            with self.subTest(host=host):
                self.assertTrue(
                    any(pattern.match(host) for pattern in patterns),
                    f"{host!r} has no matching entry in the egress filter",
                )


#: A module or submodule name is forbidden if its root matches one of these
#: exactly (``requests``, ``requests.adapters``, ``urllib.parse``, ...) - see
#: _is_forbidden_import.
_FORBIDDEN_IMPORT_ROOTS = {"requests", "httpx", "urllib"}


def _imported_module_names(path: pathlib.Path) -> set[str]:
    """Every module named in a top-level or function-local import in ``path``.

    ``ast.walk`` (not just ``tree.body``) so a local import inside a function
    body - the pattern every tool module actually uses to defer Django model
    imports - is caught the same as a module-level one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _is_forbidden_import(module_name: str) -> bool:
    """Whether ``module_name`` is a REData gateway, a REData-first resolution chokepoint, or raw HTTP."""
    parts = module_name.split(".")
    if parts[0] in _FORBIDDEN_IMPORT_ROOTS:
        return True
    return any(part.startswith("redata_") or part.endswith("_resolution") for part in parts)


class ToolsPackageImportTests(SimpleTestCase):
    """No module under ``services/ai/tools/`` may import REData or raw HTTP libraries directly.

    ``redata_configured()`` already returns ``False`` under ``ProcessRole.AI``
    (defense in depth) and the network topology is the real boundary - this
    is the fastest-failing rail: a violation here is a lint-speed failure
    instead of something only a running sandbox or a reviewer catches.
    """

    def test_no_tool_module_imports_redata_resolution_or_raw_http(self) -> None:
        from urbanlens.dashboard.services.ai import tools

        tools_dir = pathlib.Path(tools.__file__).parent
        violations = {
            path.name: bad
            for path in tools_dir.glob("*.py")
            if (bad := {name for name in _imported_module_names(path) if _is_forbidden_import(name)})
        }

        self.assertFalse(violations, f"forbidden imports found in services/ai/tools/: {violations}")
