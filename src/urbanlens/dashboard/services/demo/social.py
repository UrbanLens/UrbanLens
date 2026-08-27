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


def seed_wiki_comments(personas_by_pin: dict[Profile, list[Pin]]) -> list[Any]:
    """A few comments on the wikis of pooled locations, where more than one profile can see them.

    Only fires on a location more than one seeded profile actually holds a pin
    on - the owner holds every pooled location, so that is any location a
    persona was also given, which is enough for a real (if one-sided)
    conversation to exist under the owner's own view of the wiki.

    Args:
        personas_by_pin: Each seeded profile mapped to the pins just created
            for it, in pool order - used to find the locations two profiles
            share.

    Returns:
        Every created comment (openers and replies), in creation order.
    """
    from urbanlens.dashboard.services.comments.comments import create_comment

    created = []
    owner_locations = {pin.location_id: pin.location for pin in next(iter(personas_by_pin.values()), [])}
    for profile, pins in list(personas_by_pin.items())[1:]:
        for pin in pins[:2]:
            wiki = getattr(pin.location, "wiki", None)
            if wiki is None or pin.location_id not in owner_locations:
                continue
            opener = create_comment(profile=profile, wiki=wiki, text=_rng.choice(_WIKI_COMMENT_OPENERS))
            _backdate(opener, timezone.now() - timedelta(days=_rng.randint(1, 20)))
            created.append(opener)
            if _rng.random() < 0.5:
                reply = create_comment(profile=next(iter(personas_by_pin)), wiki=wiki, text=_rng.choice(_WIKI_COMMENT_REPLIES), parent=opener)
                _backdate(reply, timezone.now() - timedelta(days=_rng.randint(0, 5)))
                created.append(reply)
    return created


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


def seed_direct_messages(owner: Profile, personas: list[Profile]) -> list[Any]:
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

    Returns:
        Every created message, in creation order.
    """
    from urbanlens.dashboard.models.direct_messages.model import DirectMessage

    created: list[DirectMessage] = []

    def _exchange(a: Profile, b: Profile, lines: list[tuple[Profile, str]], *, start_days_ago: int) -> None:
        for offset, (sender, text) in enumerate(lines):
            message = DirectMessage.objects.create(sender=sender, recipient=b if sender is a else a, body=text, sender_delete_after=sender.direct_message_delete_after)
            _backdate(message, timezone.now() - timedelta(days=start_days_ago, hours=-offset))
            created.append(message)

    for offset, persona in enumerate(personas):
        _exchange(
            owner,
            persona,
            [(owner, "Hey - any recommendations for somewhere new to check out this weekend?"), (persona, "Depends how far you want to drive, but I've got a couple in mind.")],
            start_days_ago=3 + offset,
        )
    if len(personas) >= 2:
        _exchange(personas[0], personas[1], [(personas[0], "You still up for that trip we talked about?"), (personas[1], "Yeah, count me in.")], start_days_ago=2)
    return created


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


def seed_visits(profile: Profile, pins: list[Pin], *, on_this_day: bool = False) -> list[Any]:
    """A visit history on most of the profile's pins, spread widely enough to read as real use.

    ``create_manual_visit`` rather than ``PinVisit.objects.create`` directly:
    it also runs ``sync_last_visited`` and ``add_visited_status`` (the
    "Visited" label), which are exactly the derived state a real visit would
    produce and a raw insert would silently skip. Every seeded profile has
    ``track_pin_visits`` at its default of True, so the service's own gate
    never refuses.

    Args:
        profile: Pin owner.
        pins: The profile's own pins to log visits against.
        on_this_day: When True, the first visit is dated exactly one year ago
            today - the only way ``MemoriesOnThisDayView`` (an exact
            month/day match, current year excluded) ever has something to
            show without waiting for a real year to pass.

    Returns:
        The created visits.
    """
    from urbanlens.dashboard.services.visits.visits import create_manual_visit

    visits = []
    for index, pin in enumerate(pins[:10]):
        visit_count = _rng.randint(1, 3)
        for visit_index in range(visit_count):
            if on_this_day and index == 0 and visit_index == 0:
                visited_at = timezone.now().replace(year=timezone.now().year - 1)
            else:
                visited_at = timezone.now() - timedelta(days=_rng.randint(5, 500))
            visits.append(create_manual_visit(pin, visited_at=visited_at, notes=_rng.choice(_VISIT_NOTES) if _rng.random() < 0.6 else None))
    return visits


def mark_unlogged_visits(pins: list[Pin]) -> None:
    """Mark a couple of pins visited with no logged ``PinVisit`` - the Memories "Visits" queue's whole reason to exist.

    Deliberately the opposite of :func:`seed_visits`: ``visited_without_record``
    (the query behind that page) requires ``last_visited`` set *and* zero
    ``PinVisit`` rows, so this must never route through ``create_manual_visit``,
    which creates exactly the record that would disqualify a pin. Mirrors how a
    real one gets into this state - an import, or a status set by hand, with no
    visit ever logged.

    Args:
        pins: Candidate pins; the ones already visited via :func:`seed_visits`
            must not be passed here, or they no longer qualify either.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.visits.visits import add_visited_status

    for pin in pins[:3]:
        Pin.objects.filter(pk=pin.pk).update(last_visited=timezone.now() - timedelta(days=_rng.randint(10, 200)))
        pin.refresh_from_db(fields=["last_visited"])
        add_visited_status(pin)


