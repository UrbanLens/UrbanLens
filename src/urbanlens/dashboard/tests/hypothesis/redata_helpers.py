"""Shared helpers for tests whose subject sits behind a REData-configured gate."""

from __future__ import annotations

from unittest import mock

from urbanlens.UrbanLens.settings.app import settings as app_settings

REDATA_TEST_URL = "https://redata.test"
REDATA_TEST_KEY = "test-key"  # nosec B105 - a fixture value, not a credential

#: What REData answers for a call it did not make because a budget said no: its own (``rate_limited``), or the share
#: of it the requesting key's environment may spend (``key_budget_exhausted``). UrbanLens treats the two alike - the
#: question went unanswered, nothing is cached as an answer, and the source is backed off - so a test of one
#: answer loops over both, and the two cannot drift apart.
BUDGET_REFUSALS = ("rate_limited", "key_budget_exhausted")


class RedataConfiguredMixin:
    """Makes ``redata_configured()`` report True for the duration of each test."""

    def setUp(self) -> None:
        super().setUp()
        for attribute, value in (("redata_api_url", REDATA_TEST_URL), ("redata_api_key", REDATA_TEST_KEY)):
            patcher = mock.patch.object(app_settings, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class EveryPanelGateConfiguredMixin(RedataConfiguredMixin):
    """Also sets the Azure Maps key, so no Private Pin panel's gate refuses a pin for want of configuration.

    The page leaves out a panel whose gate refuses the pin (P53); a test about every panel's markup needs them all.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(app_settings, "azure_maps_subscription_key", "test-azure-key")
        patcher.start()
        self.addCleanup(patcher.stop)
