"""The Jinja2 backend renders, shares the Django engine's context processors, and resolves lazy context values.

Templates move to this engine one file at a time, so a value that renders under the Django engine has to render
the same way here. Jinja does not call callables the way Django's variable resolution does, which is the one
place the two engines could disagree silently rather than raise.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.template import engines
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class Jinja2BackendTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.user = baker.make(User)
        self.request = RequestFactory().get("/")
        self.request.user = self.user

    def _render(self, source: str) -> str:
        return engines["jinja2"].from_string(source).render({}, self.request)

    def test_the_backend_is_registered(self) -> None:
        self.assertEqual(engines["jinja2"].name, "jinja2")

    def test_it_renders(self) -> None:
        self.assertEqual(self._render("{{ 1 + 1 }}"), "2")

    def test_the_url_global_reverses(self) -> None:
        self.assertEqual(self._render("{{ url('map.view') }}"), reverse("map.view"))

    def test_eager_context_processors_run(self) -> None:
        self.assertEqual(self._render("{{ page_name }}"), "")

    def test_deferred_context_values_render_as_their_value(self) -> None:
        rendered = self._render("{{ nav_unread_notifications }}")

        self.assertEqual(rendered, "0")

    def test_deferred_booleans_are_truthy_tested_not_stringified(self) -> None:
        """An unevaluated Deferred is always truthy as an object; only the resolved value may decide a branch."""
        self.assertEqual(self._render("{% if show_games_nav %}yes{% else %}no{% endif %}"), "no")
