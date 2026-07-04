"""Tests for SiteSettings environment override resolution."""
from __future__ import annotations

import os
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import EnvironmentOverrideChoice, SiteSettings
from urbanlens.dashboard.services.site_admin import add_user_to_site_admin_group
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes
from urbanlens.UrbanLens.settings.app import settings as app_settings

_hyp = settings(max_examples=30, deadline=None)


class SiteSettingsEnvironmentTests(TestCase):
    """SiteSettings.get_effective_environment_type() honors override and env var."""

    def setUp(self) -> None:
        self.site = SiteSettings.get_current()

    def test_default_uses_ul_environment_when_set(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.DEFAULT,
        )
        self.site.refresh_from_db()
        with patch.dict(os.environ, {"UL_ENVIRONMENT": "production"}):
            self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.PRODUCTION)

    def test_default_falls_back_to_local_without_env_var(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.DEFAULT,
        )
        self.site.refresh_from_db()
        stripped = {k: v for k, v in os.environ.items() if k != "UL_ENVIRONMENT"}
        with patch.dict(os.environ, stripped, clear=True):
            self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.LOCAL)

    def test_development_override_wins_over_env_var(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.DEVELOPMENT,
        )
        self.site.refresh_from_db()
        with patch.dict(os.environ, {"UL_ENVIRONMENT": "production"}):
            self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.DEVELOPMENT)
            self.assertTrue(self.site.is_development_environment())

    def test_production_override(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.PRODUCTION,
        )
        self.site.refresh_from_db()
        self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.PRODUCTION)
        self.assertFalse(self.site.is_development_environment())

    def test_testing_override(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.TESTING,
        )
        self.site.refresh_from_db()
        self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.TESTING)

    def test_staging_override(self) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(
            environment_override=EnvironmentOverrideChoice.STAGING,
        )
        self.site.refresh_from_db()
        self.assertEqual(self.site.get_effective_environment_type(), EnvironmentTypes.STAGING)

    @given(
        st.sampled_from([
            EnvironmentOverrideChoice.PRODUCTION,
            EnvironmentOverrideChoice.DEVELOPMENT,
            EnvironmentOverrideChoice.TESTING,
        ]),
    )
    @_hyp
    def test_explicit_override_ignores_env_var(self, override: str) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(environment_override=override)
        self.site.refresh_from_db()
        with patch.dict(os.environ, {"UL_ENVIRONMENT": "staging"}):
            effective = self.site.get_effective_environment_type()
            self.assertNotEqual(effective, EnvironmentTypes.STAGING)


class IsDevelopmentEnvironmentTests(TestCase):
    """is_development_environment() is True for DEVELOPMENT and LOCAL, False otherwise."""

    def setUp(self) -> None:
        self.site = SiteSettings.get_current()

    def _set_override(self, override: str) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(environment_override=override)
        self.site.refresh_from_db()

    def test_development_override_is_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEVELOPMENT)
        self.assertTrue(self.site.is_development_environment())

    def test_production_override_is_not_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.PRODUCTION)
        self.assertFalse(self.site.is_development_environment())

    def test_staging_override_is_not_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.STAGING)
        self.assertFalse(self.site.is_development_environment())

    def test_testing_override_is_not_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.TESTING)
        self.assertFalse(self.site.is_development_environment())

    def test_default_with_local_env_var_is_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEFAULT)
        stripped = {k: v for k, v in os.environ.items() if k != "UL_ENVIRONMENT"}
        with patch.dict(os.environ, stripped, clear=True):
            # No UL_ENVIRONMENT → resolves to LOCAL, which is treated as dev.
            self.assertTrue(self.site.is_development_environment())

    def test_default_with_ul_environment_local_is_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEFAULT)
        with patch.dict(os.environ, {"UL_ENVIRONMENT": "local"}):
            self.assertTrue(self.site.is_development_environment())

    def test_default_with_ul_environment_production_is_not_dev(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEFAULT)
        with patch.dict(os.environ, {"UL_ENVIRONMENT": "production"}):
            self.assertFalse(self.site.is_development_environment())

    @given(st.sampled_from([EnvironmentTypes.PRODUCTION, EnvironmentTypes.STAGING, EnvironmentTypes.TESTING]))
    @_hyp
    def test_non_dev_environment_types_return_false(self, env_type: str) -> None:
        """Hypothesis: none of the non-dev env types should pass is_development_environment."""
        with patch.object(self.site, "get_effective_environment_type", return_value=env_type):
            self.assertFalse(self.site.is_development_environment())

    @given(st.sampled_from([EnvironmentTypes.DEVELOPMENT, EnvironmentTypes.LOCAL]))
    @_hyp
    def test_dev_environment_types_return_true(self, env_type: str) -> None:
        """Hypothesis: both DEVELOPMENT and LOCAL are treated as dev environments."""
        with patch.object(self.site, "get_effective_environment_type", return_value=env_type):
            self.assertTrue(self.site.is_development_environment())


