from __future__ import annotations

from unittest import mock

from django.test import override_settings
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.labels.style_suggestions import suggest_label_style


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

    ``get_gateway`` would have refused anyway; sharing ``services.ai.access.ai_features_enabled`` makes it an
    early-out instead of a provider gateway built and discarded."""
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

    It never touches the ``ai-worker`` container - ``get_gateway`` resolves an inference client through the
    shared ``ai-inference`` tier - so folding this onto ``assistant_available`` would have silently broken it
    for any install that set ``UL_AI_WORKER_ENABLED=false`` to save resources."""
    profile = _make_profile(ai_enabled=True)
    monkeypatch.setattr(
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    gateway = mock.Mock()
    gateway.send_prompt_list.return_value = ["🏭", "#F44336"]
    monkeypatch.setattr("urbanlens.dashboard.services.ai.factory.get_gateway", lambda *_args, **_kwargs: gateway)

    assert suggest_label_style("Factories", profile).icon == "🏭"


def _ai_gateway(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> mock.Mock:
    monkeypatch.setattr(
        "urbanlens.dashboard.models.subscriptions.user_has_feature",
        lambda _user, feature: feature == SiteFeature.AI,
    )
    gateway = mock.Mock()
    gateway.send_prompt_list.return_value = answers
    monkeypatch.setattr("urbanlens.dashboard.services.ai.factory.get_gateway", lambda *_args, **_kwargs: gateway)
    return gateway


def _confirm_list(profile: Profile, stem: str, *, create_category: bool = True) -> None:
    pins = [{"name": "Imported Pin", "lat": 1.0, "lng": 2.0, "description": "", "cid": None}]
    lists = [{"stem": stem, "create_category": create_category, "label_ids": [], "pins": pins}]
    list(GoogleMapsGateway(api_key="test-key").iter_confirmed_import_events(lists, profile, auto_tag=False))


@pytest.mark.django_db
def test_a_category_the_confirmed_import_creates_gets_an_ai_style(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)
    _ai_gateway(monkeypatch, ["🏭", "#F44336"])

    _confirm_list(profile, "Factories")

    label = Label.objects.get(profile=profile, name="Factories", kind=KIND_CATEGORY)
    assert label.icon == "🏭"
    assert label.color == "#F44336"
    assert label.pins.filter(name="Imported Pin").exists()


@pytest.mark.django_db
def test_an_existing_category_is_reused_without_asking_for_a_style(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)
    existing = Label.objects.create(profile=profile, name="Factories", kind=KIND_CATEGORY, color="#2196F3")
    gateway = _ai_gateway(monkeypatch, ["🏭", "#F44336"])

    _confirm_list(profile, "factories")

    gateway.send_prompt_list.assert_not_called()
    existing.refresh_from_db()
    assert existing.color == "#2196F3"
    assert Label.objects.filter(profile=profile, kind=KIND_CATEGORY, name__iexact="Factories").count() == 1
    assert existing.pins.filter(name="Imported Pin").exists()


@pytest.mark.django_db
def test_an_unusable_suggestion_leaves_the_category_unstyled(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)
    _ai_gateway(monkeypatch, ["not an emoji", "#123456"])

    _confirm_list(profile, "Factories")

    label = Label.objects.get(profile=profile, name="Factories", kind=KIND_CATEGORY)
    assert label.icon is None
    assert label.color is None


@pytest.mark.django_db
def test_no_category_asked_for_means_no_style_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _make_profile(ai_enabled=True)
    gateway = _ai_gateway(monkeypatch, ["🏭", "#F44336"])

    _confirm_list(profile, "Factories", create_category=False)

    gateway.send_prompt_list.assert_not_called()
    assert not Label.objects.filter(profile=profile, kind=KIND_CATEGORY, name__iexact="Factories").exists()
