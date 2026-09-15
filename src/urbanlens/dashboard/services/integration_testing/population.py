"""A population of ordinary accounts, for measuring how many people the site can serve at once.

The neighbour test asks what one account costs another; this asks what a thousand cost everyone. Each account is
sized from a fixed heavy-tailed distribution, placed so neighbouring accounts share places, befriended to the accounts
after it, and handed a session minted here - signing in a thousand virtual users at ~1s of PBKDF2 each would load-test
the password hasher instead of the site."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from importlib import import_module
import itertools
import logging
import math
import random
import secrets
import time
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.middleware.csrf import CSRF_ALLOWED_CHARS, CSRF_SECRET_LENGTH
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.integration_testing import INTEGRATION_EMAIL_DOMAIN, INTEGRATION_USERNAME_PREFIX
from urbanlens.dashboard.services.integration_testing.accounts import generate_password, prepare_signed_in_account
from urbanlens.dashboard.services.integration_testing.perf_seed import COORDINATE_STEP, GRID_SIDE, HEAVY_LABEL_NAME, PIN_NAME_PREFIX, analyze_seeded_tables, seed_heavy_account
from urbanlens.dashboard.services.pins.pin_list_membership import resync_smart_list

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Marks a population account, after the integration prefix every purge selects on.
POPULATION_INFIX = "load-"

#: The backend a password sign-in records in the session.
AUTH_BACKEND = "urbanlens.dashboard.services.auth.auth_backend.EmailOrUsernameModelBackend"


@dataclass(frozen=True, slots=True)
class SizeTier:
    """A share of the population, and the range of pin counts its accounts hold.

    Attributes:
        share: Fraction of accounts in this tier.
        min_pins: Smallest account in the tier.
        max_pins: Largest account in the tier.
    """

    share: float
    min_pins: int
    max_pins: int

    def __post_init__(self) -> None:
        if not 0.0 < self.share <= 1.0:
            raise ValueError(f"A tier's share must be in (0, 1]; got {self.share}.")
        if not 1 <= self.min_pins <= self.max_pins:
            raise ValueError(f"A tier needs 1 <= min_pins <= max_pins; got {self.min_pins}..{self.max_pins}.")


#: Most accounts are small and a few are very large, and the few are what make one account's cost everyone's problem.
DEFAULT_TIERS: tuple[SizeTier, ...] = (
    SizeTier(share=0.60, min_pins=10, max_pins=100),
    SizeTier(share=0.30, min_pins=100, max_pins=1_000),
    SizeTier(share=0.09, min_pins=1_000, max_pins=5_000),
    SizeTier(share=0.01, min_pins=10_000, max_pins=20_000),
)

#: Account zero's first pin. South Pacific, clear of the neighbour test's heavy account at (-30, -140).
POPULATION_ORIGIN = (-60.0, -170.0)

#: Accounts per row of the placement lattice.
ACCOUNTS_PER_ROW = 200

#: A quarter of one account's grid width, so an account holding more than 50 pins shares places with the next.
ACCOUNT_LONGITUDE_STEP = 0.5

#: Taller than the largest default tier's grid, so one row of accounts never overlaps the next.
ACCOUNT_LATITUDE_STEP = 1.5

#: Each account is friends with this many of the accounts after it.
FRIENDS_AHEAD = 5

MESSAGES_PER_CONVERSATION = 6
UNREAD_MESSAGES = 2
NOTIFICATIONS_PER_ACCOUNT = 12
UNREAD_NOTIFICATIONS = 3

#: One pin in this many has a logged visit, up to `MAX_VISITS`.
PINS_PER_VISIT = 10
MAX_VISITS = 50

LABELS_PER_PIN = 3

#: Comments on each account's first pin by its owner, and on that pin's wiki by its owner and a friend.
COMMENTS_PER_PIN = 4

#: Smart lists per account, each matching one label part of its pins carry.
SMART_LISTS = 3

#: Global labels each account restyles, where that many tags or categories exist.
LABEL_CUSTOMIZATIONS = 2
#: One pin in this many carries each global label an account restyles.
GLOBAL_LABEL_SPACING = 10

#: Stops on each account's trip, each at one of its pins, planned with a friend.
TRIP_ACTIVITIES = 3

#: Routes every account uses, resolved once for the manifest so the harness never writes a path by hand.
SHARED_ROUTES = (
    "health-ready",
    "home.view",
    "map.view",
    "map.document",
    "map.pins.meta",
    "map.search",
    "saved_filters.counts",
    "notifications.view",
    "notifications.unread_count",
    "notifications.dropdown",
    "messages.view",
    "messages.unread_count",
    "messages.list",
    "safety.active_banner",
    "memories.view",
    "organize.index",
    "trips.overview",
    "search.panel",
    "profile.view",
)


@dataclass(frozen=True, slots=True)
class PopulationAccount:
    """One account as the load harness uses it.

    Attributes:
        index: Position in the population, which fixes its size and placement.
        username: The account's username.
        profile_slug: Its profile's slug.
        pins: Root pins it holds.
        cookies: A signed-in browser's cookies, by their configured names.
        paths: Account-specific pages, resolved by name.
    """

    index: int
    username: str
    profile_slug: str
    pins: int
    cookies: dict[str, str]
    paths: dict[str, str]


@dataclass
class PopulationResult:
    """What provisioning did, and the accounts it left."""

    accounts: list[PopulationAccount] = field(default_factory=list)
    created: int = 0
    pins_created: int = 0
    analyzed: bool = False
    seconds: float = 0.0

    def manifest(self, *, site_url: str, environment: str) -> dict[str, Any]:
        """The document the capacity harness reads.

        Args:
            site_url: Where the accounts live.
            environment: The environment's name, so a manifest cannot be mistaken for another deployment's.

        Returns:
            A JSON-serialisable mapping. It holds live sessions and no passwords.
        """
        return {
            "kind": "population",
            "site_url": site_url,
            "environment": environment,
            "routes": shared_routes(),
            "pin_name_prefix": PIN_NAME_PREFIX,
            "analyzed": self.analyzed,
            "pins": sum(account.pins for account in self.accounts),
            "accounts": [asdict(account) for account in self.accounts],
        }


def population_username(index: int) -> str:
    """The username the account at *index* always has."""
    return f"{INTEGRATION_USERNAME_PREFIX}{POPULATION_INFIX}{index:05d}"


def pins_for(index: int, tiers: Sequence[SizeTier] | None = None) -> int:
    """How many pins the account at *index* holds.

    Deterministic, so a re-run tops each account up to the same size. Log-uniform within a tier, because pin counts
    spread by orders of magnitude rather than evenly.

    Args:
        index: Position in the population.
        tiers: The distribution; `DEFAULT_TIERS` when omitted.

    Returns:
        A pin count within one tier's range.
    """
    tiers = _checked(tiers)
    rng = random.Random(f"population:{index}")  # noqa: S311 - a reproducible fixture, not a secret
    draw = rng.random()
    chosen = tiers[-1]
    for tier, ceiling in zip(tiers, itertools.accumulate(tier.share for tier in tiers), strict=True):
        if draw < ceiling:
            chosen = tier
            break
    return round(math.exp(rng.uniform(math.log(chosen.min_pins), math.log(chosen.max_pins))))


def origin_for(index: int) -> tuple[float, float]:
    """Where the account at *index* lays out its grid."""
    row, column = divmod(index, ACCOUNTS_PER_ROW)
    return POPULATION_ORIGIN[0] + row * ACCOUNT_LATITUDE_STEP, POPULATION_ORIGIN[1] + column * ACCOUNT_LONGITUDE_STEP


def shared_routes() -> dict[str, str]:
    """Every route in `SHARED_ROUTES`, by name."""
    return {name: reverse(name) for name in SHARED_ROUTES}


def mint_session(user: User) -> dict[str, str]:
    """Sign *user* in without a request, recording what `django.contrib.auth.login` records.

    Args:
        user: The account to sign in.

    Returns:
        The session and CSRF cookies a browser signed in as *user* would send, by their configured names.

    Raises:
        RuntimeError: The session store created no key.
    """
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = AUTH_BACKEND
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    store.create()
    if not store.session_key:
        raise RuntimeError(f"The session store created no key for {user.username}.")
    csrf_secret = "".join(secrets.choice(CSRF_ALLOWED_CHARS) for _ in range(CSRF_SECRET_LENGTH))
    return {settings.SESSION_COOKIE_NAME: store.session_key, settings.CSRF_COOKIE_NAME: csrf_secret}


def provision_population(
    count: int,
    *,
    tiers: Sequence[SizeTier] | None = None,
    analyze: bool = True,
    progress: Callable[[str], object] | None = None,
) -> PopulationResult:
    """Create or top up *count* population accounts and sign each one in.

    Idempotent: accounts, pins, friendships, conversations, notifications, visits, comments, trips, smart lists and
    label customizations are only added where missing.
    Sessions are new on every run.

    Args:
        count: How many accounts.
        tiers: The size distribution; `DEFAULT_TIERS` when omitted.
        analyze: Refresh planner statistics for every table this wrote to.
        progress: Called with a line of progress now and then.

    Returns:
        The accounts, and what was created.

    Raises:
        ValueError: *count* is not positive, or a tier's accounts would not fit in one row of the lattice.
    """
    if count < 1:
        raise ValueError(f"A population needs at least one account; got {count}.")
    tiers = _checked(tiers)
    tallest = math.ceil(max(tier.max_pins for tier in tiers) / GRID_SIDE) * COORDINATE_STEP
    if tallest >= ACCOUNT_LATITUDE_STEP:
        raise ValueError(f"The largest tier's grid is {tallest} degrees tall and would overlap the next row of accounts.")

    started = time.perf_counter()
    password_hash = make_password(generate_password())
    result = PopulationResult()
    users: list[User] = []
    profiles: list[Profile] = []
    sizes: list[int] = []

    for index in range(count):
        user, profile, created = _ensure_account(index, password_hash)
        report = seed_heavy_account(profile, pins=pins_for(index, tiers), analyze=False, labels_per_pin=LABELS_PER_PIN, origin=origin_for(index))
        _log_visits(profile, report["pins"])
        users.append(user)
        profiles.append(profile)
        sizes.append(report["pins"])
        result.created += int(created)
        result.pins_created += report["created"]
        if progress is not None and ((index + 1) % 50 == 0 or index + 1 == count):
            progress(f"  {index + 1}/{count} accounts, {result.pins_created} pins created")

    _befriend(profiles)
    _converse(profiles)
    _notify(profiles)
    _discuss(profiles)
    _plan_trips(profiles)
    _organize(profiles)
    if analyze:
        result.analyzed = analyze_seeded_tables(_written_tables())

    for index, (user, profile) in enumerate(zip(users, profiles, strict=True)):
        friend = _friend_of(profiles, index)
        result.accounts.append(
            PopulationAccount(
                index=index,
                username=user.username,
                profile_slug=profile.ensure_slug(),
                pins=sizes[index],
                cookies=mint_session(user),
                paths=_account_paths(profile, friend),
            ),
        )
    result.seconds = round(time.perf_counter() - started, 1)
    logger.info("population: %d accounts (%d created), %d pins created, %.1fs", count, result.created, result.pins_created, result.seconds)
    return result


def _checked(tiers: Sequence[SizeTier] | None) -> Sequence[SizeTier]:
    """*tiers*, or the default, once their shares are known to cover the whole population."""
    tiers = DEFAULT_TIERS if tiers is None else tiers
    if not tiers or not math.isclose(sum(tier.share for tier in tiers), 1.0, abs_tol=1e-9):
        raise ValueError("Size tiers must be non-empty and their shares must sum to 1.")
    return tiers


@transaction.atomic
def _ensure_account(index: int, password_hash: str) -> tuple[User, Profile, bool]:
    """The account at *index*, created if absent, past every post-login diversion.

    Every account shares one password hash that nothing records, so no account can be signed into except through the
    sessions minted for the manifest.
    """
    username = population_username(index)
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": f"{username}@{INTEGRATION_EMAIL_DOMAIN}", "is_active": True, "password": password_hash},
    )
    return user, prepare_signed_in_account(user), created


def _log_visits(profile: Profile, pins: int) -> None:
    """Give one pin in `PINS_PER_VISIT` a visit, unless the account already has visits."""
    if PinVisit.objects.filter(pin__profile=profile).exists():
        return
    wanted = min(pins // PINS_PER_VISIT, MAX_VISITS)
    pin_ids = Pin.objects.filter(profile=profile).order_by("pk").values_list("pk", flat=True)[:wanted]
    now = timezone.now()
    PinVisit.objects.bulk_create([PinVisit(pin_id=pin_id, visited_at=now - timedelta(days=7 * (offset + 1))) for offset, pin_id in enumerate(pin_ids)])


def _befriend(profiles: Sequence[Profile]) -> None:
    """Befriend each account to the `FRIENDS_AHEAD` accounts after it."""
    Friendship.objects.bulk_create(
        [
            Friendship(from_profile=profiles[index], to_profile=profiles[other], status=FriendshipStatus.ACCEPTED, relationship_type=FriendshipType.FRIEND)
            for index in range(len(profiles))
            for other in range(index + 1, min(index + 1 + FRIENDS_AHEAD, len(profiles)))
        ],
        ignore_conflicts=True,
        batch_size=1_000,
    )


def _converse(profiles: Sequence[Profile]) -> None:
    """A conversation between each account and the next, ending in messages the reader has not read."""
    ids = [profile.pk for profile in profiles]
    existing = {frozenset(pair) for pair in DirectMessage.objects.filter(sender_id__in=ids, recipient_id__in=ids).values_list("sender_id", "recipient_id").distinct()}
    now = timezone.now()
    messages = []
    for first, second in itertools.pairwise(profiles):
        if frozenset((first.pk, second.pk)) in existing:
            continue
        for position in range(MESSAGES_PER_CONVERSATION):
            sender, recipient = (first, second) if position % 2 == 0 else (second, first)
            unread = position >= MESSAGES_PER_CONVERSATION - UNREAD_MESSAGES
            messages.append(
                DirectMessage(
                    sender=sender,
                    recipient=recipient,
                    body=f"Perf message {position}",
                    read_at=None if unread else now,
                    sender_delete_after=sender.direct_message_delete_after,
                ),
            )
    DirectMessage.objects.bulk_create(messages, batch_size=1_000)


def _notify(profiles: Sequence[Profile]) -> None:
    """`NOTIFICATIONS_PER_ACCOUNT` notifications for every account that has none."""
    notified = set(NotificationLog.objects.filter(profile__in=profiles).values_list("profile_id", flat=True).distinct())
    NotificationLog.objects.bulk_create(  # notify-bypass-ok: seeded fixture rows for a load test, never delivered to a person
        [
            NotificationLog(  # notify-bypass-ok: seeded fixture rows for a load test, never delivered to a person
                profile=profile,
                title=f"Perf notification {position}",
                message="Seeded for the capacity test.",
                notification_type=NotificationType.INFO,
                status=Status.UNREAD if position < UNREAD_NOTIFICATIONS else Status.READ,
            )
            for profile in profiles
            if profile.pk not in notified
            for position in range(NOTIFICATIONS_PER_ACCOUNT)
        ],
        batch_size=1_000,
    )


def _friend_of(profiles: Sequence[Profile], index: int) -> Profile | None:
    """The account the one at *index* shares a conversation, a trip and a wiki thread with."""
    if index + 1 < len(profiles):
        return profiles[index + 1]
    return profiles[index - 1] if index else None


def _first_pins(profile: Profile, count: int) -> list[Pin]:
    """The account's first root pins, which its journeys open."""
    return list(Pin.objects.filter(profile=profile).root_pins().select_related("location").order_by("pk")[:count])


