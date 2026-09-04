"""Tests for the external API's pin sub-resource endpoints.

Covers ``pins/{slug}/notes/``, ``aliases/``, ``links/`` and ``visits/`` - the
list/create/delete surface layered on top of pin detail. The behavior most
worth pinning down here is the part that is invisible in a response body: an
alias or link deletion must leave a ``PinAutoRemoval`` tombstone behind, or
the plugin panels and the pin<->wiki alias mirror recreate the deleted row on
their next run and silently undo the user's deletion.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.models import User
from django.utils import timezone
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.external_api.views import LOCATION_SEARCH_SOURCES, parse_search_sources
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

#: Every scope these endpoints need. The default grant a new key gets omits
#: the visit scopes, so tests that touch visits must set this explicitly.
_ALL_SCOPES = [
    ApiKeyScope.PINS_READ.value,
    ApiKeyScope.PINS_WRITE.value,
    ApiKeyScope.VISITS_READ.value,
    ApiKeyScope.VISITS_WRITE.value,
]


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _base(pin: Pin) -> str:
    return f"/dashboard/api/external/v1/pins/{pin.slug or pin.uuid}/"


class SubResourceTestCase(TestCase):
    """Shared fixture: one user, one API key with every relevant scope, one pin."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Sub-resource client")
        ApiKey.objects.filter(user=self.user).update(scopes=_ALL_SCOPES)
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def _other_users_pin(self) -> Pin:
        other = baker.make(User)
        return create_pin_for_profile(
            Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0
        ).pin

    def _get(self, path: str):
        return self.client.get(path, **_bearer(self.raw_key))

    def _post(self, path: str, payload: dict):
        return self.client.post(path, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def _delete(self, path: str):
        return self.client.delete(path, **_bearer(self.raw_key))

    def _patch(self, path: str, payload: dict):
        return self.client.patch(path, data=payload, content_type="application/json", **_bearer(self.raw_key))


class PinNoteEndpointTests(SubResourceTestCase):
    """Notes list, create, and delete."""

    def test_list_returns_the_standard_pagination_envelope(self) -> None:
        PinNote.objects.create(pin=self.pin, text="Catwalk is rotten")
        body = self._get(f"{_base(self.pin)}notes/").json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["text"], "Catwalk is rotten")

    def test_create_returns_201_and_persists_the_note(self) -> None:
        response = self._post(f"{_base(self.pin)}notes/", {"text": "  Bring a respirator  "})
        self.assertEqual(response.status_code, 201, response.content)
        # The service strips before saving, so the stored note has no padding.
        self.assertEqual(PinNote.objects.get(pin=self.pin).text, "Bring a respirator")

    def test_create_rejects_a_blank_note(self) -> None:
        self.assertEqual(self._post(f"{_base(self.pin)}notes/", {"text": "   "}).status_code, 400)

    def test_delete_returns_204_and_removes_the_note(self) -> None:
        note = PinNote.objects.create(pin=self.pin, text="Temporary")
        self.assertEqual(self._delete(f"{_base(self.pin)}notes/{note.pk}/").status_code, 204)
        self.assertFalse(PinNote.objects.filter(pk=note.pk).exists())

    def test_another_users_pin_is_a_404(self) -> None:
        self.assertEqual(self._get(f"{_base(self._other_users_pin())}notes/").status_code, 404)

    def test_missing_scope_is_forbidden(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PROFILE_READ.value])
        self.assertEqual(self._get(f"{_base(self.pin)}notes/").status_code, 403)

    def test_write_requires_the_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self._post(f"{_base(self.pin)}notes/", {"text": "nope"}).status_code, 403)


