"""Tests for LabelPinSuggestionsView - lazily-loaded REData suggestions in the pin label dialog."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile


class LabelPinSuggestionsViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=40.5, longitude=-74.5).pin

    def _url(self) -> str:
        return reverse("label.pin_suggestions", kwargs={"pin_slug": self.pin.slug})

    def test_login_required(self) -> None:
        self.client.logout()
        response = self.client.get(self._url())
        self.assertNotEqual(response.status_code, 200)

    def test_another_profiles_pin_is_not_found(self) -> None:
        other_user = baker.make(User)
        self.client.force_login(other_user)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 404)

    def test_renders_empty_when_no_suggestions(self) -> None:
        with mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.get_suggestions", return_value=None):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Suggested for this place")

    def test_renders_suggested_labels_with_confidence(self) -> None:
        label = Label.objects.create(profile=self.profile, name="Lighthouse", kind=KIND_TAG)
        with mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.get_suggestions", return_value=[(label, 0.82)]):
            response = self.client.get(self._url())
        self.assertContains(response, "Suggested for this place")
        self.assertContains(response, "Lighthouse")
        self.assertContains(response, "82%")

    def test_already_applied_labels_are_excluded_even_if_redata_suggests_them(self) -> None:
        label = Label.objects.create(profile=self.profile, name="Lighthouse", kind=KIND_TAG)
        self.pin.labels.add(label)
        with mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.get_suggestions", return_value=[(label, 0.82)]):
            response = self.client.get(self._url())
        self.assertNotContains(response, "Suggested for this place")
