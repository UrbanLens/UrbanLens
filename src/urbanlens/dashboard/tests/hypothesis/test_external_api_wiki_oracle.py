"""The anti-enumeration guarantee for the external API's wiki surface.

A wiki is visible only to someone who has pinned the place it describes. That
rule is worth nothing if the *error* distinguishes the cases: if "no such
location" and "you haven't pinned this one" look different, a caller can walk
slugs and learn which places other users care about, which is exactly the
inference the discovery model exists to prevent.

So these tests assert something stricter than "returns 404": for **every**
wiki route, including every sub-resource, all three of

1. a ``location_slug`` matching nothing at all,
2. a real Location with no Wiki, and
3. a real Wiki the caller has not pinned

must produce a byte-identical response - same status, same body.
"""

from __future__ import annotations

from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.throttling import ExternalApiRateThrottle
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

BASE = "/dashboard/api/external/v1/wikis"


def disable_throttling(test_case) -> None:
    """Turn off the external API's rate limits for the duration of *test_case*.

    :data:`WIKI_ROUTES` is walked once per invisibility case over a single
    credential, which is far past the burst allowance - without this the tail
    of the list comes back 429 and the visibility gate these tests exist to
    check never runs at all. Patching the shared base covers the read, write
    and burst throttles at once. The throttles themselves are covered by
    ``test_external_api_throttling.py``.

    Args:
        test_case: The test whose lifetime the patch should follow.
    """
    patcher = mock.patch.object(ExternalApiRateThrottle, "allow_request", return_value=True)
    patcher.start()
    test_case.addCleanup(patcher.stop)


def grant_wiki_scopes(user) -> None:
    """Add the wiki scopes to *user*'s API keys.

    ``_default_api_key_scopes`` deliberately grants only profile/pins/push to a
    newly issued PAT - the wiki scopes are opt-in and, until a scope-picker UI
    exists, reachable only through OAuth2. Without this, every request below
    would be refused at the permission layer (403) and never reach the
    visibility gate these tests are actually about.
    """
    ApiKey.objects.filter(user=user).update(
        scopes=[
            ApiKeyScope.PROFILE_READ.value,
            ApiKeyScope.PINS_READ.value,
            ApiKeyScope.PINS_WRITE.value,
            ApiKeyScope.WIKI_READ.value,
            ApiKeyScope.WIKI_WRITE.value,
        ],
    )

#: Every wiki route, as (method, path suffix after the location slug).
#: Sub-resource ids are arbitrary - resolution must fail at the wiki gate,
#: before any id is ever looked up.
WIKI_ROUTES: list[tuple[str, str]] = [
    ("get", ""),
    ("patch", ""),
    ("get", "history/"),
    ("post", "history/1/revert/"),
    ("get", "votes/danger/"),
    ("put", "votes/danger/"),
    ("delete", "votes/danger/"),
    ("get", "aliases/"),
    ("post", "aliases/"),
    ("delete", "aliases/1/"),
    ("get", "links/"),
    ("post", "links/"),
    ("delete", "links/1/"),
    ("get", "gallery/"),
    ("get", "article/"),
    ("put", "article/"),
    ("get", "article/revisions/"),
    ("get", "article/revisions/1/"),
    ("post", "article/revisions/1/restore/"),
    ("get", "comments/"),
    ("post", "comments/"),
    ("delete", "comments/1/"),
    ("put", "comments/1/reactions/%F0%9F%91%8D/"),
    ("delete", "comments/1/reactions/%F0%9F%91%8D/"),
]


