"""Shared helpers for tests whose subject sits behind a REData-configured gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.UrbanLens.settings.app import settings as app_settings

REDATA_TEST_URL = "https://redata.test"
REDATA_TEST_KEY = "test-key"  # nosec B105 - a fixture value, not a credential

#: What REData answers for a call it did not make because a budget said no: its own (``rate_limited``), or the share
#: of it the requesting key's environment may spend (``key_budget_exhausted``). UrbanLens treats the two alike - the
#: question went unanswered, nothing is cached as an answer, and the source is backed off - so a test of one
#: answer loops over both, and the two cannot drift apart.
BUDGET_REFUSALS = ("rate_limited", "key_budget_exhausted")

#: The two ways REData answers a CRIS ``fetch-detail`` call it will not give a record for, for now: its source is down
#: (a 503), or its source cannot resolve the resource and REData holds it (a 200 whose ``detail_status`` is
#: ``unresolved``). A caller degrades the same for both, so a test of one loops over both.
DETAIL_WITHHELD_ANSWERS = ("503", "unresolved")


def detail_unresolved_body(*, retry_after: object = None, resource_uuid: str = "r-held") -> dict:
    """REData's body for a held resource: the envelope, its message and the hold's end, and the resource without its detail."""
    wait = (datetime.now(UTC) + timedelta(minutes=10)).isoformat() if retry_after is None else retry_after
    return {
        "detail_status": "unresolved",
        "message": "The source could not resolve this resource to a record, so nothing more was fetched.",
        "retry_after": wait,
        "resource": {"uuid": resource_uuid, "attributes": {"USNName": "Old Mill"}, "detail_retrieved_at": None},
    }


def detail_withheld_error(answer: str) -> PropertyRecordsUnavailableError:
    """The error :class:`RedataGateway` raises when ``fetch-detail`` gives ``answer`` (one of :data:`DETAIL_WITHHELD_ANSWERS`).

    Built by the gateway itself, from a mocked response, so a caller is tested against the real error and not a guess at it.
    """
    response = mock.Mock(
        status_code=503 if answer == "503" else 200, headers={"Retry-After": "30"} if answer == "503" else {}
    )
    response.text = "REData is unavailable." if answer == "503" else ""
    response.json.return_value = detail_unresolved_body() if answer == "unresolved" else {"error": "source_error"}
    session = mock.MagicMock()
    session.post.return_value = response
    gateway = RedataGateway(base_url=REDATA_TEST_URL, api_key=REDATA_TEST_KEY, session=session)
    try:
        gateway._fetch_cultural_resource_detail_now("r-held")
    except PropertyRecordsUnavailableError as error:
        return error
    raise AssertionError(f"fetch-detail answering {answer!r} raised nothing")


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
