"""No test run can reach a real LLM provider, under either runner.

The patching used to live only in ``TestRunner.setup_test_environment``. Django
calls that; **pytest never does** - pytest-django ignores ``TEST_RUNNER``
entirely - so the guard protected `manage.py test` and nothing else, which is
the runner nobody uses. It went unnoticed because it was never the only defense:
``settings/test.py`` pins every provider credential to a placeholder and the
localhost-only network guard blocks the socket, so a slip failed rather than
succeeded.

It stopped being merely untidy when CI moved from `manage.py test` to pytest,
because that moved CI from the path with the guard to the path without it.

Written against ``AI_CHOKEPOINTS`` rather than a hardcoded pair, so adding a
chokepoint to that tuple extends this test with it - and adding one *without*
touching the tuple is what the test is for.
"""

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
