"""Google Programmable Search Element: gating, the postback endpoint, and the user-provided cache row."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.search.user_provided_search import (
    MAX_RESULTS_PER_ACCOUNT,
    USER_PROVIDED_SOURCE,
    InvalidSubmissionError,
    SubmittedResult,
    load_user_provided_results,
    store_user_provided_results,
    validate_submission,
)
from urbanlens.UrbanLens.settings.app import settings as app_settings
from urbanlens.UrbanLens.settings.base import GOOGLE_PSE_CSP, allow_google_pse

GOOD = {
    "results": [{"url": "https://example.org/a", "title": "A <b>mill</b>", "snippet": "Old &lt;i&gt;mill&lt;/i&gt;"}]
}


class ValidationTests(SimpleTestCase):
    def test_markup_is_stripped_to_plain_text(self) -> None:
        (result,) = validate_submission(GOOD)
        self.assertEqual(result.title, "A mill")
        self.assertNotIn("<", result.snippet)
        self.assertNotIn(">", result.snippet)

    def test_rejections(self) -> None:
        bad = [
            [],
            {},
            {"results": "x"},
            {"results": []},
            {"results": [{"url": "https://a.example/", "extra": 1}]},
            {"results": [{"title": "no url"}]},
            {"results": ["https://a.example/"]},
            {"results": [{"url": "javascript:alert(1)"}]},
            {"results": [{"url": "ftp://a.example/x"}]},
            {"results": [{"url": "//a.example/x"}]},
            {"results": [{"url": "https://user:pw@a.example/"}]},
            {"results": [{"url": "https://a.example/ x"}]},
            {"results": [{"url": "https://a.example/" + "x" * 3000}]},
            {"results": [{"url": "https://a.example/", "title": 5}]},
            {"results": [{"url": f"https://a.example/{i}"} for i in range(21)]},
            {"results": [{"url": "https://a.example/"}], "other": 1},
        ]
        for payload in bad:
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(InvalidSubmissionError):
                validate_submission(payload)

    def test_long_fields_are_truncated_and_duplicates_dropped(self) -> None:
        results = validate_submission(
            {
                "results": [
                    {"url": "https://a.example/", "title": "t" * 999, "snippet": "s" * 999},
                    {"url": "https://a.example/"},
                ]
            }
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].title), 200)
        self.assertEqual(len(results[0].snippet), 500)


class CspTests(SimpleTestCase):
    def test_nothing_admitted_without_an_engine(self) -> None:
        directives = {d: ["'self'"] for d in GOOGLE_PSE_CSP}
        self.assertEqual(allow_google_pse(directives, ""), [])
        self.assertEqual(directives, {d: ["'self'"] for d in GOOGLE_PSE_CSP})

    def test_engine_admits_exact_hosts_only(self) -> None:
        directives = {d: ["'self'"] for d in GOOGLE_PSE_CSP}
        allow_google_pse(directives, "abc")
        self.assertEqual(directives["script-src"], ["'self'", "https://cse.google.com", "https://www.google.com"])
        self.assertEqual(directives["connect-src"], ["'self'", "https://cse.google.com"])
        self.assertTrue(all("*" not in s for sources in directives.values() for s in sources))
        allow_google_pse(directives, "abc")
        self.assertEqual(directives["connect-src"].count("https://cse.google.com"), 1)


class PseViewTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        baker.make("auth.User")  # first user becomes bootstrap admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.profile.external_apis_enabled = True
        self.profile.save()
        self.loc = baker.make(
            Location, official_name="Old Mill Factory", latitude=39.1, longitude=-84.5, city="Cincinnati", state="Ohio"
        )
        self.pin = baker.make(Pin, location=self.loc, profile=self.profile, name="Old Mill Factory")
        grant_subscription(self.user, baker.make(SubscriptionRole, features=SiteFeature.SEARCH), self.user, None)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.url = reverse("pin.web_search.user_provided", args=[self.pin.slug])
        patcher = patch.object(app_settings, "google_pse_cx", "engine123")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, body: object, *, csrf: bool = True, content_type: str = "application/json"):
        payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
        extra = {"HTTP_X_CSRFTOKEN": self._csrf()} if csrf else {}
        return self.client.post(self.url, payload, content_type=content_type, **extra)

    def _csrf(self) -> str:
        token = "a" * 32
        self.client.cookies["csrftoken"] = token
        return token

    def _panel(self):
        with patch("urbanlens.dashboard.controllers.pin.search_web", return_value=[]):
            return self.client.get(reverse("pin.web_search", args=[self.pin.slug]))

    def test_post_stores_flagged_row_with_provenance(self) -> None:
        response = self._post(GOOD)
        self.assertEqual(response.status_code, 200, response.content)
        row = LocationCache.objects.get(location=self.loc, source=USER_PROVIDED_SOURCE)
        self.assertEqual(row.data["trust"], "user_provided")
        (stored,) = row.data["results"]
        self.assertEqual(stored["submitted_by"], [self.user.pk])
        self.assertTrue(stored["submitted_at"])
        self.assertEqual(stored["title"], "A mill")
        self.assertFalse(LocationCache.objects.filter(location=self.loc, source="web_search").exists())

    def test_requires_csrf(self) -> None:
        self.assertEqual(self._post(GOOD, csrf=False).status_code, 403)
        self.assertFalse(LocationCache.objects.filter(source=USER_PROVIDED_SOURCE).exists())

    def test_requires_login(self) -> None:
        self.client.logout()
        self.assertIn(self._post(GOOD, csrf=False).status_code, {302, 403})
        self.assertFalse(LocationCache.objects.filter(source=USER_PROVIDED_SOURCE).exists())

    def test_consent_and_feature_gate_the_endpoint(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save()
        self.assertEqual(self._post(GOOD).status_code, 403)
        self.profile.external_apis_enabled = True
        self.profile.save()
        with patch.object(app_settings, "google_pse_cx", ""):
            self.assertEqual(self._post(GOOD).status_code, 403)
        self.assertFalse(LocationCache.objects.filter(source=USER_PROVIDED_SOURCE).exists())

    def test_other_users_pin_is_404(self) -> None:
        other = baker.make("auth.User")
        pin = baker.make(Pin, location=self.loc, profile=Profile.objects.get(user=other), name="Someone Else Mill")
        url = reverse("pin.web_search.user_provided", args=[pin.slug])
        response = self.client.post(
            url, json.dumps(GOOD), content_type="application/json", HTTP_X_CSRFTOKEN=self._csrf()
        )
        self.assertEqual(response.status_code, 404)

    def test_malformed_bodies_are_rejected(self) -> None:
        self.assertEqual(self._post("not json").status_code, 400)
        self.assertEqual(self._post({"results": [{"url": "javascript:x"}]}).status_code, 400)
        self.assertEqual(self._post(GOOD, content_type="text/plain").status_code, 415)
        self.assertEqual(
            self._post({"results": [{"url": "https://a.example/", "snippet": "x" * 40000}]}).status_code, 413
        )
        self.assertFalse(LocationCache.objects.filter(source=USER_PROVIDED_SOURCE).exists())

    def test_rate_limited_per_user(self) -> None:
        from urbanlens.dashboard.controllers.pin import USER_SEARCH_POST_RATE

        codes = [self._post(GOOD).status_code for _ in range(USER_SEARCH_POST_RATE.limit + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(set(codes[:-1]), {200})

    def test_resubmission_keeps_original_submitter_and_merges(self) -> None:
        self._post(GOOD)
        other = {
            "results": [{"url": "https://example.org/a", "title": "x"}, {"url": "https://example.org/b", "title": "B"}]
        }
        self._post(other)
        row = LocationCache.objects.get(location=self.loc, source=USER_PROVIDED_SOURCE)
        self.assertEqual([r["link"] for r in row.data["results"]], ["https://example.org/b", "https://example.org/a"])
        self.assertEqual(next(r for r in row.data["results"] if r["link"].endswith("/a"))["title"], "A mill")

    def test_panel_renders_element_and_labelled_user_results(self) -> None:
        self._post(GOOD)
        html = self._panel().content.decode()
        self.assertIn("data-google-pse", html)
        self.assertIn('data-pse-cx="engine123"', html)
        self.assertIn("Found by a viewer", html)
        self.assertIn("https://example.org/a", html)
        self.assertIn("nofollow", html)
        self.assertNotIn("<b>mill</b>", html)

    def test_panel_omits_element_without_consent_or_engine(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save()
        with patch(
            "urbanlens.dashboard.controllers.pin.search_web",
            return_value=[{"title": "T", "link": "https://t.example/", "snippet": ""}],
        ):
            html = self.client.get(reverse("pin.web_search", args=[self.pin.slug])).content.decode()
        self.assertNotIn("data-google-pse", html)
        self.profile.external_apis_enabled = True
        self.profile.save()
        LocationCache.objects.all().delete()
        with patch.object(app_settings, "google_pse_cx", ""):
            self.assertEqual(self._panel().status_code, 204)


class CrossUserVisibilityTests(TestCase):
    """What one account posts is shown to others only once a second account corroborates it."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.a, self.b, self.c = (baker.make("auth.User") for _ in range(3))
        self.loc = baker.make(Location, official_name="Old Mill Factory", latitude=39.1, longitude=-84.5)

    def _store(self, user, urls: list[str], query: str = "q", audience: str = "") -> None:
        store_user_provided_results(self.loc, user, query, [SubmittedResult(u, "t", "s") for u in urls], audience)

    def _links(self, viewer, query: str = "q", audience: str = "") -> list[str]:
        return [r["link"] for r in load_user_provided_results(self.loc, viewer.pk, query, set(), audience)]

    def test_submitter_sees_own_but_others_do_not(self) -> None:
        self._store(self.a, ["https://x.example/a"])
        self.assertEqual(self._links(self.a), ["https://x.example/a"])
        self.assertEqual(self._links(self.b), [])

    def test_second_account_corroborates_by_canonical_url(self) -> None:
        self._store(self.a, ["https://www.x.example/a/#frag"])
        self._store(self.b, ["https://X.example/a"])
        self.assertEqual(len(self._links(self.c)), 1)
        (row,) = LocationCache.objects.get(location=self.loc, source=USER_PROVIDED_SOURCE).data["results"]
        self.assertEqual(sorted(row["submitted_by"]), sorted([self.a.pk, self.b.pk]))

    def test_same_account_twice_is_not_corroboration(self) -> None:
        self._store(self.a, ["https://x.example/a"])
        self._store(self.a, ["https://x.example/a"])
        self.assertEqual(self._links(self.c), [])

    def test_one_account_cannot_evict_anothers_entries(self) -> None:
        self._store(self.a, ["https://x.example/keep"])
        for batch in range(3):
            self._store(self.b, [f"https://y.example/{batch}/{i}" for i in range(20)])
        row = LocationCache.objects.get(location=self.loc, source=USER_PROVIDED_SOURCE).data["results"]
        self.assertIn("https://x.example/keep", [r["link"] for r in row])
        self.assertEqual(sum(1 for r in row if r["submitted_by"][0] == self.b.pk), MAX_RESULTS_PER_ACCOUNT)

    def test_other_query_or_audience_is_not_shown(self) -> None:
        self._store(self.a, ["https://x.example/a"], audience="aud1")
        self.assertEqual(self._links(self.a, audience="aud1"), ["https://x.example/a"])
        self.assertEqual(self._links(self.a, audience=""), [])
        self.assertEqual(self._links(self.a, query="other", audience="aud1"), [])

    def test_legacy_single_id_rows_still_load(self) -> None:
        LocationCache.set(
            self.loc,
            USER_PROVIDED_SOURCE,
            {"results": [{"link": "https://x.example/a", "title": "t", "snippet": "", "submitted_by": self.a.pk}]},
            query_key="q",
        )
        self.assertEqual(self._links(self.a), ["https://x.example/a"])
        self.assertEqual(self._links(self.b), [])


