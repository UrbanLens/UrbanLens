"""The layers panel carries the viewer's own default_map_view to the map engine.

Every map on the site renders this panel, but most call sites never passed a base at all, so their
maps opened on the engine's hardcoded literal instead of the setting the viewer chose.
"""

from __future__ import annotations

from django.template import Context, Template
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import MapViewChoice


def render_panel(request: object, layers: str = "street,terrain,satellite,borders") -> str:
    template = Template("{% load map_components %}{% map_layers_panel layers %}")
    return template.render(Context({"request": request, "layers": layers}))


class PanelCarriesTheViewersDefaultBaseTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.request = RequestFactory().get("/dashboard/map/")
        self.request.user = self.user

    def _set_view(self, value: str) -> None:
        self.profile.default_map_view = value
        self.profile.save(update_fields=["default_map_view"])
        self.user.refresh_from_db()

    def test_the_configured_base_reaches_the_panel(self) -> None:
        self._set_view(MapViewChoice.SATELLITE)

        self.assertIn('data-default-base="satellite"', render_panel(self.request))

    def test_topographic_is_carried_even_though_its_button_is_called_terrain(self) -> None:
        """The panel spells this layer "terrain" and the setting spells it "topographic"."""
        self._set_view(MapViewChoice.TOPOGRAPHIC)

        self.assertIn('data-default-base="topographic"', render_panel(self.request))

    def test_remember_is_passed_through_for_the_engine_to_resolve(self) -> None:
        """Only the map knows whether it has anywhere to remember a choice."""
        self._set_view(MapViewChoice.REMEMBER)

        self.assertIn('data-default-base="remember"', render_panel(self.request))

    def test_a_base_this_panel_offers_no_button_for_falls_back_to_one_it_does(self) -> None:
        """Opening on a layer with no button would strand the viewer on a base they cannot leave."""
        self._set_view(MapViewChoice.TOPOGRAPHIC)

        html = render_panel(self.request, layers="street,satellite")

        self.assertIn('data-default-base="satellite"', html)
        self.assertNotIn("topographic", html)

    def test_no_attribute_when_the_panel_offers_nothing_that_fits(self) -> None:
        """Nothing to say, so the engine keeps its own default rather than being told a lie."""
        self._set_view(MapViewChoice.TOPOGRAPHIC)

        self.assertNotIn("data-default-base", render_panel(self.request, layers="street"))

    def test_an_anonymous_viewer_has_no_setting_to_carry(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/dashboard/map/")
        request.user = AnonymousUser()

        self.assertNotIn("data-default-base", render_panel(request))

    def test_a_request_without_a_user_does_not_raise(self) -> None:
        self.assertNotIn("data-default-base", render_panel(RequestFactory().get("/")))
