"""``POST /wikis/{location_slug}/aliases/{alias_id}/use/`` - adopting a community name.

Pins have had "use this name" since the alias list existed; wikis had the same
button in the HTMX UI and nothing at all in the external API, so a native client
could add and delete a place's names but never choose between them.

Three properties are worth stating up front, because each one is a bug that was
either present in a sibling implementation or easy to reintroduce:

1. **The response is built after the save.** ``Wiki.save()`` runs the name
   through ``sanitize_name``, so a payload assembled from ``alias.name``
   beforehand can report a name the database does not hold - and a client that
   caches it disagrees with every later read.
2. **Promoting the name that is already the name is a 200 no-op.** Idempotency
   is what makes a retry after a lost response safe.
3. **The alias id is scoped to the resolved wiki.** A bare lookup would make
   alias ids - which carry actual place names, not just handles - enumerable
   across every wiki in the database.

Everything here also inherits the surface-wide anti-enumeration guarantee from
``services.wiki.wiki_access.resolve_visible_wiki``; see
``test_external_api_wiki_oracle.py`` for the exhaustive version of that.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from django.core.cache import cache
from hypothesis import HealthCheck, given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import disable_throttling, grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"


class _WikiAliasUseTestCase(TestCase):
    """Shared fixture: one wiki the caller has pinned, plus a spare alias."""

    def setUp(self) -> None:
        """Create the key owner, a wiki they have pinned, and a wiki-scoped key."""
        # The fuzzed community pin count is cached per wiki for a day, and the
        # detail payload this endpoint returns includes it.
        cache.clear()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Wiki client")
        grant_wiki_scopes(self.user)
        # The property test below replays one credential dozens of times, which
        # is past the burst allowance; without this its tail comes back 429 and
        # the invariant it exists to check never runs. Rate limiting itself is
        # covered by test_external_api_throttling.py.
        disable_throttling(self)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def headers(self) -> dict:
        """Request kwargs carrying the fixture's API key as a bearer token."""
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self, suffix: str = "") -> str:
        """A URL under the fixture wiki."""
        return f"{BASE}/{self.location.ensure_slug()}/{suffix}"

    def use(self, alias_id: int):
        """POST the promote endpoint for *alias_id*."""
        return self.client.post(self.url(f"aliases/{alias_id}/use/"), **self.headers())

    def add_alias(self, name: str) -> WikiAlias:
        """Attach an alias to the fixture wiki."""
        return baker.make(WikiAlias, wiki=self.wiki, name=name)


class WikiAliasUseTests(_WikiAliasUseTestCase):
    """The happy path: the rename, its audit trail, and the response shape."""

    def test_promoting_an_alias_renames_the_wiki(self) -> None:
        alias = self.add_alias("The Grist Mill")
        response = self.use(alias.pk)

        self.assertEqual(response.status_code, 200, response.content)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "The Grist Mill")

    def test_the_response_is_the_full_wiki_detail(self) -> None:
        """A client re-renders the whole screen from this, as it does after GET."""
        alias = self.add_alias("The Grist Mill")
        body = self.use(alias.pk).json()

        self.assertEqual(body["name"], "The Grist Mill")
        self.assertEqual(body["uuid"], str(self.wiki.uuid))
        self.assertEqual(body["location_slug"], self.location.ensure_slug())
        # The parts a rename actually invalidates must be in the same payload.
        self.assertIn("aliases", body)
        self.assertIn("stats", body)

    def test_the_rename_lands_in_the_wiki_edit_history(self) -> None:
        """It is an audited community edit, indistinguishable from one typed in the form.

        This is the whole reason the operation goes through ``apply_wiki_edit``
        instead of assigning ``wiki.name``: a rename nobody can see in history
        is a rename nobody can revert.
        """
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)

        edit = WikiEdit.objects.filter(wiki=self.wiki).latest("created")
        self.assertEqual(edit.changes, {"name": {"from": "Old Mill", "to": "The Grist Mill"}})
        self.assertEqual(edit.editor, self.profile)

    def test_the_new_edit_is_visible_through_the_history_endpoint(self) -> None:
        """End-to-end: the audit row is reachable by the same client that made it."""
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)

        history = self.client.get(self.url("history/"), **self.headers()).json()
        rows = history["results"] if isinstance(history, dict) else history
        self.assertTrue(any(row["changes"].get("name", {}).get("to") == "The Grist Mill" for row in rows), rows)

    def test_the_outgoing_name_survives_as_an_alias(self) -> None:
        """Renaming must never be the way a place's old name is lost."""
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)

        names = set(self.wiki.aliases.values_list("name", flat=True))
        self.assertIn("Old Mill", names)
        self.assertIn("The Grist Mill", names)

    def test_the_rename_is_reversible_by_promoting_the_old_name_back(self) -> None:
        """The round trip a user actually performs after a mistaken rename."""
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)

        old = self.wiki.aliases.get(name="Old Mill")
        body = self.use(old.pk).json()

        self.assertEqual(body["name"], "Old Mill")
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")

    def test_promoting_an_alias_that_differs_only_in_case_is_a_real_rename(self) -> None:
        """The one case where ``is_current`` and the no-op rule deliberately disagree.

        ``is_current`` compares loosely, because it has to agree with the
        case-insensitive alias uniqueness constraint - two aliases differing only
        in case cannot both exist. But the *wiki's* name can drift out of case
        with its alias (renamed through PATCH, which touches no alias row), and
        recasing it back is a real change to how the place is displayed. So the
        alias reads as current and promoting it still writes history.
        """
        self.client.patch(self.url(), {"name": "old mill"}, content_type="application/json", **self.headers())
        alias = self.wiki.aliases.get(name="Old Mill")
        self.assertTrue(self.client.get(self.url("aliases/"), **self.headers()).json()[0]["is_current"])

        body = self.use(alias.pk).json()

        self.assertEqual(body["name"], "Old Mill")
        self.assertTrue(WikiEdit.objects.filter(wiki=self.wiki, changes__name__to="Old Mill").exists())


