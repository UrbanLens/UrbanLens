"""The fabricated half of a demo account: friends, messages, trips, visits, lists.

Everything here is invented, unlike the real places in :mod:`.locations` - see
that module and ``docs/DEMO.md`` for why coordinates are never fabricated. This
is the opposite case: a friendship, a chat, a trip are not claims about the
world the way a pin is, so there is nothing they could get wrong by not being
real. Fabricating them is what makes the account look inhabited rather than
empty.

**Silent by construction, not by patching.** Every writer here either goes
straight to the ORM or calls a service function chosen specifically because it
does not notify - `Friendship.objects.create(status=ACCEPTED)` rather than
`request()`/`accept()`, `DirectMessage.objects.create(...)` rather than
`create_direct_message()`, `Comment` via the plain create (the service wrapper
already does not notify; the controller layer does, and is not called here).
Two exceptions actually award something and go through the sanctioned path
regardless: recorded activity (`services.achievements.activity.record_activity`)
and any `NotificationLog` row this module ever needs, which must go through
`NotificationLog.objects.notify()` - enforced for all production code by
`bin/check_notification_choke_point.py`. This module currently writes none.

The seeding caller (`seeding.seed_demo_account`) already holds Celery patched
and the whole write in one atomic block; nothing here manages either on its
own.
"""

from __future__ import annotations

from datetime import timedelta
import itertools
import random
from typing import TYPE_CHECKING, Any

from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip

#: A fixed, non-cryptographic RNG. Demo content only ever needs to look varied,
#: not be unpredictable, and a fixed seed makes one run's output reproducible
#: for debugging - the account's own identity (username, password) already
#: comes from `secrets` where unpredictability actually matters.
_rng = random.Random(20260820)  # noqa: S311 - demo flavour text, not a security use


def _backdate(instance: Any, when: Any) -> None:
    """Set ``created`` (and, for models that carry one, ``visited_at``-like fields) into the past.

    ``created``/``updated`` are ``auto_now_add``/``auto_now`` (see
    ``models.abstract.model``), so they ignore whatever is passed to
    ``objects.create()`` - the only way to backdate one is an ``update()``
    after the fact, which is what this does. Never call ``instance.save()``
    again afterwards, or ``updated`` (and, on some models, ``created`` itself
    if it is re-specified) moves back to now.

    Args:
        instance: A saved model instance with a ``created`` field.
        when: The timestamp to backdate it to.
    """
    type(instance).objects.filter(pk=instance.pk).update(created=when)


def seed_friendships(owner: Profile, personas: list[Profile]) -> None:
    """Accepted friendships: the owner with every persona, and a few among them.

    Goes straight to ``Friendship.objects.create(status=ACCEPTED)`` rather than
    ``request()``/``accept()`` - those exist to run the request flow a real
    user would; skipping straight to the end state is what a seeder wants, and
    neither one notifies on its own (notifications are the service layer's
    job, not the model's - see ``services.social.friendship``).

    Args:
        owner: The login account's profile.
        personas: The other seeded profiles.
    """
    from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
    from urbanlens.dashboard.models.friendship.model import Friendship

    def _befriend(a: Profile, b: Profile, *, days_ago: int) -> None:
        friendship = Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
        _backdate(friendship, timezone.now() - timedelta(days=days_ago))

    for offset, persona in enumerate(personas):
        _befriend(owner, persona, days_ago=30 + offset * 7)
    # A couple of edges among the personas themselves, so the owner's friend
    # list is not a star graph with nobody knowing anybody else.
    for a, b in itertools.pairwise(personas):
        _befriend(a, b, days_ago=45)


