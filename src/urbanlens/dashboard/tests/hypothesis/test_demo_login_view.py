"""The demo instance's one-click login, and its absence everywhere else.

The route is registered conditionally at URLconf import time, so these tests
reload the URLconf under an explicit ``demo_mode`` rather than trusting whatever
value happened to be in force when Django first resolved a URL. Without that,
the result depends on test ordering: any earlier test that reverses a URL inside
a ``demo_mode=True`` patch imports the URLconf *with the demo route registered*,
and it stays registered for the rest of the process.
"""

from __future__ import annotations

import importlib
from unittest import mock

from django.urls import NoReverseMatch, clear_url_caches, reverse

import urbanlens.UrbanLens.urls as root_urls
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX


def _reload_urlconf_with_demo_mode(*, enabled: bool) -> None:
    """Re-import the root URLconf as if ``UL_DEMO_MODE`` were ``enabled``."""
    with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", enabled):
        clear_url_caches()
        importlib.reload(root_urls)


class DemoRouteRegistrationTests(TestCase):
    """The route exists only on a demo instance - it is not merely guarded."""

    def setUp(self) -> None:
        super().setUp()
        # Restore whatever the real configuration produces, so a reload here
        # cannot leak the demo route into the rest of the suite.
        self.addCleanup(lambda: _reload_urlconf_with_demo_mode(enabled=False))

    def test_the_route_is_absent_on_a_normal_instance(self) -> None:
        _reload_urlconf_with_demo_mode(enabled=False)

        with self.assertRaises(NoReverseMatch):
            reverse("demo.start")

    def test_the_route_exists_on_a_demo_instance(self) -> None:
        _reload_urlconf_with_demo_mode(enabled=True)

        self.assertEqual(reverse("demo.start"), "/demo/start/")


class DemoLoginBehaviourTests(TestCase):
    """The view itself, exercised directly rather than through the URLconf."""

    def test_it_seeds_an_account_and_signs_the_visitor_in(self) -> None:
        from django.contrib.auth import SESSION_KEY
        from django.contrib.auth.models import User
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.demo import DemoLoginView

        request = RequestFactory().post("/demo/start/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", True):
            response = DemoLoginView().post(request)

        self.assertEqual(response.status_code, 302)
        # The session, not request.user: django's login() only assigns
        # request.user when the attribute already exists, and a bare
        # RequestFactory request has not been through AuthenticationMiddleware.
        signed_in = User.objects.get(pk=request.session[SESSION_KEY])
        self.assertTrue(signed_in.username.startswith(DEMO_USERNAME_PREFIX))

    def test_it_refuses_on_an_instance_that_is_not_the_demo(self) -> None:
        """The view's own guard, independent of the route not being registered."""
        from django.http import Http404
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.demo import DemoLoginView

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", False), self.assertRaises(Http404):
            DemoLoginView().post(RequestFactory().post("/demo/start/"))