class WikiAliasUseNoOpTests(_WikiAliasUseTestCase):
    """Promoting the name that is already the name."""

    def test_it_answers_200_rather_than_400(self) -> None:
        """A retry of a request whose response was lost must not read as a failure."""
        current = self.wiki.aliases.get(name="Old Mill")
        response = self.use(current.pk)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], "Old Mill")

    def test_it_records_no_edit(self) -> None:
        """An empty edit in the history is noise that other people have to read past."""
        current = self.wiki.aliases.get(name="Old Mill")
        before = WikiEdit.objects.filter(wiki=self.wiki).count()
        self.use(current.pk)

        self.assertEqual(WikiEdit.objects.filter(wiki=self.wiki).count(), before)

    def test_repeating_it_stays_a_no_op(self) -> None:
        """Idempotent means idempotent on the third call too, not just the second."""
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)
        after_first = WikiEdit.objects.filter(wiki=self.wiki).count()

        self.use(alias.pk)
        self.use(alias.pk)

        self.assertEqual(WikiEdit.objects.filter(wiki=self.wiki).count(), after_first)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "The Grist Mill")


class WikiAliasUseSanitizationTests(_WikiAliasUseTestCase):
    """The response must report the name the database actually holds."""

    def test_the_payload_matches_the_stored_name_not_the_submitted_one(self) -> None:
        """Regression guard for serializing before the save.

        ``_AliasBase.save()`` normally sanitizes alias names on the way in, so
        this writes the unsanitized value with ``update()`` to reproduce the row
        an older write path (or a data import) could have left behind. Whatever
        the row holds, the response must agree with the wiki as re-read from the
        database - not with the string that was on the alias.
        """
        alias = self.add_alias("placeholder")
        WikiAlias.objects.filter(pk=alias.pk).update(name="Mill <b>House</b> ✨")

        body = self.use(alias.pk).json()

        self.wiki.refresh_from_db()
        self.assertEqual(body["name"], self.wiki.name)
        # And the sanitizer really did change it, or this test proves nothing.
        self.assertNotEqual(self.wiki.name, "Mill <b>House</b> ✨")
        self.assertNotIn("<", body["name"])


