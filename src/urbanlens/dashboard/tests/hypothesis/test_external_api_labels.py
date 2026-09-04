"""Tests for the external API's label surface.

Beyond the usual scope/isolation/pagination checks, two rules here are the
whole point of the endpoint's design and are asserted explicitly:

- a global label can be *read* and *customized* by anyone, but never edited,
  deleted, or consumed by a merge - it is shared by every user on the site;
- the labels list must be built with ``.with_customizations_for(profile)``, or
  the ``effective_*`` fields silently serialize the wrong values rather than
  failing (``Label._get_customization`` falls back to "no customization" when
  the prefetch is absent).
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.customization import LabelCustomization
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_BASE = "/dashboard/api/external/v1/labels/"

#: Distinctive token embedded in every fixture label's name. The database ships
#: with a couple of dozen seeded *global* labels ("Asylum", "Demolished",
#: "Visited", ...), which are legitimately visible to every user and would
#: otherwise both pollute these assertions and fill the first page. Collection
#: tests therefore filter with ``?q=<token>`` and assert against that slice,
#: rather than pretending the label table starts empty.
_TOKEN = "Zqafixture"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _scoped(*params: str) -> str:
    """A labels-collection URL filtered to this module's fixture labels."""
    return f"{_BASE}?q={_TOKEN}" + ("&" + "&".join(params) if params else "")


