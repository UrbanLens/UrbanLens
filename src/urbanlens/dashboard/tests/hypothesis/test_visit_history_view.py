"""Regression tests for the pin-detail visit history panel (VisitHistoryView/VisitEditView).

Both views render `_visit_form.html`, whose date input uses
`{{ visit.visited_at|date:'Y-m-d'|default:default_date }}`. Django resolves filter
arguments unconditionally and does not fall back to an empty string for missing
variables the way it does for the primary value, so a context missing
`default_date` raises `VariableDoesNotExist` and 500s - regardless of whether the
`default` filter would actually use it.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visits.model import PinVisit


class VisitHistoryViewTests(TestCase):
    """GET /map/pin/<slug>/visits/ and its edit-form partial must render without error."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="40.0", longitude="-74.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_visit_history_panel_renders_with_add_dialog(self):
        response = self.client.get(reverse("pin.visits", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)

    def test_visit_edit_form_renders_for_existing_visit(self):
        visit = baker.make(PinVisit, pin=self.pin)

        response = self.client.get(reverse("pin.visit.edit", args=[self.pin.slug, visit.id]))

        self.assertEqual(response.status_code, 200)