class WikiAliasIsCurrentFlagTests(_WikiAliasUseTestCase):
    """``is_current`` is what makes the promote endpoint usable from a UI."""

    def _aliases(self) -> list[dict]:
        """The alias list as the API returns it."""
        return self.client.get(self.url("aliases/"), **self.headers()).json()

    def test_exactly_one_alias_is_flagged_current(self) -> None:
        self.add_alias("The Grist Mill")
        self.add_alias("Mill Ruins")

        flagged = [row for row in self._aliases() if row["is_current"]]
        self.assertEqual([row["name"] for row in flagged], ["Old Mill"])

    def test_the_flag_follows_the_promotion(self) -> None:
        alias = self.add_alias("The Grist Mill")
        self.use(alias.pk)

        flagged = [row["name"] for row in self._aliases() if row["is_current"]]
        self.assertEqual(flagged, ["The Grist Mill"])

    def test_a_newly_created_alias_reports_the_flag(self) -> None:
        """The POST response carries the same shape as the list, flag included."""
        created = self.client.post(
            self.url("aliases/"), {"name": "Mill Ruins"}, content_type="application/json", **self.headers()
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertIs(created.json()["is_current"], False)

    @hyp_settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll"), max_codepoint=127), min_size=3, max_size=20)
    )
    def test_promoting_any_alias_leaves_exactly_one_current(self, name: str) -> None:
        """The invariant a client's list rendering depends on, over arbitrary names.

        The alphabet is letters so the generated name survives ``sanitize_name``
        intact - the property under test is "exactly one", not "the sanitizer is
        a no-op". It is further restricted to ASCII because
        ``normalize_name_for_comparison`` strips every non-ASCII character, so a
        wholly non-Latin name normalizes to the empty string and *no* alias is
        reported current. That is a real defect in shared naming code rather
        than in this endpoint (it equally breaks the "you can't delete the
        current name" guard for such places); it is reported separately, and
        this test deliberately does not encode the broken behavior as expected.
        """
        alias, _created = WikiAlias.objects.get_or_create(wiki=self.wiki, name__iexact=name, defaults={"name": name})
        self.use(alias.pk)

        rows = self._aliases()
        flagged = [row for row in rows if row["is_current"]]
        self.assertEqual(len(flagged), 1, rows)
        self.wiki.refresh_from_db()
        self.assertEqual(
            normalize_name_for_comparison(flagged[0]["name"]), normalize_name_for_comparison(self.wiki.name)
        )


class WikiAliasUseScopeTests(_WikiAliasUseTestCase):
    """Per-method scope enforcement, as every external write has."""

    def test_a_read_only_key_may_not_promote(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        alias = self.add_alias("The Grist Mill")

        response = self.use(alias.pk)

        self.assertEqual(response.status_code, 403)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")

    def test_a_pins_scoped_key_may_not_promote(self) -> None:
        """Wiki content is not reachable with the pin scopes, even on a pinned place."""
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value])
        alias = self.add_alias("The Grist Mill")

        self.assertEqual(self.use(alias.pk).status_code, 403)

    def test_an_unauthenticated_request_is_refused(self) -> None:
        alias = self.add_alias("The Grist Mill")
        response = self.client.post(self.url(f"aliases/{alias.pk}/use/"))

        self.assertEqual(response.status_code, 401)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")


class WikiAliasUseNotFoundTests(_WikiAliasUseTestCase):
    """Nothing here may confirm the existence of something the caller cannot see."""

    def setUp(self) -> None:
        """Add a second, invisible wiki alongside the fixture's visible one."""
        super().setUp()
        # A real wiki at a place this caller has never pinned.
        self.unpinned_location = baker.make("dashboard.Location")
        self.unpinned_wiki = baker.make("dashboard.Wiki", location=self.unpinned_location, name="Someone Else's Place")
        self.foreign_alias = baker.make(WikiAlias, wiki=self.unpinned_wiki, name="Their Other Name")

    def test_an_alias_from_a_wiki_the_caller_cannot_see_is_not_found(self) -> None:
        """The id is real; the answer must still be the uniform 404.

        A 403 - or any distinguishable body - would confirm that an alias with
        that id exists somewhere, which is enough to walk the table and harvest
        the names of places other people have pinned.
        """
        response = self.use(self.foreign_alias.pk)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_the_refusal_does_not_quietly_rename_anything(self) -> None:
        """A 404 must be a refusal, not a success with a misleading status."""
        self.use(self.foreign_alias.pk)

        self.wiki.refresh_from_db()
        self.unpinned_wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")
        self.assertEqual(self.unpinned_wiki.name, "Someone Else's Place")

    def test_an_alias_id_that_does_not_exist_is_not_found(self) -> None:
        response = self.use(10_000_000)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_a_nonexistent_location_slug_is_identically_not_found(self) -> None:
        """Byte-identical to the two cases above - see the module docstring."""
        alias = self.add_alias("The Grist Mill")
        missing = self.client.post(f"{BASE}/{uuid4()}/aliases/{alias.pk}/use/", **self.headers())
        unseen = self.use(self.foreign_alias.pk)

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.content, unseen.content)