class QueryKeyAndAudienceTests(TestCase):
    setUp = PseViewTests.setUp
    _post = PseViewTests._post
    _csrf = PseViewTests._csrf
    _panel = PseViewTests._panel

    def test_long_name_posts_and_loads(self) -> None:
        self.pin.name = "Mill " + "x" * 245
        self.pin.save()
        self.loc.official_name = self.pin.name
        self.loc.save()
        self.assertEqual(self._post(GOOD).status_code, 200)
        (row,) = LocationCache.objects.filter(source=USER_PROVIDED_SOURCE)
        self.assertLessEqual(len(row.query_key), 255)
        from urbanlens.dashboard.services.search.pin_web_search import pin_web_searches

        searches = pin_web_searches(self.pin)
        self.assertGreater(len(searches[0].query), 255)
        self.assertEqual(row.query_key, searches[0].query_key)
        self.assertIn("https://example.org/a", self._panel().content.decode())

    def test_private_names_are_keyed_by_audience_not_shared(self) -> None:
        from urbanlens.dashboard.services.search.pin_web_search import pin_web_searches

        bare = baker.make(Location, latitude=40.0, longitude=-80.0)
        other_user = baker.make("auth.User")
        other_profile = Profile.objects.get(user=other_user)
        mine = baker.make(Pin, location=bare, profile=self.profile, name="Grandpa's Secret Spot")
        theirs = baker.make(Pin, location=bare, profile=other_profile, name="Different Private Name")
        mine_search = pin_web_searches(mine)[0]
        theirs_search = pin_web_searches(theirs)[0]
        self.assertFalse(mine_search.scope.is_shared)
        self.assertNotEqual(mine_search.scope.audience, theirs_search.scope.audience)
        url = reverse("pin.web_search.user_provided", args=[mine.slug])
        response = self.client.post(
            url, json.dumps(GOOD), content_type="application/json", HTTP_X_CSRFTOKEN=self._csrf()
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = LocationCache.objects.get(location=bare, source=USER_PROVIDED_SOURCE)
        self.assertEqual(row.audience, mine_search.scope.audience)
        self.assertEqual(
            load_user_provided_results(
                bare, other_user.pk, theirs_search.query_key, set(), theirs_search.scope.audience
            ),
            [],
        )
        self.assertEqual(load_user_provided_results(bare, other_user.pk, theirs_search.query_key, set(), ""), [])
