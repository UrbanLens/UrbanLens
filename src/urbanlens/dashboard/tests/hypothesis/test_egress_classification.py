"""Every external service and every scheduled task is classified (D26), so the policy can never silently miss one.

An unclassified service is treated as billed at runtime; these fail first, naming it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import pkgutil

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.egress import GATED_BACKGROUND_TASKS, UNLEDGERED_SERVICES, explicit_category
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY, all_service_defaults
from urbanlens.UrbanLens.egress import BEAT_EGRESS, BeatEgress, EgressCategory

_SRC = pathlib.Path(__file__).resolve().parents[3]

#: Functions whose first positional argument, as a string literal, names the service a call is made under.
_SERVICE_CALLS = frozenset(
    {
        "api_call_slot",
        "log_api_call",
        "_reserve_call",
        "require_egress",
        "egress_permitted",
        "service_is_enabled",
        "_RateLimitedSession",
    }
)


def _import_everything_under(package: str) -> None:
    module = importlib.import_module(package)
    for info in pkgutil.walk_packages(module.__path__, prefix=f"{package}."):
        if ".tests" in info.name or ".migrations" in info.name:
            continue
        importlib.import_module(info.name)


def _gateway_classes() -> set[type[Gateway]]:
    found: set[type[Gateway]] = set()
    pending = list(Gateway.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls in found:
            continue
        found.add(cls)
        pending.extend(cls.__subclasses__())
    return found


def _literal_service_keys() -> dict[str, str]:
    """Every string literal passed as the service to a rate-limiter or egress call in the source, with where it is."""
    keys: dict[str, str] = {}
    for path in (_SRC / "dashboard").rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            first = node.args[0]
            if name in _SERVICE_CALLS and isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.setdefault(first.value, f"{path.relative_to(_SRC)}:{node.lineno}")
    return keys


class EveryServiceIsClassifiedTests(SimpleTestCase):
    def test_every_registry_and_plugin_service_declares_its_category(self) -> None:
        unclassified = sorted(
            service for service, defaults in all_service_defaults().items() if defaults.category is None
        )
        self.assertEqual(
            unclassified, [], "Give each ServiceDefaults a category (urbanlens.UrbanLens.egress.EgressCategory)"
        )

    def test_the_core_registry_alone_is_classified(self) -> None:
        """It is the fallback when plugin defaults cannot be read."""
        self.assertEqual(
            sorted(service for service, defaults in SERVICE_REGISTRY.items() if defaults.category is None), []
        )

    def test_every_concrete_gateway_s_service_is_classified(self) -> None:
        _import_everything_under("urbanlens.dashboard.services.apis")
        _import_everything_under("urbanlens.dashboard.plugins.builtin")
        missing = sorted(
            f"{cls.__module__}.{cls.__qualname__} ({cls.service_key})"
            for cls in _gateway_classes()
            # A private base (``_RedataStreetViewProvider``) only carries the key its metaclass derived; its
            # subclasses name their own.
            if cls.service_key
            and not inspect.isabstract(cls)
            and not cls.__name__.startswith("_")
            and explicit_category(cls.service_key) is None
        )
        self.assertEqual(missing, [])

    def test_every_service_named_in_the_source_is_classified(self) -> None:
        """``api_call_slot("x")``, ``log_api_call("x")``, ``require_egress("x")`` and the like."""
        keys = _literal_service_keys()
        self.assertIn("trivia_generation", keys)
        missing = sorted(f"{key} at {where}" for key, where in keys.items() if explicit_category(key) is None)
        self.assertEqual(missing, [])

    def test_every_ai_provider_s_fallback_key_is_classified(self) -> None:
        """An ``LLMGateway`` built without a feature logs its calls as ``ai_<provider>``."""
        from urbanlens.dashboard.services.ai.gateway import LLMGateway

        _import_everything_under("urbanlens.dashboard.services.ai")
        pending, providers = list(LLMGateway.__subclasses__()), set()
        while pending:
            cls = pending.pop()
            pending.extend(cls.__subclasses__())
            if "PROVIDER" in vars(cls):
                providers.add(cls.PROVIDER)
        self.assertGreaterEqual(providers, {"openai", "cloudflare", "anthropic"})
        for provider in providers:
            with self.subTest(provider=provider):
                self.assertIs(explicit_category(f"ai_{provider}"), EgressCategory.AI)

    def test_every_path_that_bypasses_the_session_says_how_it_is_held(self) -> None:
        for service, entry in UNLEDGERED_SERVICES.items():
            with self.subTest(service=service):
                self.assertTrue(entry.enforced_by.strip())

    def test_the_paths_the_audit_named_are_classified(self) -> None:
        """The LLM features, SMTP, Twilio, Stripe and Google Calendar (2026-10-05 egress audit)."""
        for service in (
            "trivia_generation",
            "link_extraction",
            "document_pin_import",
            "trip_suggestions",
            "label_style_suggestions",
            "category_suggestions",
            "assistant",
            "email",
            "sms",
            "whatsapp",
            "stripe",
            "google_calendar",
            "wayback_save",
            "unified_push",
        ):
            with self.subTest(service=service):
                self.assertIsNotNone(explicit_category(service))


class EveryBeatEntryIsClassifiedTests(SimpleTestCase):
    def test_every_scheduled_entry_is_classified_and_nothing_else_is(self) -> None:
        self.assertEqual(set(BEAT_EGRESS), set(settings.FULL_BEAT_SCHEDULE))

    def test_every_external_entry_s_own_task_is_gated(self) -> None:
        """Leaving it off the schedule is not enough: a chained or re-enqueued run must stop too."""
        import urbanlens.dashboard.tasks  # noqa: F401 - registers the gated tasks

        for name, egress in BEAT_EGRESS.items():
            if egress is not BeatEgress.EXTERNAL:
                continue
            with self.subTest(entry=name):
                self.assertIn(settings.FULL_BEAT_SCHEDULE[name]["task"], GATED_BACKGROUND_TASKS.get(name, set()))

    def test_no_internal_entry_is_gated(self) -> None:
        import urbanlens.dashboard.tasks  # noqa: F401 - registers the gated tasks

        self.assertEqual(
            sorted(name for name in GATED_BACKGROUND_TASKS if BEAT_EGRESS.get(name) is not BeatEgress.EXTERNAL), []
        )
