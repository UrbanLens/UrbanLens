"""Tests for services.ai.access.assistant_available - the assistant's single access chokepoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from model_bakery import baker
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.access import assistant_available

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
