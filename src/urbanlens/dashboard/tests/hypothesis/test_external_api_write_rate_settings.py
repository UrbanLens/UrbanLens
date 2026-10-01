"""A test deployment can raise the per-key caps without touching production's defaults.

The integration suite drives most of its requests through one key per account, and a full run spends
more than 300 writes and 1000 reads an hour on it - every spec after that point dies on 429 instead of
testing anything.
"""

from __future__ import annotations

from django.conf import settings
from pydantic import ValidationError

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings.app import AppSettings, settings as app_settings


def _fresh(**values: str) -> AppSettings:
    """A real construction: AppSettings is a per-class singleton, so each probe needs its own subclass."""
    probe = type("AppSettingsProbe", (AppSettings,), {})
    return probe(_env_file=None, **values)


class ExternalApiWriteRateSettingsTests(SimpleTestCase):
    def test_the_defaults_are_the_published_caps(self) -> None:
        self.assertEqual(AppSettings.model_fields["external_api_write_rate"].default, "300/hour")
        self.assertEqual(AppSettings.model_fields["external_api_burst_rate"].default, "60/minute")
        self.assertEqual(AppSettings.model_fields["external_api_read_rate"].default, "1000/hour")

    def test_drf_reads_the_configured_rates(self) -> None:
        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]

        self.assertEqual(rates["external_api_write"], app_settings.external_api_write_rate)
        self.assertEqual(rates["external_api_burst"], app_settings.external_api_burst_rate)
        self.assertEqual(rates["external_api_read"], app_settings.external_api_read_rate)

    def test_a_raised_rate_is_accepted(self) -> None:
        self.assertEqual(_fresh(external_api_write_rate="5000/hour").external_api_write_rate, "5000/hour")

    def test_a_malformed_rate_is_refused_at_startup_not_on_the_first_write(self) -> None:
        for bad in ("lots", "300", "300/fortnight", "-1/hour", "/hour"):
            for field in ("external_api_write_rate", "external_api_read_rate"):
                with self.subTest(bad=bad, field=field), self.assertRaises(ValidationError):
                    _fresh(**{field: bad})
