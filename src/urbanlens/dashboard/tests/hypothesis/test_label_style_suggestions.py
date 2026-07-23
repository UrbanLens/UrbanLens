from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from model_bakery import baker
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.labels.style_suggestions import suggest_label_style

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

@pytest.mark.django_db
def test_suggest_label_style_requires_ai_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)

    monkeypatch.setattr(
        "urbanlens.dashboard.services.labels.style_suggestions.user_has_feature",
        lambda _user, _feature: False,
    )
    with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
        suggestion = suggest_label_style("Factories", profile)

    assert suggestion.icon is None
    assert suggestion.color is None
    get_gateway.assert_not_called()


@pytest.mark.django_db
def test_suggest_label_style_requires_external_apis_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True, external_apis_enabled=False)

    monkeypatch.setattr(
        "urbanlens.dashboard.services.labels.style_suggestions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
        suggestion = suggest_label_style("Factories", profile)

    assert suggestion.icon is None
    assert suggestion.color is None
    get_gateway.assert_not_called()


@pytest.mark.django_db
def test_suggest_label_style_validates_ai_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)

    monkeypatch.setattr(
        "urbanlens.dashboard.services.labels.style_suggestions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    gateway = mock.Mock()
    gateway.send_prompt_list.return_value = ["🏭", "#F44336"]
    monkeypatch.setattr(
        "urbanlens.dashboard.services.ai.factory.get_gateway",
        lambda *_args, **_kwargs: gateway,
    )

    suggestion = suggest_label_style("Factories", profile)

    assert suggestion.icon == "🏭"
    assert suggestion.color == "#F44336"


@pytest.mark.django_db
def test_import_filename_label_uses_ai_style_for_new_label(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "urbanlens.dashboard.services.apis.locations.google.maps.suggest_label_style",
        lambda _name, _user_profile: mock.Mock(icon="🏭", color="#F44336"),
    )
    monkeypatch.setattr(
        "urbanlens.dashboard.models.pin.Pin.objects.get_nearby_or_create",
        lambda **_kwargs: (pin, True),
    )

    list(gateway.import_pins_streaming([("Factories.csv", b"Title,URL\nA,B")], profile, tag_by_filename=True))

    label = Label.objects.get(profile=profile, name="Factories", kind=KIND_TAG)
    assert label.icon == "🏭"
    assert label.color == "#F44336"
    assert label in pin.labels.all()
