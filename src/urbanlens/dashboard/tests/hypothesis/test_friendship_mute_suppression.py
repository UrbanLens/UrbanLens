"""Muting a friend actually silences them.

For as long as the flag existed it suppressed nothing. The profile page's Mute
button and the external API's ``PATCH /friends/{uuid}/mute/`` both recorded the
preference faithfully, and no delivery path read it: the muter kept receiving
friend-request, pin-share, trip-invite and comment notifications, each with an
unread row in the bell.

That was structural rather than an oversight. Around thirty places create a
``NotificationLog``, so honouring the preference meant remembering it thirty
times, and a notification type added later could not inherit a rule that lived
nowhere. ``NotificationLog.objects.notify()`` is now the one place, and
``bin/check_notification_choke_point.py`` fails the build for a production call
site that goes around it.

These tests pin what the preference does and, as importantly, what it must not
do: it is one person's volume control on another person's *social* activity,
not an opt-out from being told that somebody has not come back from a site.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import MUTE_EXEMPT_TYPES, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social.friendship import notifications_muted, profiles_muting


def _profile() -> Profile:
    """Create a profile via its auto-created user."""
    return baker.make(User).profile


def _friendship(from_profile: Profile, to_profile: Profile) -> Friendship:
    """One accepted relationship row, created directly."""
    return Friendship.objects.create(
        from_profile=from_profile,
        to_profile=to_profile,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class NotificationsMutedTests(TestCase):
    """The helper that turns the stored preference into a yes/no."""

    def setUp(self) -> None:
        super().setUp()
        self.muter = _profile()
        self.other = _profile()
        self.friendship = _friendship(self.muter, self.other)

    def test_an_unmuted_relationship_is_not_muted(self) -> None:
        self.assertFalse(notifications_muted(self.muter, self.other))

    def test_the_muters_own_notifications_are_suppressed(self) -> None:
        self.friendship.mute(self.muter)

        self.assertTrue(notifications_muted(self.muter, self.other))

    def test_the_other_party_keeps_hearing_from_the_muter(self) -> None:
        """The reason this could not be wired up before the column was split."""
        self.friendship.mute(self.muter)

        self.assertFalse(notifications_muted(self.other, self.muter))

    def test_strangers_have_nothing_to_consult(self) -> None:
        """Mute needs an existing relationship, so no row means not muted."""
        self.assertFalse(notifications_muted(self.muter, _profile()))

    def test_a_missing_end_is_not_muted(self) -> None:
        """System notifications name no source; they are nobody's to silence."""
        self.assertFalse(notifications_muted(self.muter, None))
        self.assertFalse(notifications_muted(None, self.other))

    def test_a_notification_about_yourself_is_not_muted(self) -> None:
        self.assertFalse(notifications_muted(self.muter, self.muter))

    def test_pks_answer_the_same_as_instances(self) -> None:
        """Producers hold whichever is cheaper; both must reach the same row."""
        self.friendship.mute(self.muter)

        self.assertTrue(notifications_muted(self.muter.pk, self.other.pk))

    def test_mute_survives_the_relationship_being_removed(self) -> None:
        """``remove()`` keeps the row, so the preference outlives the friendship.

        Worth stating rather than discovering: someone who muted a person and
        then un-friended them stays un-notified if the pair reconnect, because
        ``request()`` reuses the row. Unmuting is one click, and the opposite
        default - silently restoring a person you muted - is the worse
        surprise.
        """
        self.friendship.mute(self.muter)
        self.friendship.remove()

        self.assertTrue(notifications_muted(self.muter, self.other))


