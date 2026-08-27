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

from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.site_admin_models import scrub_personal_keys

_LABELS = "urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway.get_model"
_PHOTOS = "urbanlens.dashboard.services.apis.photos.redata_photos_gateway.RedataPhotosGateway.get_model"
_CONFIGURED = "urbanlens.dashboard.services.labels.redata_suggestions.redata_labels_configured"

#: The envelope REData returns before any model has been promoted.
_HEURISTIC = {"active": None, "ranker": "heuristic"}


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
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=_HEURISTIC), mock.patch(_PHOTOS, return_value=_HEURISTIC):
            response = self.client.get(self.url)

        summary = response.context["models"][0]["summary"]
        self.assertTrue(summary["available"])
        self.assertFalse(summary["has_model"])
        self.assertContains(response, "heuristic is answering")

    def test_a_promoted_model_shows_its_version_and_metrics(self) -> None:
        """Metrics live on the serialized model version, not at the envelope's top level."""
        body = {
            "active": {"version": 12, "algorithm": "logreg", "metrics": {"brier": 0.081}, "baseline_metrics": {"heuristic": {"brier": 0.14}}},
            "ranker": "model",
        }

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=body), mock.patch(_PHOTOS, return_value=_HEURISTIC):
            response = self.client.get(self.url)

        summary = response.context["models"][0]["summary"]
        self.assertTrue(summary["has_model"])
        self.assertEqual(summary["version"], 12)
        self.assertEqual(summary["metrics"]["brier"], 0.081)
        self.assertEqual(summary["baseline_metrics"]["heuristic"]["brier"], 0.14)
        self.assertContains(response, "Model version 12")

    def test_redatas_own_ranker_field_is_believed_over_inference(self) -> None:
        """REData states which ranker answered; inferring it would disagree the moment they differ."""
        body = {"active": {"version": 3}, "ranker": "heuristic"}

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=body), mock.patch(_PHOTOS, return_value=_HEURISTIC):
            response = self.client.get(self.url)

        self.assertEqual(response.context["models"][0]["summary"]["ranker"], "heuristic")

    def test_the_photo_models_scorer_field_is_read_too(self) -> None:
        """The same fact is named `ranker` for labels and `scorer` for photos."""
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=_HEURISTIC), mock.patch(_PHOTOS, return_value={"active": {"version": 9}, "scorer": "model"}):
            response = self.client.get(self.url)

        self.assertEqual(response.context["models"][1]["summary"]["ranker"], "model")

    def test_a_malformed_active_renders_rather_than_500ing(self) -> None:
        """A diagnostics page that dies on an unexpected shape hides what it was reporting."""
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value={"active": 12}), mock.patch(_PHOTOS, return_value=_HEURISTIC):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["models"][0]["summary"]["has_model"])

    def test_an_unreachable_redata_degrades_instead_of_500ing(self) -> None:
        """A diagnostics page that dies when the thing it diagnoses is down is useless."""
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, side_effect=GatewayRequestError("down")), mock.patch(_PHOTOS, side_effect=GatewayRequestError("down")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["models"][0]["summary"]["available"])

    def test_a_personal_field_never_reaches_the_page(self) -> None:
        """The whole constraint, asserted end to end."""
        body = {"active": {"version": 3, "metrics": {"brier": 0.1}}, "ranker": "model", "uploader": "jess@example.test", "reputation": 0.9}

        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value=body), mock.patch(_PHOTOS, return_value=_HEURISTIC):
            response = self.client.get(self.url)

        self.assertNotContains(response, "jess@example.test")
        self.assertNotIn("uploader", response.context["models"][0]["summary"])

    def test_both_models_are_reported(self) -> None:
        with mock.patch(_CONFIGURED, return_value=True), mock.patch(_LABELS, return_value={"active": {"version": 1}}), mock.patch(_PHOTOS, return_value={"active": {"version": 2}}):
            response = self.client.get(self.url)

        self.assertEqual([entry["title"] for entry in response.context["models"]], ["Label suggestion", "Photo relevance"])


class ReputationIsNotConsumedTests(SimpleTestCase):
    """The per-contributor endpoint must stay unused, not merely unwired today.

    The first version of this guard could not fail: it globbed a directory that
    does not exist, so it reported success with a real consumer present. It now
    proves it is looking at the right tree before drawing any conclusion, and
    matches *executable* references rather than the substring - two docstrings
    document the deliberate non-use, and a guard that trips on its own
    explanation gets deleted rather than heeded.
    """

    def _source_root(self) -> Path:

        root = Path(__file__).resolve().parents[4]
        # Prove the root before trusting a negative result from it.
        self.assertTrue((root / "urbanlens" / "dashboard" / "controllers").is_dir(), f"source root not found at {root}")
        return root / "urbanlens"

    def test_the_search_root_is_real(self) -> None:
        """Guards the guard: an empty scan of a missing tree looks like success."""
        root = self._source_root()

        self.assertGreater(len(list(root.rglob("*.py"))), 100, "the tree being scanned is implausibly small")

    def test_no_code_calls_the_reputation_endpoint(self) -> None:
        import ast

        root = self._source_root()
        offenders = []
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Identity of the *node*, not of its string value: equal short
            # strings can be interned to one object, which would let a real
            # call be mistaken for a docstring.
            docstring_nodes = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                # Only strings *used in code* count - a docstring saying the
                # endpoint is deliberately unused is not a call to it.
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "photos/reputation" in node.value and id(node) not in docstring_nodes:
                    offenders.append(f"{path.name}:{node.lineno}")

        self.assertEqual(offenders, [], "a per-contributor reputation score is not something this application has a use for")

    def test_the_guard_catches_a_real_consumer(self) -> None:
        """Proves the matcher fires, since a guard is only worth its false-negative rate."""
        import ast

        source = 'def get_reputation(self, user_id):\n    return self._get_json("/api/v1/photos/reputation/")\n'
        tree = ast.parse(source)
        docstring_nodes = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and "photos/reputation" in n.value and id(n) not in docstring_nodes]

        self.assertEqual(len(hits), 1)

    def test_a_docstring_mention_is_not_a_consumer(self) -> None:
        """Both gateways document the deliberate non-use; that must stay legal."""
        import ast

        source = '"""Deliberately does not wrap GET /api/v1/photos/reputation/."""\n'
        tree = ast.parse(source)
        docstring_nodes = {id(tree.body[0].value)}
        hits = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and "photos/reputation" in n.value and id(n) not in docstring_nodes]

        self.assertEqual(hits, [])
