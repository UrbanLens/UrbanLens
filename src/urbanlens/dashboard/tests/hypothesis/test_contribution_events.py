"""The events core announces when contributions are made, withdrawn, restored or merged away."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.plugins.extensions import ActionHook
from urbanlens.dashboard.services.media.images import detach_image_from_wiki
from urbanlens.dashboard.services.wiki.duplicate_wikis import merge_duplicate_wiki
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit, revert_wiki_edit

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def listening(hook: ActionHook) -> Iterator[list[tuple]]:
    """Record every call a hook receives for the length of the block."""
    calls: list[tuple] = []

    def record(*args: object) -> None:
        calls.append(args)

    hook.connect(record)
    try:
        yield calls
    finally:
        hook.disconnect(record)


class ContributionEventTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)

    def test_claiming_an_invitation_announces_it_once(self) -> None:
        invitation = baker.make(FriendInvitation, inviter=self.profile)

        with listening(points.INVITATION_ACCEPTED) as calls:
            self.assertTrue(invitation.mark_accepted())
            self.assertFalse(invitation.mark_accepted(), "a second claim must not announce again")

        self.assertEqual(calls, [(invitation,)])

    def _wiki_photo(self) -> Image:
        pin = baker.make(Pin, profile=self.profile, location=self.location)
        return baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            pin=pin,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )

    def test_a_contributor_taking_their_photo_off_a_wiki_announces_it(self) -> None:
        image = self._wiki_photo()

        with listening(points.CONTRIBUTION_WITHDRAWN) as calls:
            detach_image_from_wiki(image, withdrawn_by_contributor=True)

        self.assertEqual(calls, [(image,)])

    def test_someone_elses_removal_of_a_photo_announces_nothing(self) -> None:
        image = self._wiki_photo()

        with listening(points.CONTRIBUTION_WITHDRAWN) as calls:
            detach_image_from_wiki(image, withdrawn_by_contributor=False)

        self.assertEqual(calls, [])

    def test_deleting_your_own_wiki_comment_announces_it_before_the_row_goes(self) -> None:
        baker.make(Pin, profile=self.profile, location=self.location)
        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, text="My note")
        self.client.force_login(self.user)
        seen: list[tuple[int | None, bool]] = []

        def record(contribution: Comment) -> None:
            seen.append((contribution.pk, Comment.objects.filter(pk=contribution.pk).exists()))

        points.CONTRIBUTION_WITHDRAWN.connect(record)
        try:
            response = self.client.delete(
                reverse("location.wiki.comment.delete", args=[self.location.slug, comment.pk])
            )
        finally:
            points.CONTRIBUTION_WITHDRAWN.disconnect(record)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [(comment.pk, True)])

    def test_undoing_a_revert_announces_the_restored_edits(self) -> None:
        editor = baker.make(User).profile
        reverter = baker.make(User).profile
        edit = apply_wiki_edit(self.wiki, editor, {"name": "New Name"})
        assert edit is not None
        revert_edit, skipped = revert_wiki_edit(self.wiki, reverter, edit)
        assert revert_edit is not None and not skipped

        with listening(points.WIKI_EDITS_RESTORED) as calls:
            revert_wiki_edit(self.wiki, editor, revert_edit)

        self.assertEqual(calls, [([edit.pk],)])

    def test_merging_a_duplicate_wiki_announces_both_pages(self) -> None:
        absorbed = baker.make(Wiki, location=baker.make(Location))

        with listening(points.WIKIS_MERGED) as calls:
            merge_duplicate_wiki(self.wiki, absorbed)

        self.assertEqual(calls, [(self.wiki, absorbed)])

    def test_a_raising_listener_stops_the_merge(self) -> None:
        absorbed = baker.make(Wiki, location=baker.make(Location))

        def refuse(kept: Wiki, gone: Wiki) -> None:
            raise RuntimeError("no")

        points.WIKIS_MERGED.connect(refuse)
        try:
            with self.assertRaises(RuntimeError):
                merge_duplicate_wiki(self.wiki, absorbed)
        finally:
            points.WIKIS_MERGED.disconnect(refuse)

        self.assertTrue(Wiki.objects.filter(pk=absorbed.pk).exists(), "the merge rolled back")