class ProfilesMutingTests(TestCase):
    """The batch form, which has to reach the same answer as the per-row one.

    It exists for ``_notify_group_message``, which resolves every per-member
    fact up front so a 50-member group does not pay a lookup per member on the
    synchronous send path. A batch query that disagreed with
    :func:`notifications_muted` would silence the wrong people only in group
    chats, which is exactly the kind of divergence nobody notices.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.quiet = _profile()
        self.loud = _profile()
        _friendship(self.quiet, self.sender).mute(self.quiet)
        _friendship(self.sender, self.loud)

    def test_only_the_muting_member_is_returned(self) -> None:
        muted = profiles_muting(self.sender, [self.quiet.pk, self.loud.pk])

        self.assertEqual(muted.profile_ids, {self.quiet.pk})
        self.assertEqual(muted.source_id, self.sender.pk, "the answer must say who it is about")

    def test_it_reads_both_ends_of_the_row(self) -> None:
        """Which side a member is on is an accident of who requested first."""
        far_side = _profile()
        _friendship(self.sender, far_side).mute(far_side)

        self.assertEqual(profiles_muting(self.sender, [far_side.pk]).profile_ids, {far_side.pk})

    def test_the_senders_own_mute_does_not_silence_the_others(self) -> None:
        """The direction that was indistinguishable before the column was split."""
        Friendship.objects.all().between(self.sender, self.loud).mute(self.sender)

        self.assertEqual(profiles_muting(self.sender, [self.loud.pk]).profile_ids, frozenset())

    def test_it_agrees_with_the_per_row_helper(self) -> None:
        recipients = [self.quiet, self.loud, _profile()]
        batched = profiles_muting(self.sender, [profile.pk for profile in recipients]).profile_ids

        for profile in recipients:
            with self.subTest(profile=profile.pk):
                self.assertEqual(profile.pk in batched, notifications_muted(profile, self.sender))

    def test_the_sender_is_never_in_their_own_result(self) -> None:
        self.assertEqual(profiles_muting(self.sender, [self.sender.pk]).profile_ids, frozenset())

    def test_an_empty_membership_asks_nothing(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(profiles_muting(self.sender, []).profile_ids, frozenset())

        self.assertEqual(len(queries.captured_queries), 0)

    def test_one_query_however_many_recipients(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        many = [_profile().pk for _ in range(10)]
        with CaptureQueriesContext(connection) as queries:
            profiles_muting(self.sender, many)

        self.assertEqual(len(queries.captured_queries), 1)


def _notify(recipient: Profile, source: Profile | None, notification_type: str = NotificationType.PIN_SHARED) -> NotificationLog | None:
    """Raise one notification through the sanctioned entry point."""
    return NotificationLog.objects.notify(
        profile=recipient,
        source_profile=source,
        status=Status.UNREAD,
        importance=Importance.MEDIUM,
        notification_type=notification_type,
        title="Pin shared with you",
        message="Someone shared a pin with you.",
    )


class NotifyChokePointTests(TestCase):
    """``NotificationLog.objects.notify`` is where the preference is applied."""

    def setUp(self) -> None:
        super().setUp()
        self.muter = _profile()
        self.other = _profile()
        self.friendship = _friendship(self.muter, self.other)

    def test_an_unmuted_source_still_gets_through(self) -> None:
        notification = _notify(self.muter, self.other)

        self.assertIsNotNone(notification)
        self.assertEqual(NotificationLog.objects.for_profile(self.muter).count(), 1)

    def test_a_muted_source_writes_no_row_at_all(self) -> None:
        """Not "writes it read" - the row is what every delivery channel hangs off.

        The live WebSocket toast, the WhatsApp/SMS alert and the native push
        are all ``post_save`` receivers on ``NotificationLog``, so declining to
        write is what produces actual silence rather than a quieter bell.
        """
        self.friendship.mute(self.muter)

        self.assertIsNone(_notify(self.muter, self.other))
        self.assertEqual(NotificationLog.objects.for_profile(self.muter).count(), 0)

    def test_muting_does_not_silence_the_muter_themselves(self) -> None:
        self.friendship.mute(self.muter)

        self.assertIsNotNone(_notify(self.other, self.muter))
        self.assertEqual(NotificationLog.objects.for_profile(self.other).count(), 1)

    def test_a_notification_with_no_source_is_unaffected(self) -> None:
        self.friendship.mute(self.muter)

        self.assertIsNotNone(_notify(self.muter, None, NotificationType.PIN_IMPORT_COMPLETE))

    def test_safety_check_ins_are_never_suppressed(self) -> None:
        """A preference about someone's chatter is not consent to stop watching."""
        self.friendship.mute(self.muter)

        for notification_type in sorted(MUTE_EXEMPT_TYPES):
            with self.subTest(notification_type=notification_type):
                self.assertIsNotNone(_notify(self.muter, self.other, notification_type))

    def test_every_other_type_is_suppressed(self) -> None:
        """Stated as a sweep so a new notification type inherits the rule."""
        self.friendship.mute(self.muter)

        for notification_type in NotificationType.values:
            if notification_type in MUTE_EXEMPT_TYPES:
                continue
            with self.subTest(notification_type=notification_type):
                self.assertIsNone(_notify(self.muter, self.other, notification_type))


