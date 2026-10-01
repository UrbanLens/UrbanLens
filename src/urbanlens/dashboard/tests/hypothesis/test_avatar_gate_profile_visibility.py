"""The media gate serves an avatar only to viewers the profile is visible to (G2-23)."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from django.contrib.auth.models import User
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.media.access import authorize_avatar


class AvatarGateProfileVisibilityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp(prefix="ul_avatar_gate_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        (Path(self._media_root) / "avatars").mkdir(parents=True)
        (Path(self._media_root) / "avatars" / "private-face.webp").write_bytes(b"avatar-bytes")

        baker.make(User)
        self.owner = baker.make(User).profile
        Profile.objects.filter(pk=self.owner.pk).update(
            avatar="avatars/private-face.webp", profile_visibility=VisibilityChoice.FRIENDS
        )
        self.owner.refresh_from_db()
        self.stranger = baker.make(User).profile
        self.friend = baker.make(User).profile
        Friendship.objects.create(
            from_profile=self.owner,
            to_profile=self.friend,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )

    def _fetch(self, viewer: Profile) -> int:
        self.client.force_login(viewer.user)
        response = self.client.get("/media/avatars/private-face.webp")
        if getattr(response, "streaming", False):
            b"".join(response.streaming_content)
            handle = getattr(response, "file_to_stream", None)
            if handle is not None:
                handle.close()
        return response.status_code

    def test_a_viewer_the_profile_is_hidden_from_cannot_fetch_its_avatar(self) -> None:
        """The URL leaked once (a cached page, a shared link) is no way around the masking."""
        self.assertFalse(self.owner.can_view_profile(self.stranger))

        self.assertEqual(self._fetch(self.stranger), 404)

    def test_a_viewer_the_profile_is_visible_to_can(self) -> None:
        self.assertEqual(self._fetch(self.friend), 200)

    def test_the_owner_always_can(self) -> None:
        Profile.objects.filter(pk=self.owner.pk).update(profile_visibility=VisibilityChoice.NO_ONE)

        self.assertEqual(self._fetch(self.owner), 200)

    def test_a_public_profiles_avatar_is_served_to_anyone_signed_in(self) -> None:
        Profile.objects.filter(pk=self.owner.pk).update(profile_visibility=VisibilityChoice.ANYONE)

        self.assertEqual(self._fetch(self.stranger), 200)

    def test_an_avatar_no_profile_uses_is_refused(self) -> None:
        """An orphan cannot be attributed, so it cannot be judged visible."""
        (Path(self._media_root) / "avatars" / "orphan.webp").write_bytes(b"x")

        self.assertFalse(authorize_avatar(self.friend, "avatars/orphan.webp"))

    def test_a_generated_emoji_avatar_is_served_to_anyone(self) -> None:
        """Emoji avatars carry nothing personal and may be shared by many profiles."""
        self.assertTrue(authorize_avatar(self.stranger, "avatars/emoji_128512.svg"))

    def test_the_lookup_is_one_indexed_query_not_a_scan(self) -> None:
        with CaptureQueriesContext(connection) as queries:
            authorize_avatar(self.stranger, "avatars/private-face.webp")
        lookup = next(
            q["sql"] for q in queries.captured_queries if "dashboard_profiles" in q["sql"] and '"avatar"' in q["sql"]
        )
        with connection.cursor() as cursor:
            cursor.execute("SET enable_seqscan = off")
            cursor.execute(f"EXPLAIN {lookup}")  # nosec B608 - SQL captured from the ORM in this test
            plan = " ".join(row[0] for row in cursor.fetchall())
            cursor.execute("SET enable_seqscan = on")
        self.assertIn("idxdb_profile_avatar", plan)
