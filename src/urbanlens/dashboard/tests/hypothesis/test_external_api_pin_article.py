"""A pin's own private article over the external API.

The wiki article endpoints already existed; these mirror them for a *pin*, and
the whole reason the mirror is worth testing separately is that the two look
identical and must not be treated identically:

1. **Scopes.** A pin article is the owner's private write-up - route notes,
   access details, things deliberately not published to the community page. It
   is reachable with ``pins:read``/``pins:write`` and must be unreachable with
   ``wiki:read``/``wiki:write``, which a user grants to a wiki-editing client
   with no expectation that it can read their private notes.
2. **Ownership is a lookup, not a check.** Another user's pin, and a revision
   id belonging to another article, are both "not found" - never 403, which
   would confirm they exist.
3. **Optimistic concurrency really is wired.** ``base_revision_id`` is required
   and enforced, so two clients (or the same key on two devices) cannot
   silently overwrite each other.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.wiki.articles import save_article

BASE = "/dashboard/api/external/v1/pins"


class PinArticleApiTests(TestCase):
    """GET/PUT a pin's article and walk its revision history."""

    def setUp(self) -> None:
        """Create the key owner, a bystander, and a pin with no article yet."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Article client")
        self.other_profile = Profile.objects.get(user=baker.make(User, username="bystander"))

        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def _headers(self, raw_key: str | None = None) -> dict:
        """Bearer-header kwargs for the fixture key, or an explicitly given one.

        Args:
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            Request kwargs carrying the Authorization header.
        """
        return {"HTTP_AUTHORIZATION": f"Bearer {raw_key or self.raw_key}"}

    def _url(self, suffix: str = "", *, pin_slug: str | None = None) -> str:
        """Build a pin-article URL.

        Args:
            suffix: Path fragment appended after ``article/``.
            pin_slug: Pin to address; defaults to the fixture pin.

        Returns:
            The fully-built URL.
        """
        slug = pin_slug or self.pin.slug or str(self.pin.uuid)
        return f"{BASE}/{slug}/article/{suffix}"

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes*.

        Args:
            scopes: Raw scope values to store on the row.

        Returns:
            The raw key value.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _save(self, content: str, base_revision_id: int | None = None) -> dict:
        """PUT one version of the fixture pin's article.

        Args:
            content: The Markdown source to save.
            base_revision_id: The revision the client believes it started from.

        Returns:
            The parsed response body.
        """
        response = self.client.put(
            self._url(),
            {"content": content, "base_revision_id": base_revision_id},
            content_type="application/json",
            **self._headers(),
        )
        return response.json()

    def test_pin_without_an_article_is_a_404(self) -> None:
        """"Not written yet" and "not yours" answer identically."""
        response = self.client.get(self._url(), **self._headers())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_put_creates_the_article_then_get_returns_it(self) -> None:
        """PUT is the escape hatch from the 404 GET returns on a fresh pin."""
        created = self.client.put(
            self._url(),
            {"content": "## Access\n\nSouth door.", "base_revision_id": None},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(created.status_code, 200)

        response = self.client.get(self._url(), **self._headers())
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["content"], "## Access\n\nSouth door.")
        self.assertIn("South door", body["content_html"])
        self.assertIsNotNone(body["base_revision_id"])
        self.assertEqual(Article.objects.filter(pin=self.pin).count(), 1)

    def test_the_article_is_attached_to_the_pin_not_a_wiki(self) -> None:
        """The host argument really is ``pin=`` - a wiki article would be a different row."""
        self._save("Private notes")

        article = Article.objects.get(pin=self.pin)
        self.assertIsNone(article.wiki_id)
        self.assertEqual(article.last_edited_by, self.profile)

    def test_stale_base_revision_is_a_409_and_writes_nothing(self) -> None:
        """The conflict rule is enforced, and the loser's content is discarded."""
        first = self._save("Version one")
        stale = first["base_revision_id"]
        self._save("Version two", stale)

        response = self.client.put(
            self._url(),
            {"content": "Version three from a stale client", "base_revision_id": stale},
            content_type="application/json",
            **self._headers(),
        )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body["conflict"])
        self.assertNotEqual(body["current_revision_id"], stale)
        self.assertEqual(Article.objects.get(pin=self.pin).content, "Version two")

    def test_omitting_base_revision_id_is_a_400(self) -> None:
        """Required, so an accidentally-omitted key cannot silently clobber."""
        response = self.client.put(self._url(), {"content": "No opinion"}, content_type="application/json", **self._headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid request.")
        self.assertIn("base_revision_id", response.json()["fields"])

    def test_revisions_list_is_newest_first_with_deltas(self) -> None:
        """The history endpoint pages the same rows the internal view shows."""
        first = self._save("one")
        self._save("one two", first["base_revision_id"])

        response = self.client.get(self._url("revisions/"), **self._headers())
        rows = response.json()["results"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(rows), 2)
        # Newest first: the later revision grew the article, the first created it.
        self.assertEqual(rows[0]["size_delta"], len("one two") - len("one"))
        self.assertEqual(rows[1]["size_delta"], len("one"))

    def test_revision_detail_carries_content_and_a_diff(self) -> None:
        """One request is enough to render a history entry."""
        first = self._save("one")
        second = self._save("one two", first["base_revision_id"])

        response = self.client.get(self._url(f"revisions/{second['base_revision_id']}/"), **self._headers())
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["content"], "one two")
        self.assertTrue(body["diff"])
        self.assertTrue(all({"kind", "text"} == set(row) for row in body["diff"]))

    def test_restore_appends_rather_than_rewriting_history(self) -> None:
        """History is append-only: restoring writes the old content forward."""
        first = self._save("original")
        first_revision_id = first["base_revision_id"]
        self._save("replaced", first_revision_id)

        response = self.client.post(self._url(f"revisions/{first_revision_id}/restore/"), **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "original")
        self.assertEqual(ArticleRevision.objects.filter(article__pin=self.pin).count(), 3)
        self.assertEqual(ArticleRevision.objects.filter(article__pin=self.pin, restored_from_id=first_revision_id).count(), 1)

    def test_another_users_pin_article_is_not_found(self) -> None:
        """Every handler resolves through the owner-scoped pin lookup."""
        their_pin = create_pin_for_profile(self.other_profile, name="Theirs", latitude=1.0, longitude=1.0).pin
        save_article(editor=self.other_profile, content="Their private notes", pin=their_pin)
        slug = their_pin.slug or str(their_pin.uuid)

        for suffix in ("", "revisions/"):
            with self.subTest(suffix=suffix):
                response = self.client.get(self._url(suffix, pin_slug=slug), **self._headers())
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"error": "Not found."})

    def test_a_revision_from_another_article_is_not_found(self) -> None:
        """Revision ids are sequential across every article in the database.

        Without ``article=`` in the lookup this endpoint would hand back other
        users' private pin articles one integer at a time.
        """
        self._save("mine")
        their_pin = create_pin_for_profile(self.other_profile, name="Theirs", latitude=1.0, longitude=1.0).pin
        _their_article, their_revision = save_article(editor=self.other_profile, content="Their private notes", pin=their_pin)
        assert their_revision is not None

        detail = self.client.get(self._url(f"revisions/{their_revision.pk}/"), **self._headers())
        restore = self.client.post(self._url(f"revisions/{their_revision.pk}/restore/"), **self._headers())

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(restore.status_code, 404)
        self.assertEqual(Article.objects.get(pin=self.pin).content, "mine")

    def test_wiki_scopes_cannot_reach_a_pin_article(self) -> None:
        """The privacy decision this module exists for, asserted directly.

        A key granted only "read/edit community wikis" must not be able to read
        or write a pin's private article; if this ever passes with 200 the
        consent screen has started lying.
        """
        self._save("private")
        raw = self._key_with_scopes([ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])

        read = self.client.get(self._url(), **self._headers(raw))
        write = self.client.put(self._url(), {"content": "x", "base_revision_id": None}, content_type="application/json", **self._headers(raw))
        revisions = self.client.get(self._url("revisions/"), **self._headers(raw))

        self.assertEqual(read.status_code, 403)
        self.assertEqual(write.status_code, 403)
        self.assertEqual(revisions.status_code, 403)

    def test_read_scope_cannot_write(self) -> None:
        """``pins:read`` reads the article and its history, and nothing more."""
        self._save("private")
        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value])

        self.assertEqual(self.client.get(self._url(), **self._headers(raw)).status_code, 200)
        write = self.client.put(self._url(), {"content": "x", "base_revision_id": None}, content_type="application/json", **self._headers(raw))
        self.assertEqual(write.status_code, 403)
        self.assertEqual(Article.objects.get(pin=self.pin).content, "private")