def _discuss(profiles: Sequence[Profile]) -> None:
    """Comments on each account's first pin, and on its wiki from the account and a friend, where there are none."""
    comments: list[Comment] = []
    for index, profile in enumerate(profiles):
        pins = _first_pins(profile, 1)
        if not pins or pins[0].location is None or Comment.objects.filter(pin=pins[0]).exists():
            continue
        pin = pins[0]
        wiki, _ = Wiki.objects.get_or_create(location=pin.location, defaults={"name": pin.name})
        voices = (profile, _friend_of(profiles, index) or profile)
        for position in range(COMMENTS_PER_PIN):
            comments.append(Comment(pin=pin, profile=profile, text=f"Perf note {position}"))
            comments.append(Comment(wiki=wiki, profile=voices[position % 2], text=f"Perf comment {position}"))
    Comment.objects.bulk_create(comments, batch_size=1_000)


def _plan_trips(profiles: Sequence[Profile]) -> None:
    """A trip through each account's first pins with a friend on it, for every account without one."""
    planned = set(Trip.objects.filter(creator__in=profiles).values_list("creator_id", flat=True))
    for index, profile in enumerate(profiles):
        friend = _friend_of(profiles, index)
        if friend is None or profile.pk in planned:
            continue
        with transaction.atomic():
            trip = Trip.objects.create(name=f"Perf Trip {index}", creator=profile)
            TripMembership.objects.bulk_create([TripMembership(trip=trip, profile=profile, is_organizer=True), TripMembership(trip=trip, profile=friend)])
            stops = _first_pins(profile, TRIP_ACTIVITIES)
            TripActivity.objects.bulk_create([TripActivity(trip=trip, pin=pin, location=pin.location, added_by=profile, title=pin.name, order=order) for order, pin in enumerate(stops)])


