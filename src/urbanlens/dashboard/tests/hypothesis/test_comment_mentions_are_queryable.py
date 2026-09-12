"""The @loc mention gate becomes a queryset filter instead of a text scan.

Gate 3 (see ``services.comments.comments``) drops a comment entirely when it
names a location the viewer has not pinned - the existence of the mention is
itself the leak. It was answered by parsing every candidate's text in Python,
which meant no caller could page a comment list in SQL: the rows a page should
contain were not knowable until every row had been read and parsed.

``CommentLocationMention`` records what a comment names at the moment it is
written, so the same decision is an ``EXISTS`` against the viewer's own pins.

The gate is security-critical, so the filter is held to
``mentions.is_visible_to`` - the function it replaces - over every shape of
mention rather than a few chosen ones. A filter that is merely *cheaper* than
the scan and disagrees with it by one comment is a location disclosure.
"""

from __future__ import annotations

import uuid as uuid_module

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.location_mention import CommentLocationMention
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.notifications.mentions import is_visible_to, viewer_pinned_uuids


def mention(location_uuid) -> str:
    """One @loc token in storage format."""
    return f"@[Somewhere](loc:{location_uuid})"


class CommentMentionRowsTests(TestCase):
    """The rows say what the text says."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.other_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.pin_location)

    def _comment(self, text: str) -> Comment:
        return Comment.objects.create(pin=self.pin, profile=self.profile, text=text)

    def test_a_comment_with_no_mention_records_none(self) -> None:
        comment = self._comment("just a normal comment")
        self.assertEqual(comment.location_mentions.count(), 0)

    def test_each_distinct_mention_is_recorded_once(self) -> None:
        text = f"{mention(self.pin_location.uuid)} and {mention(self.other_location.uuid)} and {mention(self.pin_location.uuid)} again"
        comment = self._comment(text)
        self.assertEqual(
            set(comment.location_mentions.values_list("location_uuid", flat=True)),
            {self.pin_location.uuid, self.other_location.uuid},
        )

    def test_a_mention_of_a_location_that_does_not_exist_is_still_recorded(self) -> None:
        """It has to be: an unresolvable uuid hides the comment from everyone today."""
        ghost = uuid_module.uuid4()
        comment = self._comment(mention(ghost))
        self.assertEqual(list(comment.location_mentions.values_list("location_uuid", flat=True)), [ghost])

    def test_deleting_a_comment_takes_its_mentions(self) -> None:
        comment = self._comment(mention(self.pin_location.uuid))
        comment.delete()
        self.assertFalse(CommentLocationMention.objects.exists())


class MentionFilterAgreesWithTheTextScanTests(TestCase):
    """The SQL gate and the Python gate must name the same comments."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make(User).profile
        self.author = baker.make(User).profile

        self.pinned = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.also_pinned = Location.objects.create(latitude=40.5, longitude=-74.5)
        self.unpinned = Location.objects.create(latitude=41.0, longitude=-73.0)
        baker.make(Pin, profile=self.viewer, location=self.pinned)
        baker.make(Pin, profile=self.viewer, location=self.also_pinned)
        host = baker.make(Pin, profile=self.author, location=self.unpinned)
        self.ghost = uuid_module.uuid4()

        texts = [
            "no mention at all",
            mention(self.pinned.uuid),
            mention(self.unpinned.uuid),
            mention(self.ghost),
            f"{mention(self.pinned.uuid)} {mention(self.also_pinned.uuid)}",
            f"{mention(self.pinned.uuid)} {mention(self.unpinned.uuid)}",
            f"{mention(self.unpinned.uuid)} {mention(self.ghost)}",
            f"before {mention(self.also_pinned.uuid)} after",
        ]
        self.comments = [Comment.objects.create(pin=host, profile=self.author, text=text) for text in texts]

    def test_the_filter_and_the_scan_agree_on_every_comment(self) -> None:
        pinned = viewer_pinned_uuids(self.viewer)
        allowed = set(Comment.objects.mentions_all_visible_to(self.viewer).values_list("pk", flat=True))
        assert_agrees(
            lambda comment: is_visible_to(comment.text, pinned),
            lambda comment: comment.pk in allowed,
            self.comments,
            describe=lambda comment: f"comment {comment.pk}: {comment.text!r}",
            label="mentions_all_visible_to",
        )

    def test_the_battery_covers_both_answers(self) -> None:
        """A filter that hid nothing, or everything, would agree with a one-sided battery."""
        pinned = viewer_pinned_uuids(self.viewer)
        decisions = {is_visible_to(comment.text, pinned) for comment in self.comments}
        self.assertEqual(decisions, {True, False})

    def test_a_viewer_with_no_pins_sees_only_the_unmentioning_comments(self) -> None:
        stranger = baker.make(User).profile
        visible = set(Comment.objects.mentions_all_visible_to(stranger).values_list("text", flat=True))
        self.assertEqual(visible, {"no mention at all"})


class BackfillTests(TestCase):
    """Comments written before the rows existed get them."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def test_the_migration_derives_rows_for_existing_comments(self) -> None:
        from django.apps import apps

        from urbanlens.dashboard.migrations import __name__ as migrations_package

        # bulk_create skips save(), which is what a pre-migration row looks like.
        Comment.objects.bulk_create(
            [
                Comment(pin=self.pin, profile=self.profile, text=f"see {mention(self.location.uuid)}"),
                Comment(pin=self.pin, profile=self.profile, text="nothing here"),
            ],
        )
        self.assertFalse(CommentLocationMention.objects.exists())

        module = __import__(f"{migrations_package}.0037_backfill_comment_location_mentions", fromlist=["backfill"])
        module.backfill(apps, None)

        self.assertEqual(
            list(CommentLocationMention.objects.values_list("location_uuid", flat=True)), [self.location.uuid]
        )
