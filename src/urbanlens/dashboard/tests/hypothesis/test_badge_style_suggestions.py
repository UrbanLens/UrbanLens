from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from model_bakery import baker
import pytest

from urbanlens.dashboard.models.badges.meta import KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.badges.style_suggestions import suggest_badge_style
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _make_profile(**attrs) -> Profile:
    profile = baker.make("auth.User").profile
    for field, value in attrs.items():
        setattr(profile, field, value)
    if attrs:
        profile.save(update_fields=[*attrs, "updated"])
    return profile


@pytest.mark.django_db
def test_suggest_badge_style_requires_ai_subscription() -> None:
    profile = _make_profile(ai_enabled=True)

    with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
        suggestion = suggest_badge_style("Factories", profile)

    assert suggestion.icon is None
    assert suggestion.color is None
    get_gateway.assert_not_called()


@pytest.mark.django_db
def test_suggest_badge_style_validates_ai_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)

    monkeypatch.setattr(
        "urbanlens.dashboard.services.badges.style_suggestions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    gateway = mock.Mock()
    gateway.send_prompt_list.return_value = ["🏭", "#F44336"]
    monkeypatch.setattr(
        "urbanlens.dashboard.services.ai.factory.get_gateway",
        lambda *_args, **_kwargs: gateway,
    )

    suggestion = suggest_badge_style("Factories", profile)

    assert suggestion.icon == "🏭"
    assert suggestion.color == "#F44336"


@pytest.mark.django_db
def test_import_filename_badge_uses_ai_style_for_new_badge(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile()
    pin = baker.make("dashboard.Pin", profile=profile)
    gateway = GoogleMapsGateway(api_key="test-key")

    monkeypatch.setattr(
        gateway,
        "_csv_row_iter",
        lambda _file_data, _user_profile: iter(
            [
                {
                    "profile": profile,
                    "name": "Imported Pin",
                    "latitude": 1.0,
                    "longitude": 2.0,
                },
            ],
        ),
    )
    monkeypatch.setattr(
        "urbanlens.dashboard.services.archive_extractor.validate_content_type",
        lambda _filename, _raw_bytes: "csv",
    )
    monkeypatch.setattr(
        "urbanlens.dashboard.services.apis.locations.google.maps.suggest_badge_style",
        lambda _name, _user_profile: mock.Mock(icon="🏭", color="#F44336"),
    )
    monkeypatch.setattr(
        "urbanlens.dashboard.models.pin.Pin.objects.get_nearby_or_create",
        lambda **_kwargs: (pin, True),
    )

    list(gateway.import_pins_streaming([("Factories.csv", b"Title,URL\nA,B")], profile, tag_by_filename=True))

    badge = Badge.objects.get(profile=profile, name="Factories", kind=KIND_TAG)
    assert badge.icon == "🏭"
    assert badge.color == "#F44336"
    assert badge in pin.badges.all()
