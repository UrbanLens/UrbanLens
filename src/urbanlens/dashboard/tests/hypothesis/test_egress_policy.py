"""The egress policy itself (D26): which category each environment may call, and on what share.

Every UrbanLens host shares one residential address with production REData, so a keyless API's per-address
tolerance and a billed API's free tier are one budget, and production is meant to spend it. These pin the
table in ``urbanlens.UrbanLens.egress`` without the rest of the app.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.egress import (
    BEAT_EGRESS,
    CONSOLE_EMAIL_BACKEND,
    ENVIRONMENT_SHARE_DEFAULTS,
    SMTP_EMAIL_BACKEND,
    BeatEgress,
    EgressCategory,
    decide,
    default_environment_share,
    email_delivery_backend,
    hosted_basemap_key,
    parse_share_overrides,
    policy_environment,
    scaled_limit,
    scheduled_beat_entries,
)

PRODUCTION, STAGING, DEVELOPMENT, LOCAL, TESTING = "production", "staging", "development", "local", "testing"
DEPLOYMENTS = (PRODUCTION, STAGING, DEVELOPMENT, LOCAL)


def _decide(
    category: EgressCategory, environment: str, *, overrides: dict[str, float] | None = None, service: str = "svc"
):
    return decide(
        service,
        category,
        environment,
        environment_share=default_environment_share(environment),
        overrides=overrides or {},
    )


class TheTableTests(SimpleTestCase):
    """Each category against each environment, with the default share."""

    def test_redata_internal_and_ai_are_allowed_everywhere_unscaled(self) -> None:
        for category in (EgressCategory.REDATA, EgressCategory.INTERNAL, EgressCategory.AI):
            for environment in (*DEPLOYMENTS, TESTING):
                with self.subTest(category=category, environment=environment):
                    decision = _decide(category, environment)
                    self.assertTrue(decision.allowed)
                    self.assertEqual(decision.share, 1.0)

    def test_quota_and_billed_take_the_environment_s_share(self) -> None:
        expected = {PRODUCTION: 0.9, STAGING: 0.05, DEVELOPMENT: 0.0, LOCAL: 0.0, TESTING: 1.0}
        for category in (EgressCategory.QUOTA, EgressCategory.BILLED):
            for environment, share in expected.items():
                with self.subTest(category=category, environment=environment):
                    decision = _decide(category, environment)
                    self.assertEqual(decision.share, share)
                    self.assertEqual(decision.allowed, share > 0)

    def test_development_refuses_quota_and_billed(self) -> None:
        for category in (EgressCategory.QUOTA, EgressCategory.BILLED):
            for environment in (DEVELOPMENT, LOCAL):
                with self.subTest(category=category, environment=environment):
                    self.assertFalse(_decide(category, environment).allowed)

    def test_messaging_and_public_writes_are_production_s_alone(self) -> None:
        for category in (EgressCategory.MESSAGING, EgressCategory.PUBLIC_WRITE):
            with self.subTest(category=category):
                self.assertTrue(_decide(category, PRODUCTION).allowed)
                self.assertTrue(_decide(category, TESTING).allowed)
                for environment in (STAGING, DEVELOPMENT, LOCAL):
                    self.assertFalse(_decide(category, environment).allowed, environment)

    def test_an_unknown_environment_gets_nothing_restricted(self) -> None:
        for category in (
            EgressCategory.QUOTA,
            EgressCategory.BILLED,
            EgressCategory.MESSAGING,
            EgressCategory.PUBLIC_WRITE,
        ):
            with self.subTest(category=category):
                self.assertFalse(_decide(category, "someones-laptop").allowed)

    def test_the_deployment_shares_sum_to_at_most_one(self) -> None:
        self.assertLessEqual(sum(ENVIRONMENT_SHARE_DEFAULTS[environment] for environment in DEPLOYMENTS), 1.0)


class OverrideTests(SimpleTestCase):
    """``UL_ENVIRONMENT_SHARE_OVERRIDES``: how a provider is tried from development or staging."""

    def test_an_override_opts_one_quota_service_in_on_development(self) -> None:
        decision = _decide(EgressCategory.QUOTA, DEVELOPMENT, overrides={"nominatim": 0.02}, service="nominatim")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.share, 0.02)
        self.assertTrue(decision.overridden)

    def test_it_names_one_service_only(self) -> None:
        self.assertFalse(
            _decide(EgressCategory.QUOTA, DEVELOPMENT, overrides={"nominatim": 0.02}, service="wikipedia").allowed
        )

    def test_an_override_opts_messaging_in_off_production(self) -> None:
        self.assertTrue(_decide(EgressCategory.MESSAGING, STAGING, overrides={"sms": 1.0}, service="sms").allowed)

    def test_zero_switches_a_service_off_on_production(self) -> None:
        self.assertFalse(
            _decide(EgressCategory.BILLED, PRODUCTION, overrides={"google_maps": 0.0}, service="google_maps").allowed
        )
        self.assertFalse(_decide(EgressCategory.MESSAGING, PRODUCTION, overrides={"sms": 0.0}, service="sms").allowed)

    def test_an_unrestricted_category_ignores_it(self) -> None:
        """REData, our own hosts and AI are switched off on the API-limits page, not by environment."""
        self.assertTrue(
            _decide(EgressCategory.REDATA, DEVELOPMENT, overrides={"redata_api": 0.0}, service="redata_api").allowed
        )

    def test_parsing(self) -> None:
        self.assertEqual(parse_share_overrides(" nominatim=0.02, SMS=1 "), {"nominatim": 0.02, "sms": 1.0})
        self.assertEqual(parse_share_overrides(""), {})
        self.assertEqual(parse_share_overrides(None), {})

    def test_malformed_input_is_refused_at_startup(self) -> None:
        for raw in (
            "nominatim",
            "=0.1",
            "nominatim=lots",
            "nominatim=1.5",
            "nominatim=-0.1",
            "nominatim=nan",
            "a=0.1,a=0.2",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_share_overrides(raw)


class ScaledLimitTests(SimpleTestCase):
    def test_a_share_scales_a_window_down_but_never_to_nothing(self) -> None:
        self.assertEqual(scaled_limit(500, 0.05), 25)
        self.assertEqual(scaled_limit(20, 0.9), 18)
        self.assertEqual(scaled_limit(1, 0.05), 1)

    def test_no_limit_a_zero_limit_and_the_whole_share_are_unchanged(self) -> None:
        self.assertIsNone(scaled_limit(None, 0.05))
        self.assertEqual(scaled_limit(0, 0.05), 0)
        self.assertEqual(scaled_limit(500, 1.0), 500)


class PolicyEnvironmentTests(SimpleTestCase):
    def test_a_test_run_is_testing_whatever_its_container_says(self) -> None:
        self.assertEqual(policy_environment("development", testing=True), TESTING)

    def test_otherwise_it_is_the_environment(self) -> None:
        self.assertEqual(policy_environment(" Staging ", testing=False), STAGING)
        self.assertEqual(policy_environment(None, testing=False), "")


class EmailBackendTests(SimpleTestCase):
    """Off production, mail is printed to the log unless real delivery is asked for."""

    def test_production_sends_by_default_and_honours_the_configured_backend(self) -> None:
        self.assertEqual(
            email_delivery_backend(PRODUCTION, configured=None, send_outside_production=False), SMTP_EMAIL_BACKEND
        )
        self.assertEqual(
            email_delivery_backend(PRODUCTION, configured="x.Backend", send_outside_production=False), "x.Backend"
        )

    def test_staging_and_development_print_even_when_smtp_is_configured(self) -> None:
        """Development's .env names SMTP explicitly; that alone no longer sends."""
        for environment in (STAGING, DEVELOPMENT, LOCAL):
            with self.subTest(environment=environment):
                self.assertEqual(
                    email_delivery_backend(environment, configured=SMTP_EMAIL_BACKEND, send_outside_production=False),
                    CONSOLE_EMAIL_BACKEND,
                )

    def test_the_explicit_opt_in_restores_real_delivery(self) -> None:
        self.assertEqual(
            email_delivery_backend(STAGING, configured=None, send_outside_production=True), SMTP_EMAIL_BACKEND
        )
        self.assertEqual(
            email_delivery_backend(DEVELOPMENT, configured="x.Backend", send_outside_production=True), "x.Backend"
        )

    def test_the_suite_never_defaults_to_sending(self) -> None:
        self.assertEqual(
            email_delivery_backend(TESTING, configured=None, send_outside_production=False), CONSOLE_EMAIL_BACKEND
        )


