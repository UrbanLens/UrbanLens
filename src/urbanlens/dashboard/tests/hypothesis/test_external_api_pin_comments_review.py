"""Pin-scoped comments and reviews, plus properties of ``visible_comment_tree``.

These endpoints deliberately use ``pins:read``/``pins:write`` rather than the
wiki scopes: a pin's comment thread and star rating are the owner's own private
annotations of their own pin, not shared community content, so a key granted
only wiki access must not reach them.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.comments.comments import top_level_comment_queryset, visible_comment_tree
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

BASE = "/dashboard/api/external/v1/pins"


class PinCommentsApiTests(TestCase):
    """GET/POST/DELETE a pin's own comment thread."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Pin comment client")
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self, suffix: str = "") -> str:
        return f"{BASE}/{self.pin.slug or self.pin.uuid}/comments/{suffix}"

    def test_post_then_list(self) -> None:
        created = self.client.post(
            self.url(), {"text": "Note to self"}, content_type="application/json", **self.headers()
        )
        self.assertEqual(created.status_code, 201)

        rows = self.client.get(self.url(), **self.headers()).json()["results"]
        self.assertEqual([row["text"] for row in rows], ["Note to self"])
        self.assertTrue(rows[0]["author_is_self"])

    def test_delete_own_comment(self) -> None:
        comment_id = self.client.post(
            self.url(), {"text": "Temporary"}, content_type="application/json", **self.headers()
        ).json()["id"]
        removed = self.client.delete(self.url(f"{comment_id}/"), **self.headers())
        self.assertEqual(removed.status_code, 204)

    def test_another_users_pin_is_a_404(self) -> None:
        other = Profile.objects.get(user=baker.make(User))
        their_pin = create_pin_for_profile(other, name="Theirs", latitude=1.0, longitude=1.0).pin

        response = self.client.get(f"{BASE}/{their_pin.slug or their_pin.uuid}/comments/", **self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_comments_route_is_not_swallowed_by_the_pin_detail_pattern(self) -> None:
        """`<str:>` never matches "/", so the sub-resource route still resolves."""
        self.assertEqual(self.client.get(self.url(), **self.headers()).status_code, 200)

    def test_requires_pins_scope_not_wiki_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])
        self.assertEqual(self.client.get(self.url(), **self.headers()).status_code, 403)


class PinReviewApiTests(TestCase):
    """GET/PUT/DELETE the caller's own rating for their pin."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Review client")
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def url(self) -> str:
        return f"{BASE}/{self.pin.slug or self.pin.uuid}/review/"

    def test_unrated_pin_is_a_404(self) -> None:
        response = self.client.get(self.url(), **self.headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})

    def test_put_creates_then_updates(self) -> None:
        created = self.client.put(self.url(), {"rating": 4}, content_type="application/json", **self.headers())
        self.assertEqual(created.status_code, 201)

        updated = self.client.put(self.url(), {"rating": 2}, content_type="application/json", **self.headers())
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get(self.url(), **self.headers()).json()["rating"], 2)

        # Upsert, not insert: exactly one row per (profile, pin).
        self.assertEqual(Review.objects.filter(profile=self.profile, pin=self.pin).count(), 1)

    def test_out_of_range_rating_is_rejected(self) -> None:
        response = self.client.put(self.url(), {"rating": 9}, content_type="application/json", **self.headers())
        self.assertEqual(response.status_code, 400)

    def test_delete_clears_the_rating(self) -> None:
        self.client.put(self.url(), {"rating": 3}, content_type="application/json", **self.headers())
        self.assertEqual(self.client.delete(self.url(), **self.headers()).status_code, 204)
        self.assertEqual(self.client.get(self.url(), **self.headers()).status_code, 404)

    def test_deleting_a_nonexistent_rating_is_a_404(self) -> None:
        self.assertEqual(self.client.delete(self.url(), **self.headers()).status_code, 404)


class VisibleCommentTreePropertyTests(TestCase):
    """Properties that must hold for the shared visibility gate."""

    def setUp(self) -> None:
        baker.make(User)
        self.viewer = Profile.objects.get(user=baker.make(User))
        # comment_visibility defaults to ANYTHING_IN_COMMON, which correctly
        # hides a stranger's comments (gate 1). These properties are about the
        # mention gate, so the author opts into ANYONE to isolate it.
        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()
        location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.viewer, location=location)

    def _tree(self):
        """Run the gate over this wiki's comments as the viewer."""
        return visible_comment_tree(list(top_level_comment_queryset(self.wiki.comments.all())), self.viewer)

    @settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(mentioned_count=st.integers(min_value=1, max_value=3), plain_count=st.integers(min_value=0, max_value=3))
    def test_comments_mentioning_unpinned_locations_are_always_dropped(
        self, mentioned_count: int, plain_count: int
    ) -> None:
        """However many there are, none of them survives the gate."""
        self.wiki.comments.all().delete()

        for _ in range(mentioned_count):
            secret = baker.make("dashboard.Location")
            baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"see @[X](loc:{secret.uuid})")
        for index in range(plain_count):
            baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"plain {index}")

        visible = self._tree()
        self.assertEqual(len(visible), plain_count)
        for item in visible:
            self.assertNotIn("loc:", item.comment.text)

    @settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(count=st.integers(min_value=0, max_value=4))
    def test_plain_comments_always_survive(self, count: int) -> None:
        """The gate is not over-eager - text with no mentions is never dropped."""
        self.wiki.comments.all().delete()
        for index in range(count):
            baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"plain {index}")

        self.assertEqual(len(self._tree()), count)

    def test_mention_of_a_pinned_location_survives(self) -> None:
        """The gate keys on what the viewer has pinned, not on mentions per se."""
        self.wiki.comments.all().delete()
        known = baker.make("dashboard.Location")
        baker.make("dashboard.Pin", profile=self.viewer, location=known)
        baker.make("dashboard.Comment", wiki=self.wiki, profile=self.author, text=f"see @[Known](loc:{known.uuid})")

        self.assertEqual(len(self._tree()), 1)
