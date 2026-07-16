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

    def test_visit_list_carries_adaptive_pagination_markup(self):
        """Regression coverage: pagination used to be a fixed 6-per-page count
        regardless of each visit's actual rendered height (photos/notes/maps
        make some visits much taller than others) - it now hands the client
        the same height-based adaptive-pagination system Web Search uses, via
        data-adaptive-pagination-list/-item and adaptive_pagination context."""
        baker.make(PinVisit, pin=self.pin, notes="First visit")

        response = self.client.get(reverse("pin.visits", args=[self.pin.slug]))

        self.assertContains(response, "data-adaptive-pagination-list")
        self.assertContains(response, "data-adaptive-pagination-item")
        self.assertContains(response, "data-adaptive-pagination-controls")

    def test_visit_list_batch_size_exceeds_typical_client_page_size(self):
        """The server-side batch must be comfortably larger than a typical
        visible page, or the client-side height measurer has nothing to
        actually paginate against - see _VISITS_BATCH_MULTIPLIER."""
        from urbanlens.dashboard.controllers.visits import _VISITS_CLIENT_PAGE_SIZE, _VISITS_PAGE_SIZE

        self.assertGreater(_VISITS_PAGE_SIZE, _VISITS_CLIENT_PAGE_SIZE)