def seed_wiki_comments(personas_by_pin: dict[Profile, list[Pin]]) -> None:
    """A few comments on the wikis of pooled locations, where more than one profile can see them.

    Only fires on a location more than one seeded profile actually holds a pin
    on - the owner holds every pooled location, so that is any location a
    persona was also given, which is enough for a real (if one-sided)
    conversation to exist under the owner's own view of the wiki.

    Args:
        personas_by_pin: Each seeded profile mapped to the pins just created
            for it, in pool order - used to find the locations two profiles
            share.
    """
    from urbanlens.dashboard.services.comments.comments import create_comment

    owner_locations = {pin.location_id: pin.location for pin in next(iter(personas_by_pin.values()), [])}
    for profile, pins in list(personas_by_pin.items())[1:]:
        for pin in pins[:2]:
            wiki = getattr(pin.location, "wiki", None)
            if wiki is None or pin.location_id not in owner_locations:
                continue
            opener = create_comment(profile=profile, wiki=wiki, text=_rng.choice(_WIKI_COMMENT_OPENERS))
            _backdate(opener, timezone.now() - timedelta(days=_rng.randint(1, 20)))
            if _rng.random() < 0.5:
                reply = create_comment(profile=next(iter(personas_by_pin)), wiki=wiki, text=_rng.choice(_WIKI_COMMENT_REPLIES), parent=opener)
                _backdate(reply, timezone.now() - timedelta(days=_rng.randint(0, 5)))


_WIKI_COMMENT_OPENERS = [
    "Got out to this one last spring - the south stair is still solid if anyone's planning a visit.",
    "Anyone know the current access situation here? Heard it changed recently.",
    "The light in the main hall around golden hour is worth the trip on its own.",
    "Watch the floor on the second level, a couple of boards gave way when I was through.",
]
_WIKI_COMMENT_REPLIES = [
    "Good to know, thanks for the update.",
    "Same experience when I went - still holding up.",
    "Appreciate the heads up, will be careful there.",
]