class WikiDiscoveryOracleTests(TestCase):
    """No wiki route may distinguish "absent" from "not yours"."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Oracle client")
        grant_wiki_scopes(self.user)
        disable_throttling(self)

        # (1) nothing at this slug at all.
        self.missing_slug = str(uuid4())

        # (2) a real Location that has no Wiki.
        self.location_without_wiki = baker.make("dashboard.Location")

        # (3) a real Wiki at a place this caller has never pinned.
        self.unpinned_location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.unpinned_location, name="Someone Else's Place")

        # A control: a wiki the caller *can* see, proving the routes work at all
        # and that the 404s above aren't just "everything is broken".
        self.visible_location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.visible_location, name="My Place")
        baker.make("dashboard.Pin", profile=self.profile, location=self.visible_location)

    def _call(self, method: str, slug: str, suffix: str):
        """Issue one authenticated request against a wiki route."""
        return getattr(self.client, method)(
            f"{BASE}/{slug}/{suffix}",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )

    def test_every_route_is_identically_not_found_for_all_three_cases(self) -> None:
        """Status and body match exactly across all three invisibility causes."""
        slugs = {
            "nonexistent slug": self.missing_slug,
            "location with no wiki": self.location_without_wiki.ensure_slug(),
            "real wiki, caller has not pinned it": self.unpinned_location.ensure_slug(),
        }

        for method, suffix in WIKI_ROUTES:
            responses = {label: self._call(method, slug, suffix) for label, slug in slugs.items()}

            for label, response in responses.items():
                with self.subTest(method=method, suffix=suffix, case=label):
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json(), {"error": "Not found."})

            # Byte-for-byte, not merely equal-when-parsed.
            bodies = {label: response.content for label, response in responses.items()}
            with self.subTest(method=method, suffix=suffix, check="identical bytes"):
                self.assertEqual(len(set(bodies.values())), 1, f"Responses differ across cases for {method.upper()} {suffix}: {bodies}")

    def test_the_control_wiki_is_actually_reachable(self) -> None:
        """A pinned place's wiki really does resolve - the 404s above mean something."""
        response = self._call("get", self.visible_location.ensure_slug(), "")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "My Place")

    def test_404_body_never_explains_why(self) -> None:
        """No 404 body may leak a distinguishing reason."""
        for label, slug in (
            ("no wiki", self.location_without_wiki.ensure_slug()),
            ("not pinned", self.unpinned_location.ensure_slug()),
        ):
            with self.subTest(case=label):
                body = self._call("get", slug, "").content.decode()
                for leak in ("wiki", "pin", "permission", "allowed", "location"):
                    self.assertNotIn(leak, body.lower())


class WikiCrossScopeIdTests(TestCase):
    """A sub-resource id from another wiki must 404, never resolve."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Scope client")
        grant_wiki_scopes(self.user)

        # Two wikis, BOTH visible to this caller - so any 404 below is the id
        # scoping working, not the discovery gate.
        self.location_a = baker.make("dashboard.Location")
        self.wiki_a = baker.make("dashboard.Wiki", location=self.location_a, name="Wiki A")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location_a)

        self.location_b = baker.make("dashboard.Location")
        self.wiki_b = baker.make("dashboard.Wiki", location=self.location_b, name="Wiki B")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location_b)

        # Resources that belong to wiki B.
        self.b_edit = baker.make("dashboard.WikiEdit", wiki=self.wiki_b, editor=self.profile, changes={"name": {"from": "x", "to": "y"}})
        self.b_alias = baker.make("dashboard.WikiAlias", wiki=self.wiki_b, name="B alias")
        self.b_link = baker.make("dashboard.WikiLink", wiki=self.wiki_b, url="https://example.com/b")
        self.b_comment = baker.make("dashboard.Comment", wiki=self.wiki_b, profile=self.profile, text="B comment")

    def _headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def _a(self, suffix: str) -> str:
        """A URL under wiki A."""
        return f"{BASE}/{self.location_a.ensure_slug()}/{suffix}"

    def test_edit_id_from_another_wiki_is_not_found(self) -> None:
        response = self.client.post(self._a(f"history/{self.b_edit.pk}/revert/"), **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_alias_id_from_another_wiki_is_not_found(self) -> None:
        response = self.client.delete(self._a(f"aliases/{self.b_alias.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 404)

    def test_link_id_from_another_wiki_is_not_found(self) -> None:
        response = self.client.delete(self._a(f"links/{self.b_link.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 404)

    def test_comment_id_from_another_wiki_is_not_found(self) -> None:
        response = self.client.delete(self._a(f"comments/{self.b_comment.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 404)

    def test_deleting_a_foreign_comment_does_not_delete_it(self) -> None:
        """The 404 is a refusal, not a silent success."""
        self.client.delete(self._a(f"comments/{self.b_comment.pk}/"), **self._headers())
        self.b_comment.refresh_from_db()
        self.assertIsNotNone(self.b_comment.pk)

    def test_revision_id_from_another_wiki_is_not_found(self) -> None:
        from urbanlens.dashboard.services.wiki.articles import save_article

        _article_b, revision_b = save_article(editor=self.profile, content="B body", wiki=self.wiki_b)
        # Wiki A needs its own article, or the 404 would come from "no article
        # here" rather than from the revision being scoped to another article.
        save_article(editor=self.profile, content="A body", wiki=self.wiki_a)

        response = self.client.get(self._a(f"article/revisions/{revision_b.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})
