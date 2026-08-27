"""Shared helper for tests whose subject sits behind a REData-configured gate.

Most panel sources are REData-backed now, and each one's ``gate()`` ends in
``redata_configured()`` - which reads ``UL_REDATA_API_URL``/``UL_REDATA_API_KEY``
live from app settings. Neither is set in a test environment, so those gates
refuse and the panel view degrades to a quiet 204: correct behaviour for an
install with no REData, and invisible breakage for a test that assumed the
pre-REData contract where the panel simply rendered.

Patching the two settings values rather than the ``redata_configured`` symbol is
deliberate: a plugin module that imports that function by name holds its own
reference, so patching it at its origin would miss them, and patching it per
module means listing every module a test happens to touch. The function reads
the settings at call time, so setting them covers all of them at once.

Tests that specifically want the *unconfigured* behaviour patch
``redata_configured`` in their own plugin module instead - see
``test_inaturalist_panel.py`` for that shape. Panels built on
``services.pins.redata_panel.RedataInfoPanelSource`` no longer hold a
module-level reference (the gate imports it inside the call), so those patch
``...redata_context_gateway.redata_configured`` directly.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.UrbanLens.settings.app import settings as app_settings

REDATA_TEST_URL = "https://redata.test"
REDATA_TEST_KEY = "test-key"  # nosec B105 - a fixture value, not a credential


class RedataConfiguredMixin:
    """Makes ``redata_configured()`` report True for the duration of each test."""

    def setUp(self) -> None:
        super().setUp()
        for attribute, value in (("redata_api_url", REDATA_TEST_URL), ("redata_api_key", REDATA_TEST_KEY)):
            patcher = mock.patch.object(app_settings, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