def seed_direct_messages(owner: Profile, personas: list[Profile]) -> None:
    """A short plaintext exchange between the owner and each persona, and one among personas.

    Plain ``DirectMessage.objects.create`` rather than
    ``create_direct_message()`` - the service function is what a real send goes
    through (permission checks, notifications, scheduled email/text alerts,
    address detection), all of which a seeder wants none of. Plaintext because
    the demo account has no browser-side E2EE key to encrypt with; the body/
    ciphertext constraint only forbids both being set, so leaving
    ``ciphertext``/``nonce`` empty and ``key_version=0`` satisfies it exactly.

    Args:
        owner: The login account's profile.
        personas: The other seeded profiles.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage

    def _exchange(a: Profile, b: Profile, lines: list[tuple[Profile, str]], *, start_days_ago: int) -> None:
        for offset, (sender, text) in enumerate(lines):
            message = DirectMessage.objects.create(sender=sender, recipient=b if sender is a else a, body=text, sender_delete_after=sender.direct_message_delete_after)
            _backdate(message, timezone.now() - timedelta(days=start_days_ago, hours=-offset))

    for offset, persona in enumerate(personas):
        _exchange(
            owner,
            persona,
            [(owner, "Hey - any recommendations for somewhere new to check out this weekend?"), (persona, "Depends how far you want to drive, but I've got a couple in mind.")],
            start_days_ago=3 + offset,
        )
    if len(personas) >= 2:
        _exchange(personas[0], personas[1], [(personas[0], "You still up for that trip we talked about?"), (personas[1], "Yeah, count me in.")], start_days_ago=2)


def seed_group_chat(owner: Profile, personas: list[Profile]) -> None:
    """One group chat, memberships created strictly before the messages they should see.

    Ordering is load-bearing: ``GroupChatMembership.created`` is the floor
    ``visible_window`` uses to decide which messages a member can see (see
    ``models.group_chats.queryset``), so a message backdated earlier than its
    sender's own membership would be invisible to everyone else in the group -
    including, confusingly, its own sender.

    Args:
        owner: The login account's profile, and the chat's creator.
        personas: The other seeded profiles, all invited.
    """
    from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage

    if not personas:
        return

    group = GroupChat.objects.create(name="Field trip planning", creator=owner)
    membership_time = timezone.now() - timedelta(days=10)
    for profile in [owner, *personas]:
        membership = GroupChatMembership.objects.create(group=group, profile=profile)
        _backdate(membership, membership_time)

    lines = [
        (owner, "Starting this so we can plan the next trip somewhere other than everyone's individual DMs."),
        (personas[0], "Works for me. Where are we thinking?"),
        (personas[-1], "I'll pull together a short list this week."),
    ]
    for offset, (sender, text) in enumerate(lines):
        message = GroupMessage.objects.create(group=group, sender=sender, body=text)
        _backdate(message, membership_time + timedelta(hours=offset + 1))


def seed_visits(profile: Profile, pins: list[Pin]) -> None:
    """A short visit history on a handful of the profile's pins.

    ``create_manual_visit`` rather than ``PinVisit.objects.create`` directly:
    it also runs ``sync_last_visited`` and ``add_visited_status`` (the
    "Visited" label), which are exactly the derived state a real visit would
    produce and a raw insert would silently skip. Every seeded profile has
    ``track_pin_visits`` at its default of True, so the service's own gate
    never refuses.

    Args:
        profile: Pin owner.
        pins: The profile's own pins to log visits against.
    """
    from urbanlens.dashboard.services.visits.visits import create_manual_visit

    for pin in pins[:6]:
        for _ in range(_rng.randint(1, 2)):
            create_manual_visit(pin, visited_at=timezone.now() - timedelta(days=_rng.randint(5, 400)), notes=_rng.choice(_VISIT_NOTES) if _rng.random() < 0.6 else None)


_VISIT_NOTES = [
    "Quick visit, didn't linger - looked like someone else had been through recently.",
    "Spent a couple of hours here, worth the trip.",
    "Overcast, so the light wasn't great for photos - might go back.",
    None,
]


def seed_trip(owner: Profile, personas: list[Profile], pool: list[Location]) -> Trip | None:
    """One trip, with activities on pooled locations, a member and an RSVP.

    Args:
        owner: The trip's creator.
        personas: Candidates for trip membership.
        pool: Pooled locations to build activities against; a trip with no
            locations to visit is not built.

    Returns:
        The created trip, or None when there is no location pool yet.
    """
    from urbanlens.dashboard.services.trips.trip_activities import create_activity
    from urbanlens.dashboard.services.trips.trip_crud import create_trip
    from urbanlens.dashboard.services.trips.trip_membership import set_trip_rsvp

    if not pool:
        return None

    trip, _created = create_trip(owner, name="Fall exploring weekend", description="A weekend circuit - see what's actually feasible to hit in two days.")
    for location in pool[:3]:
        create_activity(trip, owner, title=location.official_name or "Stop", place={"location_uuid": str(location.uuid)})

    if personas:
        from urbanlens.dashboard.models.trips.model import TripMembership

        membership = TripMembership.objects.create(trip=trip, profile=personas[0], status=TripMembership.STATUS_JOINED)
        _backdate(membership, timezone.now() - timedelta(days=5))
        set_trip_rsvp(trip, personas[0], "yes")
    return trip


def seed_pin_lists(owner: Profile, pins: list[Pin]) -> None:
    """A couple of ordinary lists over the owner's own pins.

    Args:
        owner: List owner.
        pins: Pins to distribute across the lists.
    """
    from urbanlens.dashboard.models.pin_list.model import PinList
    from urbanlens.dashboard.services.pins.pin_list_membership import add_pins_to_list

    if not pins:
        return

    want_to_visit = PinList.objects.create(profile=owner, name="Want to visit", description="Places on the list, not yet been.")
    add_pins_to_list(want_to_visit, pins[: max(1, len(pins) // 2)])

    if len(pins) > 2:
        favorites = PinList.objects.create(profile=owner, name="Favorites", description="The ones worth going back to.")
        add_pins_to_list(favorites, pins[len(pins) // 2 :])


#: Generated, not photographed - flat colours with a caption drawn on, so the
#: gallery UI has real image files to render (thumbnailing, lightbox, EXIF
#: panel gracefully showing nothing) without depending on any external host
#: staying up, or on a network call this instance may not be allowed to make.
_PHOTO_PALETTE: list[tuple[int, int, int]] = [(74, 62, 54), (58, 74, 66), (70, 60, 74), (76, 70, 52), (54, 62, 74)]


def _placeholder_photo(caption: str, color: tuple[int, int, int]) -> Any:
    """A small synthetic JPEG, in memory - never touches the network.

    Args:
        caption: Text drawn onto the image.
        color: Background RGB.

    Returns:
        A ``django.core.files.base.ContentFile`` ready to assign to an
        ``ImageField``.
    """
    from io import BytesIO

    from django.core.files.base import ContentFile
    from PIL import Image as PILImage, ImageDraw

    canvas = PILImage.new("RGB", (640, 480), color)
    ImageDraw.Draw(canvas).text((24, 24), caption, fill=(235, 235, 235))
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=70)
    return ContentFile(buffer.getvalue(), name=f"{_rng.randint(10**7, 10**8 - 1)}.jpg")


def seed_photos(pins_by_profile: dict[Profile, list[Pin]]) -> None:
    """A couple of generated photos per profile, attached to their own pins.

    ``source=ImageSource.UPLOAD`` (the field default) is correct here, not a
    fileless-row workaround: a real file is written to local storage, so this
    is what a genuine upload looks like, and it is also what
    ``achievements.signals._is_genuine_upload`` requires for the photo streak
    - a nice side effect, since it means that surface has something real to
    show too.

    Args:
        pins_by_profile: Each seeded profile mapped to its own pins.
    """
    from urbanlens.dashboard.models.images.model import Image

    for profile, pins in pins_by_profile.items():
        for index, pin in enumerate(pins[:2]):
            color = _PHOTO_PALETTE[index % len(_PHOTO_PALETTE)]
            photo = Image.objects.create(
                pin=pin,
                location=pin.location,
                profile=profile,
                image=_placeholder_photo(pin.location.official_name or "UrbanLens", color),
                taken_at=timezone.now() - timedelta(days=_rng.randint(3, 200)),
            )
            _backdate(photo, photo.taken_at)


def seed_achievements_and_activity(profiles: list[Profile]) -> None:
    """Award whatever Achievement definitions already exist, and backfill an activity streak.

    Achievement *definitions* are global (no profile FK - see
    ``models.achievements.model``), so this never creates one: doing so would
    appear on every real user's achievements page, and saving an active one
    enqueues a backfill sweep across every profile in the database. Only
    existing definitions are awarded against.

    Args:
        profiles: Every seeded profile - owner and personas.
    """
    from urbanlens.dashboard.models.achievements.meta import ActivityKind
    from urbanlens.dashboard.models.achievements.model import Achievement, ProfileActivityDay, UserAchievement
    from urbanlens.dashboard.services.achievements.activity import rebuild_streak

    definitions = list(Achievement.objects.filter(is_active=True)[:3])
    for profile in profiles:
        for achievement in definitions:
            award, _created = UserAchievement.objects.get_or_create(profile=profile, achievement=achievement, defaults={"value_at_award": achievement.threshold})
            _backdate(award, timezone.now() - timedelta(days=_rng.randint(1, 60)))

        # A short streak leading up to today, so the profile page's activity
        # graph shows something rather than a blank grid.
        today = timezone.now().date()
        for days_back in range(_rng.randint(2, 5)):
            ProfileActivityDay.objects.get_or_create(profile=profile, kind=ActivityKind.LOGIN, day=today - timedelta(days=days_back))
        rebuild_streak(profile, ActivityKind.LOGIN)
