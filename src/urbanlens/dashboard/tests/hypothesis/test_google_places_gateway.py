"""Tests for GooglePlacesGateway's field-mask/fields handling.

Places API (New) bills fields by SKU tier - rating/userRatingCount are
"Enterprise + Atmosphere" tier, billed extra whether or not a caller actually
uses them (see docs/prompts/completed.md's "Stop retrieving Google Places
atmosphere data" entry for the original billing report this addresses).
These tests guard the fix: find_nearest_place_id must request a minimal
field_mask, and get_place_details must never be callable without explicit
fields (which would make the legacy endpoint return - and bill for -
everything).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from urbanlens.dashboard.services.apis.locations.google.places import GooglePlacesGateway


def _gateway(session: Mock) -> GooglePlacesGateway:
    return GooglePlacesGateway(api_key="test-key", session=session)


def test_search_nearby_defaults_to_the_full_field_mask_when_not_given() -> None:
    response = Mock()
    response.json.return_value = {"places": []}
    session = Mock()
    session.post.return_value = response
    gw = _gateway(session)

    gw.search_nearby(1.0, 2.0)

    headers = session.post.call_args.kwargs["headers"]
    assert "places.rating" in headers["X-Goog-FieldMask"]  # nosec B101
    assert "places.userRatingCount" in headers["X-Goog-FieldMask"]  # nosec B101


def test_search_nearby_uses_the_given_field_mask_instead() -> None:
    response = Mock()
    response.json.return_value = {"places": []}
    session = Mock()
    session.post.return_value = response
    gw = _gateway(session)

    gw.search_nearby(1.0, 2.0, field_mask="places.id")

    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-Goog-FieldMask"] == "places.id"  # nosec B101


def test_find_nearest_place_id_requests_only_the_place_id_field() -> None:
    """The only field this lookup ever reads is `id` - it must never pay for
    the default mask's Atmosphere-tier rating/userRatingCount."""
    response = Mock()
    response.json.return_value = {"places": [{"id": "place123"}]}
    session = Mock()
    session.post.return_value = response
    gw = _gateway(session)

    result = gw.find_nearest_place_id(1.0, 2.0)

    assert result == "place123"  # nosec B101
    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-Goog-FieldMask"] == "places.id"  # nosec B101


def test_find_nearest_place_id_returns_none_when_nothing_found() -> None:
    response = Mock()
    response.json.return_value = {"places": []}
    session = Mock()
    session.post.return_value = response
    gw = _gateway(session)

    assert gw.find_nearest_place_id(1.0, 2.0) is None  # nosec B101


def test_get_place_details_fields_is_a_required_argument() -> None:
    """No default - a caller can no longer accidentally omit fields and have
    the legacy endpoint return (and bill for) every field."""
    gw = _gateway(Mock())

    with pytest.raises(TypeError):
        gw.get_place_details("place123")  # type: ignore[call-arg]


def test_get_place_details_sends_requested_fields_as_a_comma_joined_param() -> None:
    response = Mock()
    response.json.return_value = {"result": {}}
    session = Mock()
    session.get.return_value = response
    gw = _gateway(session)

    gw.get_place_details("place123", fields=["name", "rating"])

    params = session.get.call_args.kwargs["params"]
    assert params["fields"] == "name,rating"  # nosec B101
