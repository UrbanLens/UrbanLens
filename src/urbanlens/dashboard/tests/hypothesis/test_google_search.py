from __future__ import annotations

from unittest.mock import Mock

import pytest
from requests import HTTPError

from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchError, GoogleCustomSearchGateway


def test_search_masks_forbidden_key_from_exception_and_logs(caplog) -> None:
    response = Mock()
    response.status_code = 403
    response.json.return_value = {"error": {"message": "API key not valid", "errors": [{"reason": "keyInvalid"}]}}
    response.raise_for_status.side_effect = HTTPError("403 Client Error: Forbidden for url: https://x?key=SECRETKEY1234")

    session = Mock()
    session.get.return_value = response
    gateway = GoogleCustomSearchGateway(api_key="SECRETKEY1234", cx="CXVALUE5678", session=session)

    with pytest.raises(GoogleCustomSearchError) as excinfo:
        gateway.search("UrbanLens")

    assert "SECRETKEY1234" not in str(excinfo.value)  # nosec B101
    assert "SECRETKEY1234" not in caplog.text  # nosec B101
    assert "SECR...1234" in caplog.text  # nosec B101


def test_search_rejects_missing_configuration_before_request() -> None:
    session = Mock()
    gateway = GoogleCustomSearchGateway(api_key="", cx="", session=session)

    with pytest.raises(GoogleCustomSearchError, match="UL_GOOGLE_DOMAIN_RESTRICTED_API_KEY"):
        gateway.search("UrbanLens")

    session.get.assert_not_called()


def test_build_query_skips_empty_nested_terms() -> None:
    gateway = GoogleCustomSearchGateway(api_key="key", cx="cx")

    assert gateway.build_query([None, [None, "Cincinnati"], "UrbanLens"]) == '("Cincinnati" OR "UrbanLens")'  # nosec B101
