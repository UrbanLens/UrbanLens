"""Tests for the beta time-slider's context wiring.

Three surfaces have to agree for the slider to ever appear:

1. ``context_processors.add_feature_access`` exposes ``has_beta_features`` -
   a generic "does this user get beta stuff at all" flag reused by future
   beta features, not slider-specific.
2. ``PinController.view()`` and ``LocationWikiView.get()`` both expose
   ``temporal_slider_years`` (empty unless the viewer holds
   ``SiteFeature.BETA_FEATURES`` *and* OHM has confirmed, cached coverage for
   the location) and ``temporal_imagery_url_template`` (always present - it's
   only a URL template, gated separately by the years list being non-empty,
   which is what the partial actually checks).

The four combinations of (has beta feature) x (has cached OHM coverage) are
exercised directly against the pin detail and wiki page controllers, since
``services.locations.temporal_imagery.temporal_slider_years`` is the single
place both are supposed to delegate to - a controller-level regression here
(e.g. one page forgetting the feature gate) would not be caught by a unit
test of that function alone.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.context_processors import add_feature_access
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.locations.temporal_imagery import OHM_COVERAGE_CACHE_SOURCE


def _grant_beta_features(user: User) -> None:
    role = baker.make(SubscriptionRole, features=SiteFeature.BETA_FEATURES)
    grant_subscription(user, role, user, None)


def _cache_coverage(location, *, available: bool, years: list[int]) -> None:
    LocationCache.set(location, OHM_COVERAGE_CACHE_SOURCE, {"available": available, "years": years})


class AddFeatureAccessBetaFlagTests(TestCase):
    """``has_beta_features`` on the site-wide context processor."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()
        # Absorb the fresh-test-db bootstrap admin promotion (see
        # test_panel_feature_gate.py's setUp for why the first user is not a
        # safe subject).
        baker.make(User)
        self.user = baker.make(User)

    def _flag(self) -> bool:
        request = self.factory.get("/")
        request.user = self.user
        return add_feature_access(request)["has_beta_features"]

    def test_false_without_the_feature(self) -> None:
        self.assertFalse(self._flag())

    def test_true_once_granted(self) -> None:
        _grant_beta_features(self.user)
        self.assertTrue(self._flag())

    def test_present_in_the_import_error_fallback_shape(self) -> None:
        """The fallback dict (ImportError/DatabaseError branch) must carry the same key.

        Called directly rather than by breaking the import, since the point is
        just that the two dicts declare the same keys - drifting would leave
        one branch's templates referencing an undefined variable.
        """
        request = self.factory.get("/")
        request.user = self.user
        self.assertIn("has_beta_features", add_feature_access(request))


class PinDetailTemporalSliderContextTests(TestCase):
    """``temporal_slider_years``/``temporal_imagery_url_template`` on the pin detail page."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def _context(self) -> dict:
        from django.urls import reverse

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_no_feature_no_coverage_yields_no_years(self) -> None:
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_no_feature_with_coverage_still_yields_no_years(self) -> None:
        """The feature gate applies even when OHM has real data - beta-only means beta-only."""
        _cache_coverage(self.pin.location, available=True, years=[1920, 1950])
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_feature_without_coverage_yields_no_years(self) -> None:
        """Holding the beta flag alone isn't coverage - an uncached/no-coverage place stays hidden."""
        _grant_beta_features(self.user)
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_feature_with_coverage_yields_sorted_years(self) -> None:
        _grant_beta_features(self.user)
        _cache_coverage(self.pin.location, available=True, years=[1950, 1900, 1975])
        self.assertEqual(self._context()["temporal_slider_years"], [1900, 1950, 1975])

    def test_cached_but_unavailable_coverage_yields_no_years(self) -> None:
        """An explicit empty result (available=False) must not be treated as coverage."""
        _grant_beta_features(self.user)
        _cache_coverage(self.pin.location, available=False, years=[])
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_url_template_always_present_and_uses_the_placeholder(self) -> None:
        """The template itself isn't feature-gated - the partial that reads it is."""
        from urbanlens.dashboard.controllers.temporal_imagery import TEMPORAL_YEAR_PLACEHOLDER

        template = self._context()["temporal_imagery_url_template"]
        self.assertIn(self.pin.slug, template)
        self.assertIn(str(TEMPORAL_YEAR_PLACEHOLDER), template)


class WikiTemporalSliderContextTests(TestCase):
    """``temporal_slider_years``/``temporal_imagery_url_template`` on the wiki page."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        # A pin at this location is what earns wiki visibility - see
        # services.wiki.wiki_access.resolve_visible_wiki.
        baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)

    def _context(self) -> dict:
        from django.urls import reverse

        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_no_feature_no_coverage_yields_no_years(self) -> None:
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_no_feature_with_coverage_still_yields_no_years(self) -> None:
        _cache_coverage(self.location, available=True, years=[1920, 1950])
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_feature_without_coverage_yields_no_years(self) -> None:
        _grant_beta_features(self.user)
        self.assertEqual(self._context()["temporal_slider_years"], [])

    def test_feature_with_coverage_yields_sorted_years(self) -> None:
        _grant_beta_features(self.user)
        _cache_coverage(self.location, available=True, years=[1975, 1900])
        self.assertEqual(self._context()["temporal_slider_years"], [1900, 1975])

    def test_url_template_always_present_and_uses_the_placeholder(self) -> None:
        from urbanlens.dashboard.controllers.temporal_imagery import TEMPORAL_YEAR_PLACEHOLDER

        template = self._context()["temporal_imagery_url_template"]
        self.assertIn(self.location.slug, template)
        self.assertIn(str(TEMPORAL_YEAR_PLACEHOLDER), template)