def _organize(profiles: Sequence[Profile]) -> None:
    """Smart lists over labels each account's pins carry, and restyled global labels, where it has none."""
    listed = set(PinList.objects.filter(profile__in=profiles, is_smart=True).values_list("profile_id", flat=True))
    customized = set(LabelCustomization.objects.filter(profile__in=profiles).values_list("profile_id", flat=True))
    shared = list(Label.objects.global_only().suggestable().order_by("pk").values_list("pk", flat=True)[:LABEL_CUSTOMIZATIONS])
    for profile in profiles:
        if profile.pk not in listed:
            carried = Exists(Pin.labels.through.objects.filter(label_id=OuterRef("pk"), pin__profile=profile))
            for label_id in list(Label.objects.for_profile(profile).location_labels().filter(carried).exclude(name=HEAVY_LABEL_NAME).order_by("pk").values_list("pk", flat=True)[:SMART_LISTS]):
                resync_smart_list(PinList.objects.create(profile=profile, name=f"Perf Smart {label_id}", is_smart=True, smart_filter={"tags": [label_id]}))
        if shared and profile.pk not in customized:
            _restyle(profile, shared)


def _restyle(profile: Profile, label_ids: Sequence[int]) -> None:
    """Carry each global label on a share of the account's pins, and override its colour."""
    pin_ids = list(Pin.objects.filter(profile=profile).root_pins().order_by("pk").values_list("pk", flat=True))[::GLOBAL_LABEL_SPACING]
    through = Pin.labels.through
    with transaction.atomic():
        through.objects.bulk_create([through(pin_id=pin_id, label_id=label_id) for pin_id in pin_ids for label_id in label_ids], ignore_conflicts=True, batch_size=5_000)
        LabelCustomization.objects.bulk_create([LabelCustomization(profile=profile, label_id=label_id, color="#a5584a") for label_id in label_ids])