class HostedBasemapTests(SimpleTestCase):
    def test_only_production_buys_the_hosted_basemap(self) -> None:
        self.assertEqual(hosted_basemap_key(PRODUCTION, "key"), "key")
        for environment in (STAGING, DEVELOPMENT, LOCAL):
            with self.subTest(environment=environment):
                self.assertEqual(hosted_basemap_key(environment, "key"), "")


class BeatScheduleTests(SimpleTestCase):
    """Off production only internal entries are scheduled, plus the allow-listed ones."""

    def setUp(self) -> None:
        super().setUp()
        from django.conf import settings

        self.full = settings.FULL_BEAT_SCHEDULE

    def test_production_runs_everything(self) -> None:
        self.assertEqual(set(scheduled_beat_entries(self.full, PRODUCTION, ())), set(self.full))

    def test_staging_and_development_run_only_internal_entries(self) -> None:
        internal = {name for name in self.full if BEAT_EGRESS[name] is BeatEgress.INTERNAL}
        for environment in (STAGING, DEVELOPMENT, LOCAL):
            with self.subTest(environment=environment):
                self.assertEqual(set(scheduled_beat_entries(self.full, environment, ())), internal)

    def test_the_sweeps_that_reach_out_are_off_and_internal_maintenance_stays_on(self) -> None:
        scheduled = scheduled_beat_entries(self.full, STAGING, ())
        for name in (
            "wayback-archive-sweep",
            "scheduled-trivia-generation",
            "scheduled-trivia-wiki-incorporation",
            "calendar-push-sweep",
            "stripe-subscriptions-sync",
        ):
            self.assertNotIn(name, scheduled)
        for name in (
            "task-outbox-drain",
            "provider-health-evaluation",
            "safety-checkin-escalation",
            "spotguessr-stall-sweep",
            "api-call-log-pruning",
        ):
            self.assertIn(name, scheduled)

    def test_an_allow_listed_entry_runs(self) -> None:
        scheduled = scheduled_beat_entries(self.full, DEVELOPMENT, ("wayback-archive-sweep",))
        self.assertIn("wayback-archive-sweep", scheduled)
        self.assertNotIn("calendar-push-sweep", scheduled)

    def test_an_unclassified_entry_counts_as_external(self) -> None:
        schedule = {"something-new": {"task": "x", "schedule": 60}}
        self.assertEqual(scheduled_beat_entries(schedule, STAGING, ()), {})
        self.assertEqual(set(scheduled_beat_entries(schedule, PRODUCTION, ())), {"something-new"})

    def test_the_suite_sees_the_whole_schedule(self) -> None:
        from django.conf import settings

        self.assertEqual(set(settings.CELERY_BEAT_SCHEDULE), set(self.full))
