"""Wiki detail, PATCH, stat votes, scopes, and the community-count privacy rules."""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"


class WikiDetailBaseTestCase(TestCase):
    """Shared fixture: one wiki the caller has pinned."""

    def setUp(self) -> None:
        # The fuzzed pin count is cached per wiki for a day; without clearing it
        # a count from an earlier test can bleed into this one.
        cache.clear()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Wiki client")
        grant_wiki_scopes(self.user)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self, suffix: str = "") -> str:
        return f"{BASE}/{self.location.ensure_slug()}/{suffix}"


class WikiDetailGetTests(WikiDetailBaseTestCase):
    """GET returns the full detail payload."""

    def test_core_identity_fields(self) -> None:
        body = self.client.get(self.url(), **self.headers()).json()
        self.assertEqual(body["name"], "Old Mill")
        self.assertEqual(body["uuid"], str(self.wiki.uuid))
        self.assertEqual(body["location_slug"], self.location.ensure_slug())

    def test_location_slug_round_trips(self) -> None:
        """The slug the payload hands back actually resolves the same wiki."""
        body = self.client.get(self.url(), **self.headers()).json()
        again = self.client.get(f"{BASE}/{body['location_slug']}/", **self.headers())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["uuid"], body["uuid"])

    def test_security_is_nested(self) -> None:
        body = self.client.get(self.url(), **self.headers()).json()
        self.assertIn("fences", body["security"])
        self.assertIn("locked", body["security"])

    def test_stats_cover_every_field(self) -> None:
        body = self.client.get(self.url(), **self.headers()).json()
        self.assertEqual(set(body["stats"]), {"danger", "vulnerability", "priority", "rating"})


class WikiCommunityCountPrivacyTests(WikiDetailBaseTestCase):
    """The pinned-user count and first-pinned date must not leak a single pinner."""

    def test_low_count_suppresses_both_the_count_and_the_date(self) -> None:
        """With only one pinner, "first pinned" *is* that person's activity."""
        body = self.client.get(self.url(), **self.headers()).json()
        self.assertTrue(body["pin_count_low"])
        self.assertIsNone(body["pin_count_approx"])
        self.assertIsNone(body["first_pinned"])

    def test_first_pinned_is_truncated_to_the_first_of_the_month(self) -> None:
        """Above the threshold the date appears, but never with a real day."""
        for _ in range(4):
            other = Profile.objects.get(user=baker.make(User))
            pin = baker.make("dashboard.Pin", profile=other, location=self.location)
            # Force a distinctive day-of-month that must not survive.
            type(pin).objects.filter(pk=pin.pk).update(created=timezone.now() - timedelta(days=17))

        body = self.client.get(self.url(), **self.headers()).json()
        self.assertFalse(body["pin_count_low"])
        self.assertIsNotNone(body["first_pinned"])
        self.assertTrue(body["first_pinned"].endswith("-01"), body["first_pinned"])
        self.assertEqual(body["first_pinned_precision"], "month")

    def test_approximate_count_is_present_once_above_the_threshold(self) -> None:
        for _ in range(4):
            other = Profile.objects.get(user=baker.make(User))
            baker.make("dashboard.Pin", profile=other, location=self.location)

        body = self.client.get(self.url(), **self.headers()).json()
        self.assertFalse(body["pin_count_low"])
        self.assertIsInstance(body["pin_count_approx"], int)


class WikiPatchTests(WikiDetailBaseTestCase):
    """PATCH applies a community edit, strictly."""

    def _patch(self, payload: dict):
        return self.client.patch(self.url(), payload, content_type="application/json", **self.headers())

    def test_name_change_is_applied_and_audited(self) -> None:
        response = self._patch({"name": "New Mill"})
        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "New Mill")
        self.assertTrue(WikiEdit.objects.filter(wiki=self.wiki, editor=self.profile).exists())

    def test_nested_security_is_applied_and_audited(self) -> None:
        """Security is nested on write, matching the read shape."""
        response = self._patch({"security": {"fences": SecurityLevel.SOME.value}})
        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.fences, SecurityLevel.SOME.value)

        edit = WikiEdit.objects.filter(wiki=self.wiki).latest("created")
        self.assertIn("fences", edit.changes)

    def test_invalid_security_value_is_rejected_not_skipped(self) -> None:
        """The internal view silently drops this; the API must not (PROBLEMS.md)."""
        response = self._patch({"security": {"fences": "extremely"}})
        self.assertEqual(response.status_code, 400)
        self.wiki.refresh_from_db()
        self.assertNotEqual(self.wiki.fences, "extremely")
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_invalid_date_is_rejected(self) -> None:
        response = self._patch({"date_abandoned": "not-a-date"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_valid_date_is_applied(self) -> None:
        response = self._patch({"date_abandoned": "1999-06-15"})
        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.date_abandoned, date(1999, 6, 15))

    def test_unknown_field_is_rejected(self) -> None:
        """A misspelled key must not read as a successful no-op write."""
        response = self._patch({"decription": "typo"})
        self.assertEqual(response.status_code, 400)


class WikiStatVoteApiTests(WikiDetailBaseTestCase):
    """Casting, reading, and withdrawing a community stat vote."""

    def _vote_url(self, field: str = "danger") -> str:
        return self.url(f"votes/{field}/")

    def test_cast_then_read_own_vote(self) -> None:
        cast = self.client.put(self._vote_url(), {"value": 4}, content_type="application/json", **self.headers())
        self.assertEqual(cast.status_code, 200)
        self.assertEqual(cast.json()["my_vote"], 4)

        read = self.client.get(self._vote_url(), **self.headers())
        self.assertEqual(read.json()["my_vote"], 4)

    def test_recasting_replaces_rather_than_adding(self) -> None:
        self.client.put(self._vote_url(), {"value": 2}, content_type="application/json", **self.headers())
        self.client.put(self._vote_url(), {"value": 5}, content_type="application/json", **self.headers())
        self.assertEqual(self.client.get(self._vote_url(), **self.headers()).json()["my_vote"], 5)

    def test_delete_withdraws_the_vote(self) -> None:
        self.client.put(self._vote_url(), {"value": 3}, content_type="application/json", **self.headers())
        self.client.delete(self._vote_url(), **self.headers())
        self.assertIsNone(self.client.get(self._vote_url(), **self.headers()).json()["my_vote"])

    def test_out_of_range_value_is_rejected(self) -> None:
        response = self.client.put(self._vote_url(), {"value": 9}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 400)

    def test_unknown_stat_field_is_the_uniform_404(self) -> None:
        response = self.client.get(self._vote_url("charisma"), **self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})


