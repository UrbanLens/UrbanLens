"""Nothing in the web tier limits how often an anonymous caller may ask.

`CustomLoginView` is the exception and is deliberately not covered here: it
already carries per-identifier and per-IP failure lockouts of its own, so a
second limiter in front of it would be two gates disagreeing about one rule.

`external_api/throttling.py` is rich and DRF-only; `services/core/rate_limiter.py`
caps *outbound* third-party spend. Between them there was no inbound limit on a
plain Django view, which is why `POST /signup/` - about 1.1s of PBKDF2 CPU per
request, measured by the slow-request middleware - could be called in a loop by
anyone, occupying the gunicorn workers every signed-in user shares. That is the
governing requirement failing to an attacker with no account.

Two properties here are load-bearing and easy to get backwards:

**It fails open.** A throttle that refuses when it cannot read its counter turns
a Valkey outage into a site-wide lockout, and P105 already records that a Valkey
outage 500s every request. An abuse control is not worth an availability
incident, so an unreachable cache means "allowed" - and that is asserted, not
assumed.

**The window is keyed, not shared.** Two callers, two counters; two scopes, two
counters. A throttle that pools callers is a denial-of-service tool pointed at
the site by whoever hits it first.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.security import throttle


class TheCounterTests(TestCase):
    """The primitive, exercised directly."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_calls_under_the_limit_are_allowed(self) -> None:
        rate = throttle.Rate(limit=3, window_seconds=60)
        self.assertTrue(all(throttle.allow("scope", "1.2.3.4", rate) for _ in range(3)))

    def test_the_call_over_the_limit_is_refused(self) -> None:
        rate = throttle.Rate(limit=3, window_seconds=60)
        for _ in range(3):
            throttle.allow("scope", "1.2.3.4", rate)
        self.assertFalse(throttle.allow("scope", "1.2.3.4", rate))

    def test_two_callers_do_not_share_a_budget(self) -> None:
        rate = throttle.Rate(limit=2, window_seconds=60)
        for _ in range(2):
            throttle.allow("scope", "1.2.3.4", rate)
        self.assertTrue(throttle.allow("scope", "5.6.7.8", rate), "one caller exhausted another caller's budget")

    def test_two_scopes_do_not_share_a_budget(self) -> None:
        rate = throttle.Rate(limit=2, window_seconds=60)
        for _ in range(2):
            throttle.allow("signup", "1.2.3.4", rate)
        self.assertTrue(throttle.allow("login", "1.2.3.4", rate), "one endpoint exhausted another endpoint's budget")

    def test_an_unreachable_cache_allows_the_call(self) -> None:
        """P105's lesson: an abuse control must not become an outage."""
        rate = throttle.Rate(limit=1, window_seconds=60)
        with mock.patch.object(throttle, "_cache_add", side_effect=ConnectionError("valkey is gone")):
            self.assertTrue(throttle.allow("scope", "1.2.3.4", rate))
            self.assertTrue(throttle.allow("scope", "1.2.3.4", rate))

    def test_a_refusal_says_how_long_to_wait(self) -> None:
        """Never zero and never longer than the window - the exact second
        depends on where in the window the call landed, so asserting one would
        be asserting the clock."""
        rate = throttle.Rate(limit=1, window_seconds=45)
        throttle.allow("scope", "1.2.3.4", rate)
        wait = throttle.retry_after("scope", "1.2.3.4", rate)
        self.assertGreaterEqual(wait, 1)
        self.assertLessEqual(wait, 45)


class TheViewDecoratorTests(TestCase):
    """What a view gets when it is over the limit."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.factory = RequestFactory()

    def _view(self, limit: int = 1):
        @throttle.throttled("probe", throttle.Rate(limit=limit, window_seconds=60))
        def view(request):  # noqa: ANN001, ANN202
            from django.http import HttpResponse

            return HttpResponse("ok")

        return view

    def test_the_first_call_reaches_the_view(self) -> None:
        response = self._view()(self.factory.post("/probe/"))
        self.assertEqual(response.status_code, 200)

    def test_the_call_over_the_limit_is_refused_with_429(self) -> None:
        view = self._view()
        view(self.factory.post("/probe/"))
        response = view(self.factory.post("/probe/"))
        self.assertEqual(response.status_code, 429)

    def test_the_refusal_carries_retry_after(self) -> None:
        view = self._view()
        view(self.factory.post("/probe/"))
        response = view(self.factory.post("/probe/"))
        self.assertTrue(response.headers.get("Retry-After"), "a 429 with no Retry-After tells a client nothing")

    def test_the_view_body_never_runs_when_refused(self) -> None:
        """The whole point: the expensive work must not happen."""
        calls = []

        @throttle.throttled("probe", throttle.Rate(limit=1, window_seconds=60))
        def view(request):  # noqa: ANN001, ANN202
            from django.http import HttpResponse

            calls.append(1)
            return HttpResponse("ok")

        view(self.factory.post("/probe/"))
        view(self.factory.post("/probe/"))
        self.assertEqual(len(calls), 1, "the throttled call still ran the view")

    def test_the_wrapper_keeps_what_django_attached_to_as_view(self) -> None:
        """`view_class` and friends live in the callable's `__dict__`, and Django
        and its tooling read them off whatever the URLconf holds."""
        from django.views.generic import View

        class Probe(View):
            pass

        wrapped = throttle.throttled("probe", throttle.Rate(limit=1, window_seconds=60))(Probe.as_view())
        self.assertIs(getattr(wrapped, "view_class", None), Probe)

    def test_a_get_is_not_counted_by_default(self) -> None:
        """Rendering the form is cheap; submitting it is what costs PBKDF2."""
        view = self._view()
        view(self.factory.post("/probe/"))
        self.assertEqual(view(self.factory.get("/probe/")).status_code, 200)


class TheExpensiveAnonymousEndpointsAreCoveredTests(TestCase):
    """The reason the primitive exists, asserted on the real routes."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def _post_until_refused(self, url: str, data: dict[str, str], ceiling: int = 60) -> int:
        for attempt in range(1, ceiling + 1):
            if self.client.post(url, data).status_code == 429:
                return attempt
        return 0

    def test_signup_stops_accepting_before_it_has_hashed_sixty_passwords(self) -> None:
        refused_at = self._post_until_refused(
            reverse("signup"),
            {"username": "throttle-probe", "email": "t@example.com", "password1": "x", "password2": "y"},
        )
        self.assertTrue(refused_at, "signup accepted 60 posts; each costs ~1.1s of PBKDF2 on a shared worker")

    def test_password_reset_stops_before_it_has_sent_sixty_emails(self) -> None:
        refused_at = self._post_until_refused(reverse("password_reset"), {"email": "t@example.com"})
        self.assertTrue(refused_at, "password reset accepted 60 posts, each a synchronous SMTP send")

    def test_resend_verification_stops_too(self) -> None:
        refused_at = self._post_until_refused(reverse("resend_verification"), {"email": "t@example.com"})
        self.assertTrue(refused_at, "resend-verification accepted 60 posts, each a synchronous SMTP send")
