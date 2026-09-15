"""A template pays for the context values it reads, not for every processor the site configures.

The navbar badges render a partial that reads one number, and each paid for feature flags, the messages icon and the
account-deletion banner it never shows. Three of them on every page view, and again on every poll.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.template import RequestContext, Template
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings, request_cache
from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature

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
        from urbanlens.dashboard.models.subscriptions.model import user_features

        user = User.objects.get(pk=self.user.pk)
        request_cache.begin_scope()
        try:
            with CaptureQueriesContext(connection) as lookup:
                user_features(user)
        finally:
            request_cache.end_scope()

        _, flags = self._render(FLAGS)

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
    def test_it_agrees_with_asking_one_feature_at_a_time(self) -> None:
        from urbanlens.dashboard.models.subscriptions.model import user_features

        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(default_features="places")
        baker.make(User)
        member = User.objects.get(pk=baker.make(User).pk)
        admin = User.objects.get(pk=baker.make(User, is_superuser=True, is_active=True).pk)

        for user in (member, admin):
            with self.subTest(superuser=user.is_superuser):
                asked = {feature for feature in SiteFeature.values if user_has_feature(user, feature)}
                self.assertEqual(set(user_features(user)), asked)
        self.assertEqual(set(user_features(member)), {"places"})
