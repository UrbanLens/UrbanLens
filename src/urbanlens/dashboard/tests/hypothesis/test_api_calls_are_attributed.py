"""Every external API call records whose behalf it was made on.

A service budget is spent by people, but ``ApiCallLog`` recorded only *that* a
call happened. When a shared quota ran out - the tile proxy's 500/day, say -
nothing in the system could answer who had spent it, so the only available
lever was a fixed per-user cap: divide the budget by the user count and hand
everyone a slice, which throttles ordinary users to protect quota that nobody
is using.

Attribution is the prerequisite for anything better. With it, a limiter can
ask what it actually needs to know - how many people are using this service
right now, and how much of the window is left - and only restrain a heavy user
when someone else is competing for the same budget.

Two properties the fair-share design in D14 rests on:

- **A refusal is attributed too.** Rows for blocked calls are the record of
  demand that went unmet; an unattributed one cannot tell you whose demand it
  was, which is precisely the question when a quota runs out.
- **Background work belongs to nobody.** The site's own scheduled calls are not
  a user's consumption and must not be charged to one.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError, _reserve_call, log_api_call

SERVICE = "test_attribution_service"


class ApiCallAttributionTests(TestCase):
    """The log names the profile a call was made for."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        ApiRateLimit.objects.update_or_create(
            service=SERVICE,
            defaults={"calls_per_minute": 2, "calls_per_day": None, "calls_per_30_days": None, "enabled": True},
        )

    def test_a_call_inside_a_users_request_is_charged_to_that_user(self) -> None:
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            _reserve_call(SERVICE, endpoint="/probe")
        entry = ApiCallLog.objects.for_service(SERVICE).latest("created")
        self.assertEqual(entry.profile_id, self.profile.pk)

    def test_background_work_is_charged_to_nobody(self) -> None:
        with writing_as(WriteSource.AUTOMATIC, actor=None):
            _reserve_call(SERVICE, endpoint="/probe")
        entry = ApiCallLog.objects.for_service(SERVICE).latest("created")
        self.assertIsNone(entry.profile_id)

    def test_a_refused_call_still_names_who_wanted_it(self) -> None:
        """Unmet demand is the signal a fair-share limiter needs most."""
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            _reserve_call(SERVICE, endpoint="/one")
            _reserve_call(SERVICE, endpoint="/two")
            with pytest.raises(RateLimitExceededError):
                _reserve_call(SERVICE, endpoint="/three")

        refused = ApiCallLog.objects.for_service(SERVICE).rate_limited()
        self.assertEqual(refused.count(), 1)
        self.assertEqual(refused.first().profile_id, self.profile.pk)

    def test_the_bare_logging_helper_attributes_too(self) -> None:
        """Services that bypass the session log themselves; they must not lose the actor."""
        with writing_as(WriteSource.USER, actor=self.profile.pk):
            log_api_call(SERVICE, endpoint="/sdk")
        self.assertEqual(ApiCallLog.objects.for_service(SERVICE).latest("created").profile_id, self.profile.pk)


class ServiceDemandReportingTests(TestCase):
    """Who is using a service, and how much - the measurement H34 needs."""

    def setUp(self) -> None:
        super().setUp()
        self.heavy = baker.make(User).profile
        self.light = baker.make(User).profile

    def _log(self, profile, count: int) -> None:
        for index in range(count):
            with writing_as(WriteSource.USER, actor=profile.pk if profile else None):
                log_api_call(SERVICE, endpoint=f"/{index}")

    def test_usage_by_profile_ranks_the_windows_consumers(self) -> None:
        self._log(self.heavy, 5)
        self._log(self.light, 1)
        self._log(None, 3)

        usage = ApiCallLog.objects.for_service(SERVICE).usage_by_profile(timedelta(minutes=5))
        self.assertEqual(usage, [(self.heavy.pk, 5), (self.light.pk, 1)])

    def test_unattributed_calls_are_not_charged_to_a_user(self) -> None:
        self._log(None, 4)
        self.assertEqual(ApiCallLog.objects.for_service(SERVICE).usage_by_profile(timedelta(minutes=5)), [])

    def test_active_consumers_counts_distinct_people_not_calls(self) -> None:
        self._log(self.heavy, 9)
        self._log(self.light, 1)
        self.assertEqual(ApiCallLog.objects.for_service(SERVICE).active_consumers(timedelta(minutes=5)), 2)