class PinAliasEndpointTests(SubResourceTestCase):
    """Aliases list, create, delete, and the "use this name" promotion."""

    def test_create_returns_201(self) -> None:
        response = self._post(f"{_base(self.pin)}aliases/", {"name": "The Mill"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="The Mill").exists())

    def test_duplicate_alias_is_a_conflict_case_insensitively(self) -> None:
        # Pin.save() already stored "Old Mill" as an alias when the pin was named.
        response = self._post(f"{_base(self.pin)}aliases/", {"name": "old mill"})
        self.assertEqual(response.status_code, 409, response.content)

    def test_list_flags_the_current_name(self) -> None:
        body = self._get(f"{_base(self.pin)}aliases/").json()
        current = [row for row in body["results"] if row["is_current"]]
        self.assertEqual([row["name"] for row in current], ["Old Mill"])

    def test_deleting_the_current_name_is_refused(self) -> None:
        alias = PinAlias.objects.get(pin=self.pin, name="Old Mill")
        self.assertEqual(self._delete(f"{_base(self.pin)}aliases/{alias.pk}/").status_code, 400)
        self.assertTrue(PinAlias.objects.filter(pk=alias.pk).exists())

    def test_delete_records_an_auto_removal_tombstone(self) -> None:
        alias = PinAlias.objects.create(pin=self.pin, name="Disused Mill")
        self.assertEqual(self._delete(f"{_base(self.pin)}aliases/{alias.pk}/").status_code, 204)
        self.assertFalse(PinAlias.objects.filter(pk=alias.pk).exists())
        # Without this row, the pin<->wiki alias mirror recreates the alias.
        self.assertTrue(PinAutoRemoval.objects.filter(pin=self.pin, kind=AutoRemovalKind.ALIAS).exists())

    def test_use_promotes_the_alias_and_keeps_the_old_name(self) -> None:
        alias = PinAlias.objects.create(pin=self.pin, name="Riverside Works")
        response = self._post(f"{_base(self.pin)}aliases/{alias.pk}/use/", {})
        self.assertEqual(response.status_code, 200, response.content)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Riverside Works")
        # The outgoing name must survive as an alias, or the rename is not reversible.
        self.assertTrue(PinAlias.objects.filter(pin=self.pin, name="Old Mill").exists())

    def test_use_returns_the_full_pin_detail_shape(self) -> None:
        alias = PinAlias.objects.create(pin=self.pin, name="Riverside Works")
        body = self._post(f"{_base(self.pin)}aliases/{alias.pk}/use/", {}).json()
        # The same payload GET pins/{slug}/ serves, so a client reuses its parser.
        self.assertEqual(body["name"], "Riverside Works")
        self.assertIn("security", body)
        self.assertIn("custom_fields", body)

    def test_use_does_not_change_the_slug(self) -> None:
        original_slug = self.pin.slug
        alias = PinAlias.objects.create(pin=self.pin, name="Riverside Works")
        self._post(f"{_base(self.pin)}aliases/{alias.pk}/use/", {})
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.slug, original_slug)


