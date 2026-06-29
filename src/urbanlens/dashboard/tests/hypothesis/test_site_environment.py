"""Tests for SiteSettings environment override resolution."""
from __future__ import annotations

import os
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings import EnvironmentOverrideChoice, SiteSettings
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes

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