_VISIT_NOTES = [
    "Quick visit, didn't linger - looked like someone else had been through recently.",
    "Spent a couple of hours here, worth the trip.",
    "Overcast, so the light wasn't great for photos - might go back.",
    None,
]


def seed_routes(profile: Profile) -> list[Any]:
    """A couple of short recorded routes, so the Memories map has route markers and a nonzero distance total.

    No creation service exists for Route - GPX/Takeout import is the only real
    path, and a demo has neither file to import - so this goes straight to the
    ORM. A simple 3-4 point line is enough: ``services.memories.aggregator``
    only reads ``route.path.coords[0]`` for the marker and
    ``route.distance_meters``/``route.path.geojson`` for display, no
    simplification pipeline required.

    Args:
        profile: Route owner - routes have no shared/wiki analog and are
            always personal.

    Returns:
        The created routes.
    """
    from django.contrib.gis.geos import LineString

    from urbanlens.dashboard.models.routes.model import Route, RouteSource

    routes = []
    for offset in range(2):
        base_lat, base_lng = 41.7 + offset * 0.3, -73.9 - offset * 0.2
        points = [(base_lng + step * 0.004, base_lat + step * 0.003) for step in range(4)]
        started_at = timezone.now() - timedelta(days=_rng.randint(15, 300))
        route = Route.objects.create(
            profile=profile,
            name=f"Morning walk {offset + 1}",
            source=RouteSource.GPX_TRACK,
            path=LineString(points, srid=4326),
            raw_point_count=len(points),
            simplified_point_count=len(points),
            distance_meters=_rng.uniform(800.0, 4000.0),
            started_at=started_at,
            ended_at=started_at + timedelta(hours=1, minutes=_rng.randint(10, 50)),
        )
        routes.append(route)
    return routes


def seed_markup_maps(profile: Profile) -> None:
    """A couple of standalone drawn maps, for the Memories "Maps" page.

    MarkupMap/PinMarkup carry no GeoDjango geometry despite the name -
    ``center_latitude``/``center_longitude`` are plain floats and
    ``PinMarkup.geometry`` is a GeoJSON-shaped ``JSONField`` (only
    :func:`seed_routes`' ``Route.path`` is a real GIS field). Built directly
    rather than through ``materialize_markup_map`` - that helper exists to
    sanitize an untrusted client-submitted snapshot, which a seeder does not
    have.

    Args:
        profile: Map owner.
    """
    from urbanlens.dashboard.models.markup.model import MarkupMap, MarkupType, PinMarkup

    for title, center in (("Rough plan for the north side", (41.72, -73.91)), ("Access notes", (41.55, -74.12))):
        markup_map = MarkupMap.objects.create(profile=profile, title=title, center_latitude=center[0], center_longitude=center[1], zoom=14)
        PinMarkup.objects.create(
            parent_map=markup_map,
            profile=profile,
            markup_type=MarkupType.TEXT,
            geometry={"type": "Point", "coordinates": [center[1], center[0]]},
            color="#e53e3e",
        )
        PinMarkup.objects.create(
            parent_map=markup_map,
            profile=profile,
            markup_type=MarkupType.LINE,
            geometry={"type": "LineString", "coordinates": [[center[1], center[0]], [center[1] + 0.01, center[0] + 0.008]]},
            color="#3182ce",
        )
        _backdate(markup_map, timezone.now() - timedelta(days=_rng.randint(5, 100)))