class ReciprocalRowsTests(TestCase):
    """Two rows can join one pair, and neither mute path may fall over on it.

    ``unique_together`` is on ``(from_profile, to_profile)``, so ``A->B`` and
    ``B->A`` can both exist - a profile import that restores both directions
    produces exactly that, and ``test_calendar_sync`` builds one on purpose.
    ``between()`` used to ``.get()``, which meant a reciprocal pair raised
    ``MultipleObjectsReturned``; once mute was consulted on every notification,
    that would have been a 500 on every message between them.
    """

    def setUp(self) -> None:
        super().setUp()
        self.recipient = _profile()
        self.sender = _profile()
        self.forward = _friendship(self.sender, self.recipient)
        self.backward = _friendship(self.recipient, self.sender)

    def test_between_answers_with_the_older_row_instead_of_raising(self) -> None:
        self.assertEqual(Friendship.objects.all().between(self.sender, self.recipient), self.forward)

    def test_a_mute_on_either_row_is_honoured(self) -> None:
        """Whichever row the recipient's own button happened to write."""
        for row in (self.forward, self.backward):
            with self.subTest(row=row.pk):
                Friendship.objects.filter(pk__in=[self.forward.pk, self.backward.pk]).update(muted_by_from_profile=False, muted_by_to_profile=False)
                row.refresh_from_db()
                row.mute(self.recipient)

                self.assertTrue(notifications_muted(self.recipient, self.sender))
                self.assertEqual(profiles_muting(self.sender, [self.recipient.pk]).profile_ids, {self.recipient.pk})

    def test_an_unmuted_reciprocal_pair_is_not_muted(self) -> None:
        self.assertFalse(notifications_muted(self.recipient, self.sender))
        self.assertEqual(profiles_muting(self.sender, [self.recipient.pk]).profile_ids, frozenset())

    def test_notifying_across_a_reciprocal_pair_does_not_raise(self) -> None:
        self.assertIsNotNone(_notify(self.recipient, self.sender))


class MuteSurvivesOtherWritesTests(TestCase):
    """A mute is a preference somebody set; nothing else may quietly undo it.

    The mute columns are written by a targeted ``UPDATE`` that leaves the
    in-memory instance untouched, so any other write of the *whole* row from a
    stale instance overwrites them - and nothing would report it. Two shapes of
    that were live before 2026-08-20: every status transition did a bare
    ``save()``, and ``block_profile`` additionally swaps ``from_profile`` and
    ``to_profile``, which relabels which column belongs to whom.
    """

    def setUp(self) -> None:
        super().setUp()
        self.actor = _profile()
        self.other = _profile()
        self.friendship = _friendship(self.actor, self.other)

    def _stale(self) -> Friendship:
        """A copy loaded before the mute, as a request in another tab would hold."""
        return Friendship.objects.get(pk=self.friendship.pk)

    def test_a_status_transition_does_not_clobber_a_concurrent_mute(self) -> None:
        """Open a profile page, they mute you in another tab, you click Remove."""
        stale = self._stale()
        Friendship.objects.all().between(self.actor, self.other).mute(self.other)

        stale.remove()

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, FriendshipStatus.REMOVED)
        self.assertTrue(self.friendship.is_muted_by(self.other), "their mute was overwritten by an unrelated write")

    def test_every_transition_leaves_the_mute_columns_alone(self) -> None:
        for transition in ("accept", "decline", "ignore", "remove"):
            with self.subTest(transition=transition):
                Friendship.objects.filter(pk=self.friendship.pk).update(status=FriendshipStatus.REQUESTED, muted_by_from_profile=False, muted_by_to_profile=False)
                stale = self._stale()
                Friendship.objects.all().between(self.actor, self.other).mute(self.other)

                getattr(stale, transition)()

                self.friendship.refresh_from_db()
                self.assertTrue(self.friendship.is_muted_by(self.other))

    def test_a_request_does_not_clobber_a_mute(self) -> None:
        """Re-requesting after a removal is the ordinary way back to a friendship.

        Passes even against the bare-``save()`` version, because ``request``
        loads the row itself and so is never stale - kept because the property
        is worth holding, not because it catches that bug.
        """
        self.friendship.remove()
        Friendship.objects.all().between(self.actor, self.other).mute(self.other)

        Friendship.request(from_profile=self.actor, to_profile=self.other.pk)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.status, FriendshipStatus.REQUESTED)
        self.assertTrue(self.friendship.is_muted_by(self.other))

    def test_accept_still_reaches_the_achievement_signal(self) -> None:
        """Why this is `update_fields` and not `queryset.update()`.

        `queryset.update()` would sidestep the lost update outright, and also
        skip `post_save` - which the achievements system subscribes to for this
        model precisely to see a friendship *reach* ACCEPTED.
        """
        from django.db.models.signals import post_save

        seen: list[tuple[bool, frozenset[str] | None]] = []

        def _record(sender, instance, created, **kwargs) -> None:
            if instance.pk == self.friendship.pk:
                seen.append((created, kwargs.get("update_fields")))

        post_save.connect(_record, sender=Friendship, dispatch_uid="test_friendship_accept_signal")
        try:
            self.friendship.accept()
        finally:
            post_save.disconnect(sender=Friendship, dispatch_uid="test_friendship_accept_signal")

        self.assertEqual(len(seen), 1)
        created, update_fields = seen[0]
        self.assertFalse(created)
        self.assertIn("status", update_fields or ())

    def test_blocking_swaps_the_mute_columns_with_the_row_ends(self) -> None:
        """`block_profile` re-points the row so the blocker owns `from_profile`.

        The columns are named for the row's *ends*, so swapping the ends
        without swapping them hands each person the other's preference.
        """
        from urbanlens.dashboard.services.social.friendship import block_profile

        # The row was created actor->other; blocking from `other` swaps it.
        Friendship.objects.all().between(self.actor, self.other).mute(self.actor)

        block_profile(self.other, self.actor)

        self.friendship.refresh_from_db()
        self.assertEqual(self.friendship.from_profile_id, self.other.pk, "the blocker must own from_profile")
        self.assertTrue(self.friendship.is_muted_by(self.actor), "the muter's own preference must follow them")
        self.assertFalse(self.friendship.is_muted_by(self.other), "and must not be transferred to the other party")

    def test_blocking_without_a_swap_keeps_each_side_too(self) -> None:
        """The branch where the row already points the right way."""
        from urbanlens.dashboard.services.social.friendship import block_profile

        Friendship.objects.all().between(self.actor, self.other).mute(self.other)

        block_profile(self.actor, self.other)

        self.friendship.refresh_from_db()
        self.assertTrue(self.friendship.is_muted_by(self.other))
        self.assertFalse(self.friendship.is_muted_by(self.actor))


