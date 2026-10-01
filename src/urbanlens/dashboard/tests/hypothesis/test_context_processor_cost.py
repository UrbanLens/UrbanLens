"""A template pays for the context values it reads, not for every processor the site configures.

The navbar badges render a partial that reads one number, and each paid for feature flags, the messages icon and the
account-deletion banner it never shows. Three of them on every page view, and again on every poll.

This file is about one request: what a render reads, and what it re-reads within the same scope. What the *next*
request pays, and which admin acts still reach it, is ``test_page_chrome_costs_the_same_at_any_scale``. The cache that
file describes is why the measurements here clear it between two readings that both have to be cold.
"""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.template import RequestContext, Template
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings, request_cache
from urbanlens.dashboard.models.subscriptions.model import (
    SiteFeature,
    SubscriptionRole,
    UserSubscription,
    grant_subscription,
    user_features,
    user_has_feature,
)

FLAGS = (
    "{{ can_use_ai_features }}{{ show_places_layer }}{{ can_use_web_search }}{{ can_upload_videos }}"
    "{{ can_upload_documents }}{{ show_games_nav }}{{ has_beta_features }}"
)


class ContextProcessorCostTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # The first account in a fresh database is promoted to admin, which skips the lookups.
        self.user = baker.make(User)

    def _render(self, source: str) -> tuple[str, int]:
        """Render *source* as a request would: a user object with nothing cached, and the settings memo armed."""
        request = RequestFactory().get("/")
        request.user = User.objects.get(pk=self.user.pk)
        request_cache.begin_scope()
        try:
            with CaptureQueriesContext(connection) as ctx:
                output = Template(source).render(RequestContext(request, {"unread_count": 3}))
        finally:
            request_cache.end_scope()
        return output, len(ctx.captured_queries)

    def test_a_fragment_that_reads_no_processor_value_runs_no_query(self) -> None:
        output, queries = self._render("{{ unread_count }}")

        self.assertEqual(output, "3")
        self.assertEqual(queries, 0)

    def test_every_feature_flag_together_costs_one_feature_lookup(self) -> None:
        _, flags = self._render(FLAGS)

        cache.clear()  # the feature set is cached across requests, so the second reading has to start cold too
        user = User.objects.get(pk=self.user.pk)
        request_cache.begin_scope()
        try:
            with CaptureQueriesContext(connection) as lookup:
                user_features(user)
        finally:
            request_cache.end_scope()

        self.assertGreater(len(lookup.captured_queries), 0, "one lookup costs nothing, so the comparison is vacuous")
        self.assertEqual(flags, len(lookup.captured_queries))

    def test_a_value_read_twice_is_computed_once(self) -> None:
        _, once = self._render("{% if show_messages_icon %}y{% endif %}")
        _, twice = self._render("{% if show_messages_icon %}y{% endif %}{% if show_messages_icon %}y{% endif %}")

        self.assertGreater(once, 0, "the icon is decided by a query, so the zero above is not vacuous")
        self.assertEqual(twice, once)

    def test_filters_receive_the_value_rather_than_a_proxy(self) -> None:
        Profile.objects.filter(user=self.user).update(keyboard_shortcuts={"search": "/"})

        output, _ = self._render('{{ keyboard_shortcuts|json_script:"k" }}')

        self.assertIn('{"search": "/"}', output)


class TheFeatureSetTests(TestCase):
    def test_a_member_has_the_site_default_and_an_admin_has_everything(self) -> None:
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(default_features="places")
        baker.make(User)
        member = User.objects.get(pk=baker.make(User).pk)
        admin = User.objects.get(pk=baker.make(User, is_superuser=True, is_active=True).pk)

        self.assertEqual(set(user_features(member)), {"places"})
        self.assertEqual(set(user_features(admin)), set(SiteFeature.values))
        self.assertEqual({feature for feature in SiteFeature.values if user_has_feature(member, feature)}, {"places"})


class OneRequestAsksAboutFeaturesOnceTests(TestCase):
    """Views check features directly as well as through the template flags; a request should look them up once."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)

    def _queries_within_a_request(self, check: Callable[[], object]) -> int:
        request_cache.begin_scope()
        try:
            with CaptureQueriesContext(connection) as ctx:
                check()
        finally:
            request_cache.end_scope()
        return len(ctx.captured_queries)

    def test_every_check_after_the_first_is_free(self) -> None:
        fresh = User.objects.get(pk=self.user.pk)
        one = self._queries_within_a_request(lambda: user_features(fresh))
        user = User.objects.get(pk=self.user.pk)

        every = self._queries_within_a_request(
            lambda: ([user_has_feature(user, feature) for feature in SiteFeature.values], user_features(user))
        )

        self.assertGreater(one, 0, "the first check cost nothing, so the zero below proves nothing")
        self.assertEqual(every, 0, "a later check re-read the feature set, which every page and poll would pay for")

    def test_a_grant_made_during_the_request_is_seen_by_the_next_check(self) -> None:
        role = baker.make(SubscriptionRole, features=SiteFeature.BETA_FEATURES)
        request_cache.begin_scope()
        try:
            self.assertFalse(user_has_feature(self.user, SiteFeature.BETA_FEATURES))
            grant_subscription(self.user, role, self.user, None)
            self.assertTrue(user_has_feature(self.user, SiteFeature.BETA_FEATURES))
        finally:
            request_cache.end_scope()

    def test_a_bulk_update_reaches_the_next_check(self) -> None:
        """The shape the site admin's own revoke takes, which sends no ``post_save`` of its own.

        ``controllers/site_admin.py`` revokes with ``UserSubscription.objects.filter(pk=...).update(...)``, and
        editing a role's features is the same shape. The bump is deferred to the commit, which a ``TestCase`` only
        reaches through ``captureOnCommitCallbacks`` - without it this passes against a cache nothing ever retires.
        """
        role = baker.make(SubscriptionRole, features=SiteFeature.BETA_FEATURES)
        subscription = grant_subscription(self.user, role, self.user, None)
        with self.captureOnCommitCallbacks(execute=True):
            subscription.revoke()
        self.assertFalse(user_has_feature(self.user, SiteFeature.BETA_FEATURES))

        with self.captureOnCommitCallbacks(execute=True):
            UserSubscription.objects.filter(pk=subscription.pk).update(revoked_at=None)

        self.assertTrue(user_has_feature(self.user, SiteFeature.BETA_FEATURES))
