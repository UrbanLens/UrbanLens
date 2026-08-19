"""The site-admin view onto REData's two suggestion models.

Two questions this deployment could not answer about itself: whether a trained
model or the hand-weighted heuristic is answering, and how well the thing that
is answering scored against the alternatives it was promoted over. Both matter
now that auto-tagging applies suggestions above a fixed confidence floor
without distinguishing which ranker produced the number.

The constraint that shapes the tests: **nothing here may be about a person.**
REData's per-contributor reputation endpoint is not consumed anywhere, and the
view scrubs personal keys out of the model payload before rendering rather than
trusting the upstream shape to stay aggregate. That guard exists precisely
because "this response is aggregate" is a property of today's contract, not
something this codebase controls.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.site_admin_models import scrub_personal_keys

_LABELS = "urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway.get_model"
_PHOTOS = "urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway.get_model"
_CONFIGURED = "urbanlens.dashboard.services.labels.redata_suggestions.redata_labels_configured"


class ScrubPersonalKeysTests(SimpleTestCase):
    """The boundary guard, tested on shapes the endpoint does not currently return.

    That is the point: it is here for the version of REData that grows a
    per-person field, not the one documented today.
    """

    def test_a_contributor_key_is_dropped(self) -> None:
        scrubbed = scrub_personal_keys({"active": 7, "reputation": 0.83})

        self.assertEqual(scrubbed, {"active": 7})

    def test_nested_personal_keys_are_dropped(self) -> None:
        payload = {"metrics": {"brier": 0.1, "uploader": "someone"}, "features": [{"name": "f", "user_id": "abc"}]}

        scrubbed = scrub_personal_keys(payload)

        self.assertEqual(scrubbed["metrics"], {"brier": 0.1})
        self.assertEqual(scrubbed["features"], [{"name": "f"}])

    def test_matching_is_case_insensitive(self) -> None:
        self.assertEqual(scrub_personal_keys({"User_ID": "abc", "active": 1}), {"active": 1})

    def test_ordinary_metadata_survives_untouched(self) -> None:
        payload = {"active": 12, "metrics": {"brier": 0.08, "auc": 0.91}, "features": [{"name": "co_occurrence"}]}

        self.assertEqual(scrub_personal_keys(payload), payload)


class SiteAdminModelsViewTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.admin = baker.make(User)
        self.admin.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.client.force_login(self.admin)
        self.url = reverse("site_admin_models")

    def test_a_non_admin_cannot_reach_it(self) -> None:
        self.client.force_login(baker.make(User))

        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_unconfigured_deployment_says_so_rather_than_erroring(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["configured"])

    def test_no_promoted_model_is_reported_as_the_heuristic_answering(self) -> None:
        """`active: null` is a normal state, not a failure."""
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value={"active": None}), mock.patch(_PHOTOS, return_value={"active": None}):
            response = self.client.get(self.url)

        summary = response.context["models"][0]["summary"]
        self.assertTrue(summary["available"])
        self.assertFalse(summary["has_model"])
        self.assertContains(response, "heuristic is answering")

    def test_a_promoted_model_shows_its_version_and_metrics(self) -> None:
        body = {"active": 12, "metrics": {"brier": 0.081}, "baseline_metrics": {"heuristic": 0.14}}

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=body), mock.patch(_PHOTOS, return_value={"active": None}):
            response = self.client.get(self.url)

        summary = response.context["models"][0]["summary"]
        self.assertTrue(summary["has_model"])
        self.assertEqual(summary["active"], 12)
        self.assertEqual(summary["metrics"]["brier"], 0.081)
        self.assertEqual(summary["baseline_metrics"]["heuristic"], 0.14)

    def test_an_unreachable_redata_degrades_instead_of_500ing(self) -> None:
        """A diagnostics page that dies when the thing it diagnoses is down is useless."""
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, side_effect=GatewayRequestError("down")), mock.patch(_PHOTOS, side_effect=GatewayRequestError("down")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["models"][0]["summary"]["available"])

    def test_a_personal_field_never_reaches_the_page(self) -> None:
        """The whole constraint, asserted end to end."""
        body = {"active": 3, "metrics": {"brier": 0.1}, "uploader": "jess@example.test", "reputation": 0.9}

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=body), mock.patch(_PHOTOS, return_value={"active": None}):
            response = self.client.get(self.url)

        self.assertNotContains(response, "jess@example.test")
        self.assertNotIn("uploader", response.context["models"][0]["summary"])

    def test_both_models_are_reported(self) -> None:
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value={"active": 1}), mock.patch(_PHOTOS, return_value={"active": 2}):
            response = self.client.get(self.url)

        self.assertEqual([entry["title"] for entry in response.context["models"]], ["Label suggestion", "Photo relevance"])


class ReputationIsNotConsumedTests(SimpleTestCase):
    """The per-contributor endpoint must stay unused, not merely unwired today."""

    def test_no_gateway_wraps_the_reputation_endpoint(self) -> None:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3]
        hits = [path for path in (root / "urbanlens").rglob("*.py") if "photos/reputation" in path.read_text(encoding="utf-8") and "tests/" not in str(path)]

        self.assertEqual(hits, [], "a per-contributor reputation score is not something this application has a use for")
