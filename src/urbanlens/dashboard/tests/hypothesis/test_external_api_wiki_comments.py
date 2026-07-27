"""Comment visibility on the external API's wiki surface.

The gate that matters most here is the ``@[Display](loc:<uuid>)`` mention
check. A comment naming a location the viewer has not pinned must vanish
*entirely* - not be returned with the mention redacted, and certainly not with
the raw token intact. Even revealing that "a comment here mentions somewhere
you can't see" tells the viewer that place exists and that someone connected it
to this one.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"


class WikiCommentMentionGateTests(TestCase):
    """A comment mentioning an undiscovered location is absent, not redacted."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Comment client")
        grant_wiki_scopes(self.user)

        # The wiki under test - the caller has pinned this one.
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

        # A place the caller has NOT pinned, which a comment will mention.
        self.secret_location = baker.make("dashboard.Location")

        # comment_visibility defaults to ANYTHING_IN_COMMON, which correctly
        # hides a stranger's comments from this viewer (gate 1). These tests are
        # about the *mention* gate, so the author opts into ANYONE to let their
        # comments reach gate 3 at all.
        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()

    def _get_comments(self):
        return self.client.get(f"{BASE}/{self.location.ensure_slug()}/comments/", HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")

    def test_comment_mentioning_an_undiscovered_location_is_absent(self) -> None:
        """The whole comment disappears - it is not returned with the mention stripped."""
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"Pairs well with @[Secret Spot](loc:{self.secret_location.uuid})")
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text="A perfectly ordinary comment")

        body = self._get_comments().json()
        texts = [row["text"] for row in body["results"]]
        self.assertEqual(texts, ["A perfectly ordinary comment"])

    def test_the_undiscovered_uuid_appears_nowhere_in_the_response(self) -> None:
        """Not in text, not in mentions, not anywhere in the raw bytes."""
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"Check out @[Secret Spot](loc:{self.secret_location.uuid})")

        raw = self._get_comments().content.decode()
        self.assertNotIn(str(self.secret_location.uuid), raw)
        self.assertNotIn("Secret Spot", raw)

    def test_a_mention_of_a_pinned_location_is_returned_and_resolved(self) -> None:
        """Once the viewer has pinned it, the mention is safe to name."""
        baker.make("dashboard.Pin", profile=self.profile, location=self.secret_location)
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"Check out @[Secret Spot](loc:{self.secret_location.uuid})")

        rows = self._get_comments().json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mentions"], [{"display": "Secret Spot", "location_slug": self.secret_location.ensure_slug()}])

    def test_a_reply_mentioning_an_undiscovered_location_is_dropped(self) -> None:
        """The gate applies to replies too, not just top-level comments."""
        parent = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text="Parent comment")
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, parent=parent, text=f"see @[Secret Spot](loc:{self.secret_location.uuid})")
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, parent=parent, text="Visible reply")

        rows = self._get_comments().json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual([reply["text"] for reply in rows[0]["replies"]], ["Visible reply"])
        self.assertNotIn(str(self.secret_location.uuid), self._get_comments().content.decode())

    def test_pending_scan_comment_is_hidden_from_other_viewers(self) -> None:
        """An unscanned image's comment stays private to its uploader."""
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text="Unscanned upload", pending_scan=True)
        self.assertEqual(self._get_comments().json()["results"], [])

    def test_own_pending_scan_comment_is_still_visible_to_its_author(self) -> None:
        """The uploader can see their own comment while it is being scanned."""
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.profile, text="My unscanned upload", pending_scan=True)
        texts = [row["text"] for row in self._get_comments().json()["results"]]
        self.assertEqual(texts, ["My unscanned upload"])


class WikiCommentWriteTests(TestCase):
    """Posting, deleting, and reacting to wiki comments."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Comment writer")
        grant_wiki_scopes(self.user)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def _url(self, suffix: str = "") -> str:
        return f"{BASE}/{self.location.ensure_slug()}/comments/{suffix}"

    def _headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def test_posting_a_comment_creates_it(self) -> None:
        response = self.client.post(self._url(), {"text": "Great spot"}, content_type="application/json", **self._headers())
        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.wiki.comments.filter(text="Great spot").exists())

    def test_empty_text_is_rejected(self) -> None:
        response = self.client.post(self._url(), {"text": "   "}, content_type="application/json", **self._headers())
        self.assertEqual(response.status_code, 400)

    def test_deleting_someone_elses_comment_is_a_404(self) -> None:
        """Not a 403 - another author's comment id must look nonexistent."""
        other = Profile.objects.get(user=baker.make(User))
        comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=other, text="Theirs")

        response = self.client.delete(self._url(f"{comment.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})
        self.assertTrue(self.wiki.comments.filter(pk=comment.pk).exists())

    def test_deleting_own_comment_works(self) -> None:
        comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.profile, text="Mine")
        response = self.client.delete(self._url(f"{comment.pk}/"), **self._headers())
        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.wiki.comments.filter(pk=comment.pk).exists())

    def test_reactions_are_idempotent(self) -> None:
        """PUT is declarative: repeating it must not toggle the reaction back off."""
        comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.profile, text="React to me")
        url = self._url(f"{comment.pk}/reactions/%F0%9F%91%8D/")

        first = self.client.put(url, **self._headers())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reactions"]["👍"]["count"], 1)

        second = self.client.put(url, **self._headers())
        self.assertEqual(second.json()["reactions"]["👍"]["count"], 1)

        removed = self.client.delete(url, **self._headers())
        self.assertEqual(removed.json().get("reactions", {}), {})

    def test_unsupported_emoji_is_rejected(self) -> None:
        comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.profile, text="React to me")
        response = self.client.put(self._url(f"{comment.pk}/reactions/%F0%9F%92%80/"), **self._headers())
        self.assertEqual(response.status_code, 400)