class ShowDevAdminFeaturesTests(TestCase):
    """show_dev_admin_features() gates the dev toolbar for admins and, opt-in, non-admins."""

    def setUp(self) -> None:
        self.site = SiteSettings.get_current()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.admin: User = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.non_admin: User = baker.make(User)

    def _set_override(self, override: str) -> None:
        SiteSettings.objects.filter(pk=self.site.pk).update(environment_override=override)
        self.site.refresh_from_db()

    def test_anonymous_user_never_sees_it(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEVELOPMENT)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True):
            self.assertFalse(self.site.show_dev_admin_features(AnonymousUser()))

    def test_admin_sees_it_in_development(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEVELOPMENT)
        self.assertTrue(self.site.show_dev_admin_features(self.admin))

    def test_admin_does_not_see_it_in_production(self) -> None:
        self._set_override(EnvironmentOverrideChoice.PRODUCTION)
        self.assertFalse(self.site.show_dev_admin_features(self.admin))

    def test_non_admin_does_not_see_it_by_default(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEVELOPMENT)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=False):
            self.assertFalse(self.site.show_dev_admin_features(self.non_admin))

    def test_non_admin_sees_it_when_enabled_in_development(self) -> None:
        self._set_override(EnvironmentOverrideChoice.DEVELOPMENT)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True):
            self.assertTrue(self.site.show_dev_admin_features(self.non_admin))

    def test_non_admin_sees_it_when_enabled_in_testing(self) -> None:
        self._set_override(EnvironmentOverrideChoice.TESTING)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True):
            self.assertTrue(self.site.show_dev_admin_features(self.non_admin))

    def test_non_admin_does_not_see_it_when_enabled_in_staging(self) -> None:
        self._set_override(EnvironmentOverrideChoice.STAGING)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True):
            self.assertFalse(self.site.show_dev_admin_features(self.non_admin))

    def test_non_admin_does_not_see_it_when_enabled_in_production(self) -> None:
        self._set_override(EnvironmentOverrideChoice.PRODUCTION)
        with patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True):
            self.assertFalse(self.site.show_dev_admin_features(self.non_admin))

    @given(st.sampled_from([EnvironmentTypes.DEVELOPMENT, EnvironmentTypes.LOCAL, EnvironmentTypes.TESTING]))
    @_hyp
    def test_non_admin_allowed_environments(self, env_type: str) -> None:
        """Hypothesis: with the flag on, dev/local/testing all grant non-admins the toolbar."""
        with (
            patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True),
            patch.object(self.site, "get_effective_environment_type", return_value=env_type),
        ):
            self.assertTrue(self.site.show_dev_admin_features(self.non_admin))

    @given(st.sampled_from([EnvironmentTypes.PRODUCTION, EnvironmentTypes.STAGING]))
    @_hyp
    def test_non_admin_disallowed_environments(self, env_type: str) -> None:
        """Hypothesis: even with the flag on, staging/production never grant non-admins the toolbar."""
        with (
            patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=True),
            patch.object(self.site, "get_effective_environment_type", return_value=env_type),
        ):
            self.assertFalse(self.site.show_dev_admin_features(self.non_admin))