def _account_paths(profile: Profile, friend: Profile | None) -> dict[str, str]:
    """The account-specific pages the harness visits, ensuring the wiki it links to exists."""
    paths: dict[str, str] = {}
    pin = Pin.objects.filter(profile=profile).root_pins().select_related("location").order_by("pk").first()
    if pin is not None:
        pin_slug = pin.ensure_slug()
        for key, name in (("pin", "pin.details"), ("pin_gallery", "pin.gallery.json"), ("pin_nearby", "pin.nearby_pins.json"), ("pin_visits", "pin.visits")):
            paths[key] = reverse(name, kwargs={"pin_slug": pin_slug})
        location = pin.location
        location.ensure_slug()
        Wiki.objects.get_or_create(location=location, defaults={"name": pin.name})
        paths["wiki"] = reverse("location.wiki", kwargs={"location_slug": location.slug})
    if friend is not None:
        friend_slug = friend.ensure_slug()
        paths["friend_profile"] = reverse("profile.view_user", kwargs={"profile_slug": friend_slug})
        paths["conversation"] = reverse("messages.conversation", kwargs={"profile_slug": friend_slug})
    return paths


def _written_tables() -> list[str]:
    """Every table provisioning writes rows to in bulk."""
    models = (Pin, Location, Label, Pin.labels.through, Friendship, DirectMessage, NotificationLog, PinVisit, Comment, Trip, TripMembership, TripActivity, PinList, PinListItem, LabelCustomization)
    return [model._meta.db_table for model in models]  # noqa: SLF001 - _meta is Django's metadata API