class BatchedMuteIsCheckedAgainstItsOwnSourceTests(TestCase):
    """A batch resolved for one sender must not be applied to another.

    The optimisation hands ``notify`` an answer it cannot re-derive, so the
    answer says who it is about. Without that, reusing a batch across senders
    would suppress the wrong notifications - silently, and only on the paths
    that batch, which is the hardest kind of divergence to notice.
    """

    def setUp(self) -> None:
        super().setUp()
        self.recipient = _profile()
        self.muted_sender = _profile()
        self.other_sender = _profile()
        _friendship(self.recipient, self.muted_sender).mute(self.recipient)
        _friendship(self.recipient, self.other_sender)

    def _notify_from(self, sender: Profile, batch) -> NotificationLog | None:
        return NotificationLog.objects.notify(
            muted_recipients=batch,
            profile=self.recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
        )

    def test_the_matching_batch_is_used(self) -> None:
        batch = profiles_muting(self.muted_sender, [self.recipient.pk])

        self.assertIsNone(self._notify_from(self.muted_sender, batch))

    def test_a_batch_for_another_sender_is_ignored_not_trusted(self) -> None:
        """It would have suppressed a notification nobody muted."""
        wrong_batch = profiles_muting(self.muted_sender, [self.recipient.pk])

        self.assertIsNotNone(self._notify_from(self.other_sender, wrong_batch))

    def test_the_fallback_still_reaches_the_right_answer(self) -> None:
        """Ignoring a mismatched batch means re-asking, not assuming unmuted."""
        wrong_batch = profiles_muting(self.other_sender, [self.recipient.pk])

        self.assertIsNone(self._notify_from(self.muted_sender, wrong_batch))


class NotifyFieldSpellingTests(TestCase):
    """``profile_id=`` must reach the same preference as ``profile=``.

    Both are legitimate ways to write the row, and a check that read only the
    instance form would make the preference depend on how a producer happened
    to hold its profiles - a hole no reviewer would see, because the call looks
    identical.
    """

    def setUp(self) -> None:
        super().setUp()
        self.muter = _profile()
        self.other = _profile()
        _friendship(self.muter, self.other).mute(self.muter)

    def test_pk_kwargs_are_suppressed_too(self) -> None:
        suppressed = NotificationLog.objects.notify(
            profile_id=self.muter.pk,
            source_profile_id=self.other.pk,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
        )

        self.assertIsNone(suppressed)
        self.assertEqual(NotificationLog.objects.for_profile(self.muter).count(), 0)


class ExemptTypeTests(SimpleTestCase):
    """The exemption list is a decision, and must stay one."""

    def test_the_whole_safety_family_is_exempt(self) -> None:
        """Derived here, listed there: a new safety type fails until considered."""
        safety_types = {value for value in NotificationType.values if value.startswith("safety_ci_")}
        safety_types.add(NotificationType.WIKI_SAFETY_CHECKIN)

        self.assertEqual(safety_types - set(MUTE_EXEMPT_TYPES), set())

    def test_nothing_social_slipped_into_the_exemption(self) -> None:
        """The list is for safety, not for whatever seemed important that day."""
        social = {NotificationType.PIN_SHARED, NotificationType.MAP_SHARED, NotificationType.FRIEND_REQUEST, NotificationType.COMMENT_REPLY, NotificationType.MESSAGE, NotificationType.ADDED_TO_TRIP}

        self.assertEqual(social & set(MUTE_EXEMPT_TYPES), set())
