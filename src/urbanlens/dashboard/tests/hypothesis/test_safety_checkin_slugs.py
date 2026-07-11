"""Tests for SafetyCheckin slugs, the create-view's validation, and mark-safe's chat message."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.http import Http404
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.safety import SafetyCheckinCreateView, _get_checkin_by_slug
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage
from urbanlens.dashboard.services.safety import cancel_checkin, create_checkin, mark_found_safe


def _future(hours: float = 2) -> datetime.datetime:
    return timezone.now() + datetime.timedelta(hours=hours)


class CreateCheckinSlugTests(TestCase):
    """create_checkin() generates a unique, profile-scoped slug."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def test_slug_is_generated_from_title(self) -> None:
        checkin = create_checkin(profile=self.profile, title="Weekend Hike", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))
        self.assertEqual(checkin.slug, "weekend-hike")

    def test_duplicate_titles_get_distinct_slugs(self) -> None:
        first = create_checkin(profile=self.profile, title="Solo Hike", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))
        # A profile may only have one active check-in at a time - resolve the
        # first before creating the second so this only exercises slug uniqueness.
        cancel_checkin(first)
        second = create_checkin(profile=self.profile, title="Solo Hike", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))
        self.assertNotEqual(first.slug, second.slug)


class GetCheckinBySlugTests(TestCase):
    """_get_checkin_by_slug: slug lookup, UUID fallback, and owner scoping."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.checkin = create_checkin(profile=self.profile, title="Eagle Ridge", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))

    def test_finds_by_slug(self) -> None:
        found = _get_checkin_by_slug(self.profile, self.checkin.slug)
        self.assertEqual(found.pk, self.checkin.pk)

    def test_falls_back_to_uuid_when_not_a_real_slug(self) -> None:
        found = _get_checkin_by_slug(self.profile, str(self.checkin.uuid))
        self.assertEqual(found.pk, self.checkin.pk)

    def test_404s_for_a_different_profiles_checkin(self) -> None:
        other_profile = baker.make(User).profile
        with pytest.raises(Http404):
            _get_checkin_by_slug(other_profile, self.checkin.slug)


class SafetyCheckinCreateViewValidationTests(TestCase):
    """SafetyCheckinCreateView.post: optional title default and future-date validation."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user

    def _post(self, data: dict):
        req = self.factory.post("/safety/new/", data=data)
        req.user = self.user
        return SafetyCheckinCreateView.as_view()(req)

    def test_past_checkin_by_is_rejected(self) -> None:
        past = (timezone.now() - datetime.timedelta(hours=1)).isoformat()
        response = self._post({"checkin_by": past, "grace_period_hours": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 0)

    def test_blank_title_gets_a_default(self) -> None:
        response = self._post({"checkin_by": _future().isoformat(), "grace_period_hours": "1"})
        self.assertEqual(response.status_code, 302)
        checkin = SafetyCheckin.objects.get(profile=self.profile)
        self.assertTrue(checkin.title)

    def test_rejects_a_second_active_checkin(self) -> None:
        create_checkin(profile=self.profile, title="Existing Trip", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))

        response = self._post({"checkin_by": _future().isoformat(), "grace_period_hours": "1"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SafetyCheckin.objects.filter(profile=self.profile).count(), 1)

    def test_get_redirects_to_the_active_checkin(self) -> None:
        # Uses the Django test client (not RequestFactory) - the view calls
        # django.contrib.messages, which needs full middleware to back it.
        active = create_checkin(profile=self.profile, title="Existing Trip", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))
        client = Client()
        client.force_login(self.user)

        response = client.get(reverse("safety.checkin.create"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(active.slug, response.url)


class MarkFoundSafeChatMessageTests(TestCase):
    """mark_found_safe() posts a chat message recording the action."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.checkin = create_checkin(profile=self.profile, title="Night Hike", checkin_by=_future(), grace_period=datetime.timedelta(hours=1))

    def test_creates_a_message_attributed_to_the_contact(self) -> None:
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=self.checkin, contact_profile=None, email="jane@example.com", name="Jane")

        mark_found_safe(contact)

        messages = list(SafetyCheckinMessage.objects.filter(checkin=self.checkin))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender_contact_id, contact.pk)
        self.assertIn(self.profile.username, messages[0].body)