class LabelApiTestCase(TestCase):
    """Shared fixture: a user with a key granting both label scopes."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Labels client")
        self._grant(ApiKeyScope.LABELS_READ, ApiKeyScope.LABELS_WRITE)

    def _grant(self, *scopes: ApiKeyScope) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[scope.value for scope in scopes])

    def _mine(self, name: str = "Mine", kind: str = KIND_TAG, **kwargs) -> Label:
        return ensure_label(profile=self.profile, name=f"{_TOKEN}{name}", kind=kind, **kwargs)

    def _global(self, name: str = "Shared", kind: str = KIND_CATEGORY, **kwargs) -> Label:
        return ensure_label(profile=None, name=f"{_TOKEN}{name}", kind=kind, **kwargs)

    @staticmethod
    def _names(body: dict) -> list[str]:
        """Fixture names from a response, with the isolating token stripped."""
        return [row["name"].removeprefix(_TOKEN) for row in body["results"]]


class LabelCollectionTests(LabelApiTestCase):
    """GET/POST ``labels/``."""

    def test_unauthenticated_is_401(self) -> None:
        self.assertEqual(self.client.get(_BASE).status_code, 401)

    def test_requires_labels_read_scope(self) -> None:
        self._grant(ApiKeyScope.LISTS_READ)
        self.assertEqual(self.client.get(_BASE, **_bearer(self.raw_key)).status_code, 403)

    def test_lists_scope_does_not_grant_labels(self) -> None:
        """Labels carry their own scope precisely because their writes reach further."""
        self._grant(ApiKeyScope.LISTS_READ, ApiKeyScope.LISTS_WRITE)
        self.assertEqual(self.client.get(_BASE, **_bearer(self.raw_key)).status_code, 403)

    def test_pagination_envelope_shape(self) -> None:
        self._mine()
        body = self.client.get(_BASE, **_bearer(self.raw_key)).json()
        self.assertEqual(sorted(body.keys()), ["count", "next", "previous", "results"])

    def test_own_and_global_labels_are_visible_but_not_other_users(self) -> None:
        other = baker.make(User)
        ensure_label(profile=Profile.objects.get(user=other), name=f"{_TOKEN}Theirs", kind=KIND_TAG)
        self._mine("Mine")
        self._global("Shared")

        names = set(self._names(self.client.get(_scoped(), **_bearer(self.raw_key)).json()))
        self.assertIn("Mine", names)
        self.assertIn("Shared", names)
        self.assertNotIn("Theirs", names)

    def test_effective_fields_reflect_the_callers_customization(self) -> None:
        """Proves the with_customizations_for prefetch is actually applied.

        Without it these fields do not error - they silently report the
        label's own styling instead of the caller's override.
        """
        label = self._global("Shared")
        LabelCustomization.objects.create(profile=self.profile, label=label, name="My name", color="#ff0000")

        row = next(
            r
            for r in self.client.get(_scoped(), **_bearer(self.raw_key)).json()["results"]
            if r["uuid"] == str(label.uuid)
        )
        self.assertEqual(row["name"], f"{_TOKEN}Shared")
        self.assertEqual(row["effective_name"], "My name")
        self.assertEqual(row["effective_color"], "#ff0000")
        self.assertTrue(row["is_customized"])

    def test_is_global_and_is_editable_flags(self) -> None:
        self._mine("Mine")
        self._global("Shared")
        rows = {
            r["name"].removeprefix(_TOKEN): r
            for r in self.client.get(_scoped(), **_bearer(self.raw_key)).json()["results"]
        }
        self.assertFalse(rows["Mine"]["is_global"])
        self.assertTrue(rows["Mine"]["is_editable"])
        self.assertTrue(rows["Shared"]["is_global"])
        self.assertFalse(rows["Shared"]["is_editable"])

    def test_protected_label_is_not_editable(self) -> None:
        label = self._mine("Locked", is_protected=True)
        row = next(
            r
            for r in self.client.get(_scoped(), **_bearer(self.raw_key)).json()["results"]
            if r["uuid"] == str(label.uuid)
        )
        self.assertFalse(row["is_editable"])

    def test_counts_are_absent_unless_requested(self) -> None:
        self._mine()
        row = self.client.get(_scoped(), **_bearer(self.raw_key)).json()["results"][0]
        self.assertNotIn("pin_count", row)

        row = self.client.get(_scoped("with_counts=true"), **_bearer(self.raw_key)).json()["results"][0]
        self.assertIn("pin_count", row)

    def test_kind_and_q_filters(self) -> None:
        self._mine("Alpha", kind=KIND_TAG)
        self._mine("Beta", kind=KIND_CATEGORY)

        body = self.client.get(_scoped(f"kind={KIND_CATEGORY}"), **_bearer(self.raw_key)).json()
        self.assertEqual(self._names(body), ["Beta"])

        body = self.client.get(f"{_BASE}?q={_TOKEN}Alph", **_bearer(self.raw_key)).json()
        self.assertEqual(self._names(body), ["Alpha"])

    def test_is_global_filter(self) -> None:
        self._mine("Mine")
        self._global("Shared")
        body = self.client.get(_scoped("is_global=true"), **_bearer(self.raw_key)).json()
        self.assertEqual(self._names(body), ["Shared"])

    def test_create_is_always_owned_by_the_caller(self) -> None:
        """A client can never create a site-wide label."""
        response = self.client.post(
            _BASE,
            {"name": "New tag", "kind": KIND_TAG},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Label.objects.get(name="New tag").profile, self.profile)

    def test_create_requires_kind(self) -> None:
        response = self.client.post(
            _BASE, {"name": "No kind"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_unknown_parent_is_400(self) -> None:
        response = self.client.post(
            _BASE,
            {"name": "Child", "kind": KIND_TAG, "parent_uuids": [str(uuid4())]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_with_another_users_parent_is_400(self) -> None:
        other = baker.make(User)
        foreign = ensure_label(profile=Profile.objects.get(user=other), name="Theirs", kind=KIND_TAG)
        response = self.client.post(
            _BASE,
            {"name": "Child", "kind": KIND_TAG, "parent_uuids": [str(foreign.uuid)]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)


class LabelDetailTests(LabelApiTestCase):
    """GET/PATCH/DELETE ``labels/{uuid}/``."""

    def _url(self, label: Label) -> str:
        return f"{_BASE}{label.uuid}/"

    def test_get_own_label(self) -> None:
        label = self._mine("Mine")
        self.assertEqual(self.client.get(self._url(label), **_bearer(self.raw_key)).json()["name"], label.name)

    def test_get_global_label_is_allowed(self) -> None:
        label = self._global("Shared")
        self.assertEqual(self.client.get(self._url(label), **_bearer(self.raw_key)).status_code, 200)

    def test_another_users_label_is_404(self) -> None:
        other = baker.make(User)
        theirs = ensure_label(profile=Profile.objects.get(user=other), name="Theirs", kind=KIND_TAG)
        self.assertEqual(self.client.get(self._url(theirs), **_bearer(self.raw_key)).status_code, 404)

    def test_patch_own_label(self) -> None:
        label = self._mine("Old")
        response = self.client.patch(
            self._url(label), {"name": "New"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 200)
        label.refresh_from_db()
        self.assertEqual(label.name, "New")

    def test_patch_global_label_is_403(self) -> None:
        label = self._global("Shared")
        original_name = label.name
        response = self.client.patch(
            self._url(label), {"name": "Hijack"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 403)
        label.refresh_from_db()
        self.assertEqual(label.name, original_name)

    def test_patch_protected_label_is_403(self) -> None:
        label = self._mine("Visited", is_protected=True)
        response = self.client.patch(
            self._url(label), {"name": "Nope"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 403)

    def test_patch_cannot_change_kind(self) -> None:
        """Cross-kind conversion is deliberately out of scope."""
        label = self._mine("Mine", kind=KIND_TAG)
        self.client.patch(
            self._url(label),
            {"kind": KIND_CATEGORY},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        label.refresh_from_db()
        self.assertEqual(label.kind, KIND_TAG)

    def test_patch_refuses_a_parent_that_would_create_a_cycle(self) -> None:
        parent = self._mine("Parent")
        child = self._mine("Child")
        child.parents.add(parent)

        # Making the child a parent of its own parent closes a loop.
        response = self.client.patch(
            self._url(parent),
            {"parent_uuids": [str(child.uuid)]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(parent.parents.count(), 0)

    def test_patch_refuses_self_as_parent(self) -> None:
        label = self._mine("Mine")
        response = self.client.patch(
            self._url(label),
            {"parent_uuids": [str(label.uuid)]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_accepts_a_safe_parent(self) -> None:
        parent = self._mine("Parent")
        child = self._mine("Child")
        response = self.client.patch(
            self._url(child),
            {"parent_uuids": [str(parent.uuid)]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(child.parents.all()), [parent])

    def test_delete_own_label(self) -> None:
        label = self._mine("Doomed")
        self.assertEqual(self.client.delete(self._url(label), **_bearer(self.raw_key)).status_code, 204)
        self.assertFalse(Label.objects.filter(pk=label.pk).exists())

    def test_delete_global_label_is_403(self) -> None:
        label = self._global("Shared")
        self.assertEqual(self.client.delete(self._url(label), **_bearer(self.raw_key)).status_code, 403)
        self.assertTrue(Label.objects.filter(pk=label.pk).exists())

    def test_delete_protected_label_is_403(self) -> None:
        label = self._mine("Visited", is_protected=True)
        self.assertEqual(self.client.delete(self._url(label), **_bearer(self.raw_key)).status_code, 403)


class LabelCustomizationEndpointTests(LabelApiTestCase):
    """PUT/DELETE ``labels/{uuid}/customization/``."""

    def _url(self, label: Label) -> str:
        return f"{_BASE}{label.uuid}/customization/"

    def test_customizing_a_global_label_is_allowed(self) -> None:
        """The one way a client changes how a shared label looks to its user."""
        label = self._global("Shared")
        original_name = label.name
        response = self.client.put(
            self._url(label),
            {"name": "My name", "color": "#00ff00"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["effective_name"], "My name")
        self.assertTrue(LabelCustomization.objects.filter(profile=self.profile, label=label).exists())
        # The shared label itself is untouched.
        label.refresh_from_db()
        self.assertEqual(label.name, original_name)

    def test_all_blank_overrides_delete_the_row(self) -> None:
        label = self._global("Shared")
        LabelCustomization.objects.create(profile=self.profile, label=label, name="Old")

        response = self.client.put(
            self._url(label),
            {"name": "", "icon": "", "color": ""},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LabelCustomization.objects.filter(profile=self.profile, label=label).exists())
        self.assertEqual(response.json()["effective_name"], label.name)

    def test_delete_clears_the_customization(self) -> None:
        label = self._global("Shared")
        LabelCustomization.objects.create(profile=self.profile, label=label, name="Mine")

        response = self.client.delete(self._url(label), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LabelCustomization.objects.filter(profile=self.profile, label=label).exists())

    def test_customization_is_private_to_the_caller(self) -> None:
        label = self._global("Shared")
        other = baker.make(User)
        other_profile = Profile.objects.get(user=other)
        LabelCustomization.objects.create(profile=other_profile, label=label, name="Their name")

        body = self.client.get(f"{_BASE}{label.uuid}/", **_bearer(self.raw_key)).json()
        self.assertEqual(body["effective_name"], label.name)

    def test_requires_labels_write_scope(self) -> None:
        label = self._global("Shared")
        self._grant(ApiKeyScope.LABELS_READ)
        response = self.client.put(
            self._url(label), {"name": "x"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 403)

    def test_another_users_label_is_404(self) -> None:
        other = baker.make(User)
        theirs = ensure_label(profile=Profile.objects.get(user=other), name="Theirs", kind=KIND_TAG)
        response = self.client.put(
            self._url(theirs), {"name": "x"}, content_type="application/json", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 404)


class LabelMergeEndpointTests(LabelApiTestCase):
    """POST ``labels/{uuid}/merge/``."""

    def _url(self, target: Label) -> str:
        return f"{_BASE}{target.uuid}/merge/"

    def _merge(self, target: Label, *sources: Label):
        return self.client.post(
            self._url(target),
            {"source_uuids": [str(source.uuid) for source in sources]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

    def test_merge_moves_pins_and_deletes_the_source(self) -> None:
        from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

        target = self._mine("Keep")
        source = self._mine("Drop")
        pin = create_pin_for_profile(self.profile, name="P", latitude=1.0, longitude=1.0).pin
        pin.labels.add(source)

        response = self._merge(target, source)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pins_moved"], 1)
        self.assertFalse(Label.objects.filter(pk=source.pk).exists())
        self.assertIn(target, pin.labels.all())

    def test_cross_kind_merge_is_refused(self) -> None:
        target = self._mine("Tag", kind=KIND_TAG)
        source = self._mine("Category", kind=KIND_CATEGORY)
        response = self._merge(target, source)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Label.objects.filter(pk=source.pk).exists())

    def test_global_label_cannot_be_a_source(self) -> None:
        target = self._mine("Mine", kind=KIND_CATEGORY)
        shared = self._global("Shared", kind=KIND_CATEGORY)
        response = self._merge(target, shared)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Label.objects.filter(pk=shared.pk).exists())

    def test_protected_label_cannot_be_a_source(self) -> None:
        target = self._mine("Keep")
        protected = self._mine("Visited", is_protected=True)
        response = self._merge(target, protected)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Label.objects.filter(pk=protected.pk).exists())

    def test_merging_into_itself_is_refused(self) -> None:
        label = self._mine("Solo")
        self.assertEqual(self._merge(label, label).status_code, 400)

    def test_another_users_label_cannot_be_a_source(self) -> None:
        target = self._mine("Keep")
        other = baker.make(User)
        theirs = ensure_label(profile=Profile.objects.get(user=other), name="Theirs", kind=KIND_TAG)
        response = self._merge(target, theirs)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Label.objects.filter(pk=theirs.pk).exists())

    def test_children_are_reparented_onto_the_target(self) -> None:
        target = self._mine("Keep")
        source = self._mine("Drop")
        child = self._mine("Child")
        child.parents.add(source)

        self.assertEqual(self._merge(target, source).status_code, 200)
        child.refresh_from_db()
        self.assertEqual(list(child.parents.all()), [target])

    def test_requires_labels_write_scope(self) -> None:
        target = self._mine("Keep")
        source = self._mine("Drop")
        self._grant(ApiKeyScope.LABELS_READ)
        self.assertEqual(self._merge(target, source).status_code, 403)