class WikiScopeEnforcementTests(WikiDetailBaseTestCase):
    """A read-only key may not perform any write."""

    #: (method, suffix, body) for every write the wiki surface exposes.
    WRITES: list[tuple[str, str, dict | None]] = [
        ("patch", "", {"name": "Renamed"}),
        ("post", "history/1/revert/", None),
        ("put", "votes/danger/", {"value": 3}),
        ("delete", "votes/danger/", None),
        ("post", "aliases/", {"name": "Alias"}),
        ("delete", "aliases/1/", None),
        ("post", "links/", {"url": "https://example.com"}),
        ("delete", "links/1/", None),
        ("put", "article/", {"content": "x", "base_revision_id": None}),
        ("post", "article/revisions/1/restore/", None),
        ("post", "comments/", {"text": "hi"}),
        ("delete", "comments/1/", None),
        ("put", "comments/1/reactions/%F0%9F%91%8D/", None),
        ("delete", "comments/1/reactions/%F0%9F%91%8D/", None),
    ]

    def test_read_only_key_is_forbidden_from_every_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])

        for method, suffix, payload in self.WRITES:
            with self.subTest(method=method, suffix=suffix):
                kwargs = {"content_type": "application/json", **self.headers()}
                response = getattr(self.client, method)(
                    self.url(suffix), payload if payload is not None else {}, **kwargs
                )
                self.assertEqual(response.status_code, 403, f"{method.upper()} {suffix} was not refused")

    def test_read_scope_still_permits_reads(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value])
        self.assertEqual(self.client.get(self.url(), **self.headers()).status_code, 200)

    def test_write_scope_alone_does_not_grant_reads(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_WRITE.value])
        self.assertEqual(self.client.get(self.url(), **self.headers()).status_code, 403)


class WikiAliasesAndLinksTests(WikiDetailBaseTestCase):
    """Adding and removing a wiki's aliases and links."""

    def test_alias_create_list_delete(self) -> None:
        """Round-trip one alias.

        ``Wiki.save()`` auto-creates an alias matching the wiki's own name, so
        the list is never empty - assert on the alias under test rather than on
        the whole collection.
        """
        created = self.client.post(
            self.url("aliases/"), {"name": "The Mill"}, content_type="application/json", **self.headers()
        )
        self.assertEqual(created.status_code, 201)
        alias_id = created.json()["id"]

        listed = self.client.get(self.url("aliases/"), **self.headers()).json()
        self.assertIn("The Mill", [row["name"] for row in listed])

        removed = self.client.delete(self.url(f"aliases/{alias_id}/"), **self.headers())
        self.assertEqual(removed.status_code, 204)

        remaining = self.client.get(self.url("aliases/"), **self.headers()).json()
        self.assertNotIn("The Mill", [row["name"] for row in remaining])
        self.assertNotIn(alias_id, [row["id"] for row in remaining])

    def test_link_create_list_delete(self) -> None:
        created = self.client.post(
            self.url("links/"),
            {"name": "History", "url": "https://example.com/mill"},
            content_type="application/json",
            **self.headers(),
        )
        self.assertEqual(created.status_code, 201)
        link_id = created.json()["id"]

        listed = self.client.get(self.url("links/"), **self.headers()).json()
        self.assertIn("https://example.com/mill", [row["url"] for row in listed])

        removed = self.client.delete(self.url(f"links/{link_id}/"), **self.headers())
        self.assertEqual(removed.status_code, 204)

    def test_malformed_link_url_is_rejected(self) -> None:
        response = self.client.post(
            self.url("links/"), {"url": "not a url"}, content_type="application/json", **self.headers()
        )
        self.assertEqual(response.status_code, 400)
