from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.test import override_settings
from model_bakery import baker
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.labels.style_suggestions import suggest_label_style

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

@pytest.mark.django_db
def test_suggest_label_style_requires_ai_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)

    monkeypatch.setattr(
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
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
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
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
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
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
def test_suggest_label_style_respects_the_site_wide_ai_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conjunct the module's own check was missing.

    ``get_gateway`` would have refused anyway; sharing
    ``services.ai.access.ai_features_enabled`` makes it an early-out instead of
    a provider gateway built and discarded.
    """
    profile = _make_profile(ai_enabled=True)
    monkeypatch.setattr(
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_enabled=False)

    with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway") as get_gateway:
        suggestion = suggest_label_style("Factories", profile)

    assert suggestion.icon is None
    get_gateway.assert_not_called()


@pytest.mark.django_db
@override_settings(UL_AI_WORKER_ENABLED=False)
def test_suggest_label_style_does_not_need_the_assistant_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Label styling must survive turning the interactive assistant off.

    It never touches the ``ai-worker`` container - ``get_gateway`` resolves an
    inference client through the shared ``ai-inference`` tier - so folding this
    onto ``assistant_available`` would have silently broken it for any install
    that set ``UL_AI_WORKER_ENABLED=false`` to save resources.
    """
    profile = _make_profile(ai_enabled=True)
    monkeypatch.setattr(
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    gateway = mock.Mock()
    gateway.send_prompt_list.return_value = ["🏭", "#F44336"]
    monkeypatch.setattr("urbanlens.dashboard.services.ai.factory.get_gateway", lambda *_args, **_kwargs: gateway)

    assert suggest_label_style("Factories", profile).icon == "🏭"


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
        "urbanlens.dashboard.services.import_export.archive_extractor.validate_content_type",
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