def seed_labels(pins_by_profile: dict[Profile, list[Pin]]) -> None:
    """Attach a few of the profile's own default labels to its pins.

    No label is ever created here - ``create_default_tags`` already gave every
    seeded profile ~43 of them (status/category/tag/media) on creation, the
    same signal a real signup gets, so this only has to choose and attach.

    Args:
        pins_by_profile: Each seeded profile mapped to its own pins.
    """
    from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
    from urbanlens.dashboard.models.labels.model import Label

    for profile, pins in pins_by_profile.items():
        categories = list(Label.objects.filter(profile=profile, kind=KIND_CATEGORY))
        tags = list(Label.objects.filter(profile=profile, kind=KIND_TAG))
        for pin in pins:
            chosen = ([_rng.choice(categories)] if categories else []) + (_rng.sample(tags, k=min(2, len(tags))) if tags else [])
            if chosen:
                pin.labels.add(*chosen)


def seed_safety_checkins(profiles: list[Profile]) -> None:
    """A couple of historical, already-resolved safety check-ins.

    Deliberately not ``create_checkin`` - that enforces one-active-check-in
    exclusivity that is irrelevant to a resolved historical row and this way
    avoids scheduling anything live. No signal is connected to
    ``SafetyCheckin`` at all (confirmed by inspection - the only app-wide
    ``post_save`` receivers are for label defaults and achievements), and
    notification only ever happens from escalation Celery tasks or explicit
    calls this never makes, so a plain create is silent by construction, not
    by luck. ``archive_scheduled_at`` is left unset, which is what keeps a
    resolved row out of the archival sweep.

    Args:
        profiles: Every seeded profile.
    """
    from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus

    for profile in profiles:
        checkin_by = timezone.now() - timedelta(days=_rng.randint(20, 150))
        checkin = SafetyCheckin.objects.create(
            profile=profile,
            title="Evening walk",
            checkin_by=checkin_by,
            status=SafetyCheckinStatus.CHECKED_IN,
            resolved_at=checkin_by + timedelta(minutes=_rng.randint(20, 90)),
        )
        checkin.ensure_slug()


def seed_journal_content(pins_by_profile: dict[Profile, list[Pin]]) -> list[Any]:
    """Reviews and pin comments - the two Journal sources ``seed_wiki_comments`` doesn't cover.

    ``get_journal_entries`` merges four sources (visit notes, Review ratings,
    Comment/TripComment, ArticleRevision); visit notes already exist via
    :func:`seed_visits`, and this fills in the other two that are cheap to
    seed. Article revisions are left alone - a demo wiki has no article of its
    own to revise.

    Args:
        pins_by_profile: Each seeded profile mapped to its own pins.

    Returns:
        The created pin comments (not the reviews - there is nothing further
        in this module that needs to reference a review by instance).
    """
    from urbanlens.dashboard.models.reviews.model import Review
    from urbanlens.dashboard.services.comments.comments import create_comment

    comments = []
    for profile, pins in pins_by_profile.items():
        for pin in pins[:3]:
            review, created = Review.objects.get_or_create(profile=profile, pin=pin, defaults={"rating": _rng.randint(3, 5)})
            if created:
                _backdate(review, timezone.now() - timedelta(days=_rng.randint(5, 300)))
        for pin in pins[:2]:
            comment = create_comment(profile=profile, pin=pin, text=_rng.choice(_WIKI_COMMENT_OPENERS))
            _backdate(comment, timezone.now() - timedelta(days=_rng.randint(1, 60)))
            comments.append(comment)
    return comments


