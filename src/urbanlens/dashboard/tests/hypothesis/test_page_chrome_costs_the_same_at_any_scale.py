"""What the navbar every page renders costs, and that an event still reaches it.

At 1,000 concurrent users the app container ran 3.71 of its four cores and was CPU-throttled 31.6%
of the time while the database used 0.91 of its own four (``tests/perf/results/before2-*``), so
what bounds concurrent users here is work per request, not SQL. The navbar is on every page and
its two dearest values - the viewer's feature set and the dev-toolbar verdict - are derived from
rows that change when an admin acts, not when a user browses.

Caching those is only safe if the events that change them still reach the cache, so the staleness
half of this file is not an extra: it is the part that earns the other half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import Group, Permission, User
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import request_cache
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_features

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

#: Statements the viewer's access state may cost after something has already read it once. A
#: ratchet, not a target: when a change lowers it, lower it here.
WARM_STATEMENT_CEILING = 0

#: The settings singleton, which the dev-toolbar verdict is reached through. It is memoised per
#: request and shared by three context processors and the controller, so a page pays for it once
#: however many of them ask; only the verdict itself is this file's business.
SETTINGS_ROW = 1


class _AccessStateCase(TestCase):
    """A plain member, and the machinery to read their access state from a cold request."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.user = baker.make(User)
        self.factory = RequestFactory()
        SiteSettings.get_current()

    def cold(self, read: Callable[[User], Any]) -> tuple[Any, int]:
        """*read*'s answer for a request that has not already paid for it, and what it cost.

        The user is re-loaded because Django memoises permissions on the instance, and the
        statements are counted through ``CaptureQueriesContext`` because ``connection.queries_log``
        only fills when something has forced the debug cursor - counting it directly reads zero
        under a plain test run, which makes a ceiling assertion pass without measuring anything.

        Args:
            read: Called with a freshly loaded user, as a context processor would be.

        Returns:
            The answer, and the number of statements it issued.
        """
        request_cache.begin_scope()
        user = User.objects.get(pk=self.user.pk)
        with CaptureQueriesContext(connection) as captured:
            answer = read(user)
        return answer, len(captured.captured_queries)

    def committed(self) -> AbstractContextManager[list[Any]]:
        """A block whose writes reach the invalidation, the way a real request's would.

        The bump is deferred to the transaction's commit on purpose (see
        :func:`~urbanlens.dashboard.models.subscriptions.access_state.forget`), and a ``TestCase``
        never commits - so a staleness test that wrote without this would be asserting against a
        path production does not take, and would pass whether or not the invalidation works.
        """
        return self.captureOnCommitCallbacks(execute=True)


class PageChromeIsPaidForOnceTests(_AccessStateCase):
    """The feature set and the dev-toolbar verdict are a property of the account, not of the page."""

    def test_a_second_request_does_not_ask_the_database_again(self) -> None:
        self.cold(user_features)
        _, statements = self.cold(user_features)
        self.assertLessEqual(
            statements,
            WARM_STATEMENT_CEILING,
            f"a second request re-read the viewer's feature set in {statements} statements. Every authenticated "
            f"page renders the navbar, so this is paid per navigation by every concurrent user.",
        )

    def test_the_dev_toolbar_verdict_is_paid_for_by_the_same_read(self) -> None:
        def verdict(user: User) -> bool:
            return SiteSettings.get_current().show_dev_admin_features(user)

        self.cold(verdict)
        _, statements = self.cold(verdict)
        self.assertLessEqual(
            statements,
            SETTINGS_ROW,
            f"the dev-toolbar verdict cost {statements} statements on a warm read, against {SETTINGS_ROW} for the "
            f"settings row it is reached through. It asks the same question as the feature set - whether this "
            f"account is a site admin - so it should not be asking the database again itself.",
        )


class AnAdminChangeReachesTheChromeTests(_AccessStateCase):
    """Whatever the chrome remembers, an act that changes a viewer's access must still land."""

    def assertSeesFeature(self, *, expected: bool, after: str) -> None:
        features, _ = self.cold(user_features)
        self.assertEqual(
            SiteFeature.AI.value in features,
            expected,
            f"after {after} the viewer's feature set was {sorted(features)}, which is what they would see on "
            f"their next page load",
        )

    def test_a_site_default_gaining_a_feature_reaches_a_viewer_who_already_loaded_a_page(self) -> None:
        self.assertSeesFeature(expected=False, after="no change")
        site = SiteSettings.get_current()
        with self.committed():
            site.default_features = SiteFeature.AI.value
            site.save()
        self.assertSeesFeature(expected=True, after="the site default gained it")

    def test_a_site_default_losing_a_feature_reaches_them_too(self) -> None:
        site = SiteSettings.get_current()
        with self.committed():
            site.default_features = SiteFeature.AI.value
            site.save()
        self.assertSeesFeature(expected=True, after="the site default granted it")
        with self.committed():
            site.default_features = ""
            site.save()
        self.assertSeesFeature(expected=False, after="the site default withdrew it")

    def test_losing_the_admin_permission_reaches_them(self) -> None:
        """The one that has to hold: an account stripped of site admin must stop being treated as one."""
        permission = Permission.objects.get(codename="view_site_admin")
        with self.committed():
            self.user.user_permissions.add(permission)
        self.assertSeesFeature(expected=True, after="the account was granted site admin")

        with self.committed():
            self.user.user_permissions.remove(permission)
        self.assertSeesFeature(expected=False, after="site admin was revoked")

    def test_losing_the_admin_permission_through_a_group_reaches_them(self) -> None:
        group = baker.make(Group)
        with self.committed():
            group.permissions.add(Permission.objects.get(codename="view_site_admin"))
            self.user.groups.add(group)
        self.assertSeesFeature(expected=True, after="a group granted site admin")

        with self.committed():
            group.permissions.clear()
        self.assertSeesFeature(expected=False, after="the group lost the permission")

    def test_the_scope_that_changed_access_stops_trusting_the_shared_cache(self) -> None:
        """The writer's own request cannot wait for the bump, because the bump waits for its commit."""
        self.cold(user_features)

        request_cache.begin_scope()
        site = SiteSettings.get_current()
        site.default_features = SiteFeature.AI.value
        site.save()
        user = User.objects.get(pk=self.user.pk)
        with CaptureQueriesContext(connection) as captured:
            features = user_features(user)
        request_cache.end_scope()

        self.assertGreater(
            len(captured.captured_queries), 0, "the read was served from a cache the change has not reached"
        )
        self.assertIn(
            SiteFeature.AI.value,
            sorted(features),
            "a request that granted a feature did not see it itself, so the admin who made the change is shown the "
            "state they just replaced",
        )

    def test_being_deactivated_reaches_them(self) -> None:
        with self.committed():
            self.user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.assertSeesFeature(expected=True, after="the account was granted site admin")

        with self.committed():
            self.user.is_active = False
            self.user.save()
        self.assertSeesFeature(expected=False, after="the account was deactivated")


class TheMeasurementIsRealTests(_AccessStateCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_a_cold_read_does_ask_the_database(self) -> None:
        _, statements = self.cold(user_features)
        self.assertGreater(statements, 0, "the first read cost nothing, so the warm read proves nothing")

    def test_the_viewer_is_not_an_admin_to_start_with(self) -> None:
        features, _ = self.cold(user_features)
        self.assertNotIn(SiteFeature.AI.value, features, "the viewer already had the feature the staleness tests grant")
