"""No test run can reach a real LLM provider, under either runner."""

from __future__ import annotations

from unittest.mock import Mock

from django.test import SimpleTestCase

from urbanlens.core.tests.ai_guard import AI_CHOKEPOINTS


class AiGatewayGuardedTests(SimpleTestCase):
    """Every chokepoint is patched for the whole session."""

    def test_every_chokepoint_is_patched(self) -> None:
        import importlib

        for target in AI_CHOKEPOINTS:
            with self.subTest(target=target):
                module_path, class_name, attribute = target.rsplit(".", 2)
                owner = getattr(importlib.import_module(module_path), class_name)
                self.assertIsInstance(
                    getattr(owner, attribute),
                    Mock,
                    f"{target} is not patched, so a test could reach a real provider",
                )

    def test_the_chokepoint_list_is_not_empty(self) -> None:
        """A guard over an empty list passes while guarding nothing."""
        self.assertTrue(AI_CHOKEPOINTS)