def seed_reactions(owner: Profile, personas: list[Profile]) -> None:
    """A few reactions on existing comments and messages.

    Straight to ``Reaction.objects.create`` rather than ``toggle_reaction`` -
    that service function notifies the comment's author on add, which is
    exactly the kind of side effect this module stays silent by construction
    to avoid.

    Args:
        owner: The login account's profile.
        personas: The other seeded profiles.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.reactions.model import Reaction
    from urbanlens.dashboard.services.comments.comments import ALLOWED_EMOJIS

    emojis = list(ALLOWED_EMOJIS)
    for comment in Comment.objects.all()[:5]:
        reactor = _rng.choice([owner, *personas])
        if reactor == comment.profile:
            continue
        Reaction.objects.get_or_create(profile=reactor, comment=comment, defaults={"emoji": _rng.choice(emojis)})


def seed_pin_shares(owner: Profile, personas: list[Profile], pins: list[Pin]) -> None:
    """A couple of accepted pin shares, for the Memories "Sharing" page.

    Replicates the share-creation half of ``create_pin_share`` without its
    notification - ``resolve_and_stamp_origin_share``/``record_share_exposure``
    are the exact two calls that function makes to keep the ``LocationExposure``
    provenance chain intact (CLAUDE.md requires this of every share path, this
    one included), and are the whole reason this is not a bare
    ``PinShare.objects.create``.

    Args:
        owner: The sharer.
        personas: Share recipients.
        pins: The owner's pins to share.
    """
    from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
    from urbanlens.dashboard.models.pin_share.model import PinShare
    from urbanlens.dashboard.services.sharing.share_provenance import record_share_exposure, resolve_and_stamp_origin_share

    for persona, pin in zip(personas[:2], pins[:2], strict=False):
        share = PinShare.objects.create(
            pin=pin,
            location=pin.location,
            from_profile=owner,
            to_profile=persona,
            parent_share=resolve_and_stamp_origin_share(pin),
            status=PinShareStatus.ACCEPTED,
            message="Thought you'd like this one.",
        )
        record_share_exposure(share)
        _backdate(share, timezone.now() - timedelta(days=_rng.randint(2, 60)))


def seed_trips(owner: Profile, personas: list[Profile], pool: list[Location]) -> list[Trip]:
    """A past trip (so Memories has a real trip in its history) and an upcoming one.

    Both need an explicit ``start_date``/``scheduled_at`` to appear on the
    Memories timeline at all: ``_trips_for_range`` computes a trip's effective
    date as ``Coalesce(start_date, first_activity_date)`` and filters out rows
    where that is null - a trip created (as this did before) with neither set
    is dropped from the timeline entirely, though it still counts toward the
    hero-stat trip count via membership alone. That mismatch - present in the
    count, absent from the map - is what this backdating avoids.

    Args:
        owner: The trips' creator.
        personas: Candidates for trip membership.
        pool: Pooled locations to build activities against; nothing is built
            when there is no location pool yet.

    Returns:
        The created trips, in creation order. Empty when the pool is empty.
    """
    from urbanlens.dashboard.services.trips.trip_activities import create_activity
    from urbanlens.dashboard.services.trips.trip_crud import create_trip
    from urbanlens.dashboard.services.trips.trip_membership import set_trip_rsvp

    if not pool:
        return []

    from urbanlens.dashboard.models.trips.model import TripActivity

    trips = []
    past_start = (timezone.now() - timedelta(days=75)).date()
    trip, _created = create_trip(owner, name="Spring exploring weekend", description="A weekend circuit - see what's actually feasible to hit in two days.", start_date=past_start, end_date=past_start + timedelta(days=1))
    for offset, location in enumerate(pool[:3]):
        create_activity(trip, owner, title=location.official_name or "Stop", place={"location_uuid": str(location.uuid)}, scheduled_at=timezone.now() - timedelta(days=75 - offset), status=TripActivity.STATUS_CONFIRMED)
    trips.append(trip)

    if len(pool) > 3:
        upcoming_start = (timezone.now() + timedelta(days=21)).date()
        upcoming, _created = create_trip(owner, name="Fall exploring weekend", description="Next one on the list.", start_date=upcoming_start)
        for location in pool[3:5]:
            create_activity(upcoming, owner, title=location.official_name or "Stop", place={"location_uuid": str(location.uuid)}, scheduled_at=timezone.now() + timedelta(days=21))
        trips.append(upcoming)

    if personas:
        from urbanlens.dashboard.models.trips.model import TripMembership

        membership = TripMembership.objects.create(trip=trips[0], profile=personas[0], status=TripMembership.STATUS_JOINED)
        _backdate(membership, timezone.now() - timedelta(days=75))
        set_trip_rsvp(trips[0], personas[0], "yes")
        if len(trips) > 1:
            TripMembership.objects.create(trip=trips[1], profile=personas[0], status=TripMembership.STATUS_INVITED)
    return trips


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


def seed_photos(pins_by_profile: dict[Profile, list[Pin]], *, on_this_day: bool = False) -> list[Any]:
    """A handful of generated photos per profile, attached to their own pins.

    ``source=ImageSource.UPLOAD`` (the field default) is correct here, not a
    fileless-row workaround: a real file is written to local storage, so this
    is what a genuine upload looks like, and it is also what
    ``achievements.signals._is_genuine_upload`` requires for the photo streak
    - a nice side effect, since it means that surface has something real to
    show too.

    Args:
        pins_by_profile: Each seeded profile mapped to its own pins.
        on_this_day: When True, one photo is dated exactly one year ago today
            - see ``seed_visits``' identical parameter for why an exact match
            has to be deliberate rather than left to chance.

    Returns:
        The created photos.
    """
    from urbanlens.dashboard.models.images.model import Image

    photos = []
    stamped_on_this_day = False
    for profile, pins in pins_by_profile.items():
        for index, pin in enumerate(pins[:4]):
            color = _PHOTO_PALETTE[index % len(_PHOTO_PALETTE)]
            if on_this_day and not stamped_on_this_day:
                taken_at = timezone.now().replace(year=timezone.now().year - 1)
                stamped_on_this_day = True
            else:
                taken_at = timezone.now() - timedelta(days=_rng.randint(3, 300))
            photo = Image.objects.create(
                pin=pin,
                location=pin.location,
                profile=profile,
                image=_placeholder_photo(pin.location.official_name or "UrbanLens", color),
                taken_at=taken_at,
            )
            _backdate(photo, taken_at)
            photos.append(photo)
    return photos


def seed_visit_photo(profile: Profile, visit: Any) -> None:
    """One photo attached to a logged visit, via ``Image.visit``.

    Args:
        profile: The visit's owner.
        visit: A ``PinVisit`` already created by :func:`seed_visits`.
    """
    from urbanlens.dashboard.models.images.model import Image

    photo = Image.objects.create(
        pin=visit.pin,
        location=visit.pin.location,
        profile=profile,
        visit=visit,
        image=_placeholder_photo(visit.pin.location.official_name or "Visit", _rng.choice(_PHOTO_PALETTE)),
        taken_at=visit.visited_at,
    )
    _backdate(photo, visit.visited_at)


def seed_comment_photo(comment: Any) -> None:
    """One photo on a comment - unlike Pin/PinVisit/DirectMessage, ``Comment.image`` is its own field, not an ``Image`` FK.

    Args:
        comment: A ``Comment`` already created elsewhere in this module.
    """
    comment.image = _placeholder_photo("Comment photo", _rng.choice(_PHOTO_PALETTE))
    comment.save(update_fields=["image"])


def seed_dm_photo(sender: Profile, dm: Any) -> None:
    """One photo attached to a direct message, via ``Image.direct_message``.

    ``images_revealed=True`` so the seeded photo renders unblurred by default
    - that field otherwise governs the blur-reveal state for a photo from
    someone the recipient hasn't trusted yet, which is irrelevant flavor for a
    demo and would just make the photo look broken until clicked.

    Args:
        sender: The message's sender, credited as the photo's uploader.
        dm: A ``DirectMessage`` already created by :func:`seed_direct_messages`.
    """
    from urbanlens.dashboard.models.images.model import Image

    Image.objects.create(direct_message=dm, profile=sender, image=_placeholder_photo("Shared photo", _rng.choice(_PHOTO_PALETTE)), taken_at=timezone.now() - timedelta(days=_rng.randint(1, 30)))
    dm.images_revealed = True
    dm.save(update_fields=["images_revealed"])


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
