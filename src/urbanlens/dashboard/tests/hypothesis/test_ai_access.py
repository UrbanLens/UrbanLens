"""Tests for services.ai.access - the AI access chokepoints, and the one conjunct that separates them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.test import override_settings
from model_bakery import baker
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.access import ai_features_enabled, assistant_available

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _grant_ai_to_everyone() -> None:
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)


def _plain_profile(*, ai_enabled: bool = True, external_apis_enabled: bool = True) -> Profile:
    """A profile whose user has no subscription and no feature grants.

    The first user created in a fresh test database is auto-promoted to
    bootstrap site admin, and ``user_has_feature`` grants a site admin every
    feature - so a throwaway user absorbs that promotion first. Same
    precedent as ``test_property_owner_access.py``.
    """
    baker.make("auth.User")
    return _make_profile(ai_enabled=ai_enabled, external_apis_enabled=external_apis_enabled)


@pytest.mark.django_db
def test_available_when_everything_is_on() -> None:
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)

    assert assistant_available(profile) is True


@pytest.mark.django_db
def test_unavailable_without_ai_feature_grant() -> None:
    """No subscription, no default_features grant, not a site admin: the common case."""
    profile = _plain_profile()

    assert assistant_available(profile) is False


@pytest.mark.django_db
def test_unavailable_when_profile_ai_disabled() -> None:
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=False, external_apis_enabled=True)

    assert assistant_available(profile) is False


@pytest.mark.django_db
def test_unavailable_when_profile_external_apis_disabled() -> None:
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=False)

    assert assistant_available(profile) is False


@pytest.mark.django_db
def test_unavailable_when_site_ai_disabled() -> None:
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(ai_enabled=False)

    assert assistant_available(profile) is False


@pytest.mark.django_db
@override_settings(UL_AI_WORKER_ENABLED=False)
def test_unavailable_without_an_ai_worker_deployed() -> None:
    """Every other condition satisfied, but nothing drains Queue.AI.

    Must fail closed rather than degrade the tool loop onto the regular
    worker, which holds REData/OAuth credentials the loop must never run
    alongside - see services.sandbox.queues.ai_queue.
    """
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)

    assert assistant_available(profile) is False


@pytest.mark.django_db
@override_settings(UL_AI_WORKER_ENABLED=False)
def test_ai_worker_absence_does_not_disable_other_ai_features() -> None:
    """The whole reason the two predicates are separate.

    ``UL_AI_WORKER_ENABLED`` describes the sandboxed ``ai-worker`` container,
    which only the interactive assistant runs in. Label styling, auto-tagging
    and import assist resolve an inference client through the shared
    ``ai-inference`` tier instead, so a resource-constrained self-host that
    turns the chat assistant off must keep them.
    """
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)

    assert assistant_available(profile) is False
    assert ai_features_enabled(profile) is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("ai_enabled", "external_apis_enabled"),
    [(False, True), (True, False), (False, False)],
)
def test_features_disabled_by_either_profile_preference(ai_enabled: bool, external_apis_enabled: bool) -> None:
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=ai_enabled, external_apis_enabled=external_apis_enabled)

    assert ai_features_enabled(profile) is False


@pytest.mark.django_db
def test_features_disabled_without_the_subscription_entitlement() -> None:
    assert ai_features_enabled(_plain_profile()) is False


@pytest.mark.django_db
def test_features_disabled_when_site_ai_is_off() -> None:
    # style_suggestions used to reach get_gateway before learning this, which
    # is the check the shared predicate pulls forward.
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)
    SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(ai_enabled=False)

    assert ai_features_enabled(profile) is False


@pytest.mark.django_db
def test_assistant_is_a_strict_subset_of_features() -> None:
    """Anything the assistant allows, the general predicate allows too."""
    _grant_ai_to_everyone()
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)

    assert assistant_available(profile) is True
    assert ai_features_enabled(profile) is True
