from __future__ import annotations

from unittest import mock

from django.test import override_settings
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
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