class PinLinkEndpointTests(SubResourceTestCase):
    """Links list, create, and delete."""

    def test_create_returns_201(self) -> None:
        response = self._post(f"{_base(self.pin)}links/", {"name": "History", "url": "https://example.com/mill"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(PinLink.objects.filter(pin=self.pin, url="https://example.com/mill").exists())

    def test_create_rejects_a_non_http_scheme(self) -> None:
        response = self._post(f"{_base(self.pin)}links/", {"url": "ftp://example.com/mill"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PinLink.objects.filter(pin=self.pin).exists())

    def test_blank_name_falls_back_to_the_display_name(self) -> None:
        body = self._post(f"{_base(self.pin)}links/", {"url": "https://example.com/mill"}).json()
        self.assertTrue(body["name"])

    def test_unarchived_wayback_url_serializes_as_null(self) -> None:
        body = self._post(f"{_base(self.pin)}links/", {"url": "https://example.com/mill"}).json()
        self.assertIsNone(body["wayback_url"])

    def test_delete_records_an_auto_removal_tombstone(self) -> None:
        link = PinLink.objects.create(pin=self.pin, name="History", url="https://example.com/mill")
        self.assertEqual(self._delete(f"{_base(self.pin)}links/{link.pk}/").status_code, 204)
        self.assertFalse(PinLink.objects.filter(pk=link.pk).exists())
        # Without this row, a plugin panel recreates the link when its cache expires.
        self.assertTrue(PinAutoRemoval.objects.filter(pin=self.pin, kind=AutoRemovalKind.LINK).exists())

    def test_unknown_link_id_is_a_404(self) -> None:
        self.assertEqual(self._delete(f"{_base(self.pin)}links/999999/").status_code, 404)


class PinVisitEndpointTests(SubResourceTestCase):
    """Visits list, create, and delete - scoped to ``visits:*``, not ``pins:*``."""

    def setUp(self) -> None:
        super().setUp()
        self.visited_at = timezone.now() - timedelta(days=10)

    def test_create_returns_201_and_updates_last_visited(self) -> None:
        response = self._post(f"{_base(self.pin)}visits/", {"visited_at": self.visited_at.isoformat()})
        self.assertEqual(response.status_code, 201, response.content)

        self.pin.refresh_from_db()
        self.assertIsNotNone(self.pin.last_visited)

    def test_create_adds_the_visited_status_label(self) -> None:
        # Every profile is seeded with a default "Visited" status label, and
        # add_visited_status looks it up by (profile, kind, name) - so this must
        # assert on the seeded row. Creating another "Visited" label here would
        # pass the lookup a duplicate the service never selects.
        visited = Label.objects.get(profile=self.profile, kind="status", name="Visited")
        response = self._post(f"{_base(self.pin)}visits/", {"visited_at": self.visited_at.isoformat()})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(self.pin.labels.filter(pk=visited.pk).exists())

    def test_create_is_forbidden_when_visit_tracking_is_off(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(track_pin_visits=False)
        response = self._post(f"{_base(self.pin)}visits/", {"visited_at": self.visited_at.isoformat()})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())

    def test_list_includes_a_photo_count(self) -> None:
        PinVisit.objects.create(pin=self.pin, visited_at=self.visited_at)
        body = self._get(f"{_base(self.pin)}visits/").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["photo_count"], 0)

    def test_delete_returns_204_and_rederives_last_visited(self) -> None:
        visit = PinVisit.objects.create(pin=self.pin, visited_at=self.visited_at)
        self.assertEqual(self._delete(f"{_base(self.pin)}visits/{visit.pk}/").status_code, 204)

        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.last_visited)

    def test_patch_updates_notes_only(self) -> None:
        visit = PinVisit.objects.create(pin=self.pin, visited_at=self.visited_at, notes="Original")
        response = self._patch(f"{_base(self.pin)}visits/{visit.pk}/", {"notes": "Revised"})
        self.assertEqual(response.status_code, 200, response.content)
        visit.refresh_from_db()
        self.assertEqual(visit.notes, "Revised")
        self.assertEqual(visit.visited_at, self.visited_at)

    def test_patch_updates_visited_at_and_rederives_last_visited(self) -> None:
        visit = PinVisit.objects.create(pin=self.pin, visited_at=self.visited_at)
        new_date = self.visited_at + timedelta(days=5)
        response = self._patch(f"{_base(self.pin)}visits/{visit.pk}/", {"visited_at": new_date.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        visit.refresh_from_db()
        self.assertEqual(visit.visited_at, new_date)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, new_date)

    def test_patch_with_no_fields_leaves_the_visit_unchanged(self) -> None:
        visit = PinVisit.objects.create(pin=self.pin, visited_at=self.visited_at, notes="Keep me")
        response = self._patch(f"{_base(self.pin)}visits/{visit.pk}/", {})
        self.assertEqual(response.status_code, 200, response.content)
        visit.refresh_from_db()
        self.assertEqual(visit.notes, "Keep me")

    def test_patch_unknown_visit_id_is_a_404(self) -> None:
        response = self._patch(f"{_base(self.pin)}visits/999999/", {"notes": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_patch_another_users_pin_is_a_404(self) -> None:
        other_pin = self._other_users_pin()
        visit = PinVisit.objects.create(pin=other_pin, visited_at=self.visited_at)
        response = self._patch(f"{_base(other_pin)}visits/{visit.pk}/", {"notes": "hijacked"})
        self.assertEqual(response.status_code, 404)

    def test_pins_scopes_do_not_grant_visit_access(self) -> None:
        # Visit history is movement data; a client trusted with a pin's contents
        # is not automatically trusted with where its owner has been.
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value])
        self.assertEqual(self._get(f"{_base(self.pin)}visits/").status_code, 403)

    def test_another_users_pin_is_a_404(self) -> None:
        self.assertEqual(self._get(f"{_base(self._other_users_pin())}visits/").status_code, 404)

    def test_unknown_pin_is_a_404(self) -> None:
        self.assertEqual(self._get(f"/dashboard/api/external/v1/pins/{uuid4()}/visits/").status_code, 404)


#: Every strategy in SubResourcePureLogicTests is trivial (short text, or a
#: list drawn from two constants), so a too_slow report means the machine was
#: busy - this suite runs alongside others - not that generation is expensive.
#: Same suppression the rest of this package's property tests apply.
_PURE_LOGIC_SETTINGS = hyp_settings(suppress_health_check=[HealthCheck.too_slow])


class SubResourcePureLogicTests(SimpleTestCase):
    """Property-based checks for the pure helpers these endpoints rely on.

    ``@given`` is used only here: this repo's TestCase keeps ``self.client``
    state across generated examples, so view-level tests above stay
    example-based on purpose.
    """

    @_PURE_LOGIC_SETTINGS
    @given(st.text())
    def test_name_normalization_is_idempotent(self, name: str) -> None:
        once = normalize_name_for_comparison(name)
        self.assertEqual(normalize_name_for_comparison(once), once)

    @_PURE_LOGIC_SETTINGS
    @given(st.text(alphabet=st.characters(codec="ascii")))
    def test_name_normalization_ignores_case(self, name: str) -> None:
        # ASCII only: a few characters change length under case mapping (U+00DF
        # "sharp s" uppercases to "SS"), and since normalization also strips
        # non-alphanumerics, upper() and lower() of such a character genuinely
        # do not normalize alike. That is a property of Unicode casing, not a
        # defect in the comparison helper.
        self.assertEqual(normalize_name_for_comparison(name.upper()), normalize_name_for_comparison(name.lower()))

    @_PURE_LOGIC_SETTINGS
    @given(st.lists(st.sampled_from(sorted(LOCATION_SEARCH_SOURCES))))
    def test_known_sources_round_trip_through_the_csv_parser(self, sources: list[str]) -> None:
        self.assertEqual(parse_search_sources(",".join(sources)), frozenset(sources))

    @_PURE_LOGIC_SETTINGS
    @given(st.text())
    def test_source_parsing_never_invents_an_unknown_source(self, raw: str) -> None:
        self.assertTrue(parse_search_sources(raw) <= LOCATION_SEARCH_SOURCES)

    def test_absent_sources_param_means_every_source(self) -> None:
        self.assertEqual(parse_search_sources(None), LOCATION_SEARCH_SOURCES)
