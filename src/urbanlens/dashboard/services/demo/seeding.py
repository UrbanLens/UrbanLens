"""Build one demo account, with enough content to exercise the whole product.

Runs only on a demo instance (``UL_DEMO_MODE``), against that instance's own
database. See this package's docstring for why isolation is the deployment
boundary and not a per-row flag.

Two rules shape everything here:

**Plain ORM, never model-bakery.** Baker is a dev-only dependency
(``pyproject.toml``) and staging/production images install ``--no-dev``, so a
module-scope import of it is an ``ImportError`` on deploy. Its custom classes
are registered only in ``settings/test.py`` too, so baking a Profile-touching
model outside the test runner raises. The recipes stay a test tool.

**No outbound *network* call.** Seeding runs with Celery dispatch patched out,
and the profiles are written with ``external_apis_enabled``/``ai_enabled`` off
*before* any content exists, so a later worker pass cannot pick the rows up and
start calling paid APIs on their behalf. A blank ``user.email`` is load-bearing
rather than cosmetic: it is what keeps account mail, invite lookups and the
purge's own deletion notice silent. Photos are the one place real bytes are
written - generated in memory and saved to this instance's own local storage
(see ``social.seed_photos``), never fetched from anywhere.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import secrets
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.services.demo import DEMO_USERNAME_PREFIX, social
from urbanlens.dashboard.services.demo.locations import pool_locations

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The personas a demo visitor meets: friends, chat partners, trip companions,
#: comment authors. Real Profile rows, because friendship, DMs and trip
#: membership are all FK relationships - there is no "fake friend" shape to fake.
_PERSONAS = [
    ("Rue Delacroix", "Shoots large-format on abandoned rail. Will drive four hours for a good trestle."),
    ("Milo Fenn", "Industrial archaeology, mostly mills and pump houses. Keeps very good notes."),
    ("Sana Okoye", "Urban explorer and volunteer fire-tower steward. Prefers places you are allowed to be."),
    ("Theo Vance", "Weekend explorer, terrible at remembering to log visits, excellent at finding parking."),
]


def demo_username(seed: str, index: int) -> str:
    """Username for one seeded account.

    Args:
        seed: Short random token identifying this demo session.
        index: 0 for the login account, 1+ for personas.

    Returns:
        A username carrying :data:`DEMO_USERNAME_PREFIX`, which is what the
        purge selects on and what the signup validator refuses.
    """
    return f"{DEMO_USERNAME_PREFIX}{seed}-{index}"


def _make_user(seed: str, index: int, display_name: str, *, username: str = "", password: str = "") -> User:
    """Create one active demo user with no email and a real password.

    A real password rather than ``set_unusable_password()``: an unusable one
    routes the very next request into the "set a password" prompt, which is not
    a demo. Random unless the caller supplies one - a dev environment hands its
    login credentials to whoever created it (see :func:`seed_dev_environment`),
    which a random password cannot do.

    Args:
        seed: Session token.
        index: 0 for the login account, 1+ for personas.
        display_name: Shown around the UI.
        username: Explicit username, or "" for the generated demo one.
        password: Explicit password, or "" for a random one.

    Returns:
        The created user.
    """
    user = User.objects.create_user(
        username=username or demo_username(seed, index),
        email="",
        password=password or secrets.token_urlsafe(32),
        first_name=display_name.split(" ", maxsplit=1)[0],
        last_name=" ".join(display_name.split(" ")[1:]),
    )
    user.is_active = True
    user.save(update_fields=["is_active"])
    return user


def _prepare_profile(user: User, *, bio: str, expires_at: Any) -> Profile:
    """Stamp the profile the creation signal already made.

    Updated rather than constructed: ``profile_create_user_profile`` fires on
    every ``User`` insert, so a second ``Profile(...)`` would collide with the
    row that already exists.

    The external-API switches go off here, before any content is written, so
    that even a later background pass over these rows cannot bill anything.

    Args:
        user: The freshly created user.
        bio: Profile bio text.
        expires_at: When this account becomes purgeable.

    Returns:
        The updated profile.
    """
    from urbanlens.dashboard.models.profile.model import Profile

    profile = Profile.objects.get(user=user)
    profile.bio = bio
    profile.external_apis_enabled = False
    profile.ai_enabled = False
    profile.profile_setup_complete = True
    profile.welcome_onboarding_complete = True
    profile.tos_accepted_at = timezone.now()
    profile.community_enabled = True
    profile.save()
    # Repoint the user's cached reverse relation at the row we just wrote. The
    # creation signal populated `user.profile` with the *pre-update* instance,
    # so without this every caller holding this user - the login view included -
    # reads stale settings back, and the external-API switches above look as
    # though they never applied.
    user.profile = profile
    logger.debug("demo: prepared profile %s (expires %s)", user.username, expires_at)
    return profile


def _pin_pool(profile: Profile, locations: list) -> list[Pin]:
    """Give ``profile`` a pin on each pooled location.

    Pinning is what grants wiki access - visibility is earned by holding a pin
    on the location - so this is also how a demo account comes to see each
    place's wiki, aliases and cached photos.

    The pin carries no name of its own: ``Pin.name`` is a personal override, and
    leaving it unset lets the pin display the location's real name, which is the
    one the import brought across.

    Args:
        profile: Owner of the created pins.
        locations: Locations from :func:`.locations.pool_locations`.

    Returns:
        The created pins.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return [Pin.objects.create(profile=profile, location=location) for location in locations]


def seed_demo_account(*, ttl_hours: int = 24, username: str = "", password: str = "", locations: list[Location] | None = None) -> User:
    """Create one demo login account, its personas, and their content.

    Celery dispatch is patched for the *whole* call, and the patch has to stay
    entered until after the transaction actually commits - not just until the
    last row is written. Pin, Friendship, Comment and several others fire
    ``achievements.signals`` on ``post_save``, which defers to
    ``transaction.on_commit`` rather than calling ``safely_enqueue_task``
    immediately; that deferred call runs whatever the *current* function is at
    commit time, not whatever it was when it was registered. An
    ``@transaction.atomic``-decorated function commits after its body -
    including a ``with mock.patch(...):`` block nested inside it - has already
    exited, so that nesting order patches nothing the commit actually needs:
    every on_commit callback fires against the real, unpatched function. This
    is exactly backwards from what it needs to be, which is why the patch
    wraps the atomic block here rather than sitting inside it.

    Django's ``TestCase`` cannot catch this class of bug on its own - it wraps
    every test in a transaction that is rolled back, not committed, so
    ``on_commit`` never runs and the ordering never gets exercised. Even
    ``captureOnCommitCallbacks`` does not help: it defers every captured
    callback to when the *test's* ``with`` block exits, by which point this
    function has already returned regardless of the order below - it cannot
    tell "patch outlived the real commit" from "patch outlived the function
    call". See ``SeedingCommitOrderingTests`` (a genuine
    ``TransactionTestCase``, for a real commit) for the test that actually
    exercises this and that regressing this ordering fails.

    Args:
        ttl_hours: How long before the account may be purged.
        username: Username for the login account, or "" for the generated
            ``demo-<token>-0`` form. Only that form is selected by
            ``purge_demo_accounts``, so a caller naming its own account is also
            opting out of the purge - which is what a dev environment wants and
            what the public demo instance must not do.
        password: Password for the login account, or "" for a random one.
        locations: Locations to pin, or None to read the configured manifest.
            Passed explicitly by callers that just imported a catalog into an
            instance with no manifest path configured, where
            :func:`~.locations.pool_locations` would find nothing.

    Returns:
        The login account's user.
    """
    seed = secrets.token_hex(4)
    expires_at = timezone.now() + timedelta(hours=ttl_hours)

    with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"), transaction.atomic():
        owner_user = _make_user(seed, 0, "Alex Rivera", username=username, password=password)
        owner = _prepare_profile(
            owner_user,
            bio="Trying out UrbanLens. Mostly mills, hospitals and anything with a good stair.",
            expires_at=expires_at,
        )

        personas = []
        for index, (name, bio) in enumerate(_PERSONAS, start=1):
            persona_user = _make_user(seed, index, name)
            personas.append(_prepare_profile(persona_user, bio=bio, expires_at=expires_at))

        pool = pool_locations() if locations is None else locations
        owner_pins = _pin_pool(owner, pool)
        pins_by_profile = {owner: owner_pins}
        for offset, persona in enumerate(personas):
            # Overlapping subsets, so friends' maps differ from the owner's and
            # from each other while still sharing enough places that "pins in
            # common" style surfaces have something to show.
            pins_by_profile[persona] = _pin_pool(persona, pool[offset :: len(personas)])

        # Everything below is fabricated content, not a claim about the world -
        # see services.demo.social for why that distinction is what lets it be
        # invented at all, unlike the pins above.
        social.seed_friendships(owner, personas)
        social.seed_labels(pins_by_profile)

        wiki_comments = social.seed_wiki_comments(pins_by_profile)
        journal_comments = social.seed_journal_content(pins_by_profile)
        social.seed_reactions(owner, personas)  # after comments exist - it reacts to whatever is already there

        messages = social.seed_direct_messages(owner, personas)
        social.seed_group_chat(owner, personas)

        # The "on this day" callout needs an exact month/day match in a past
        # year - deliberate on the owner's visits/photos only, once each, is
        # enough to populate it without every seeded profile claiming the
        # same anniversary.
        owner_visits: list[Any] = []
        for profile, pins in pins_by_profile.items():
            visited = social.seed_visits(profile, pins, on_this_day=profile is owner)
            if profile is owner:
                owner_visits = visited
        # Pins seed_visits never touched (it caps at 10) - deliberately
        # disjoint, since visited_without_record requires zero PinVisit rows.
        social.mark_unlogged_visits(owner_pins[10:13])

        social.seed_photos(pins_by_profile, on_this_day=True)
        if owner_visits:
            social.seed_visit_photo(owner, owner_visits[-1])
        if messages:
            social.seed_dm_photo(owner, messages[0])
        for comment in (wiki_comments or journal_comments)[:1]:
            social.seed_comment_photo(comment)

        social.seed_routes(owner)
        social.seed_markup_maps(owner)
        if personas:
            social.seed_markup_maps(personas[0])
        social.seed_pin_shares(owner, personas, owner_pins)
        social.seed_trips(owner, personas, pool)
        social.seed_pin_lists(owner, owner_pins)
        social.seed_safety_checkins([owner, *personas])
        social.seed_achievements_and_activity([owner, *personas])

        logger.info("demo: seeded account %s with %d pins and %d personas", owner_user.username, len(owner_pins), len(personas))
        if not owner_pins:
            # Expected until public locations have been imported. Logged rather
            # than raised: a demo instance must still come up and sign people in.
            logger.warning("demo: the location pool is empty - seeded %s with no pins", owner_user.username)

    return owner_user


#: The one place a dev environment is pinned to by name rather than by catalog.
#: Real coordinates, like everything else seeded here - a pin is a claim that a
#: place exists at a point, and the whole Private Pin page (boundaries, parcel
#: lookup, wiki) answers emptily for a point nobody has ever surveyed.
#: ``Location`` carries no name field of its own (the community-editable name
#: lives on ``Wiki``, the external-source one in ``official_name``), so the
#: recognisable label goes on the *pin*, where a user's own name for a place
#: belongs.
HUDSON_RIVER_STATE_HOSPITAL = {
    "name": "Hudson River State Hospital",
    "latitude": "41.733000",
    "longitude": "-73.928000",
    "locality": "Poughkeepsie",
    "administrative_area_level_1": "NY",
    "country": "US",
}


def ensure_location_pool() -> tuple[list[Location], str]:
    """Make sure there is something for seeding to pin, and say what happened.

    The gap between "the seeder works" and "a fresh environment has content":
    the pool comes from an imported catalog, and a database nobody has imported
    into has none, so seeding succeeds and produces zero pins. That reads as a
    broken seeder rather than an empty catalog, which is why the reason travels
    back to the caller as text instead of only into the log.

    Makes the one outbound call this module otherwise forbids - REData's
    ``/public-locations/`` catalog, the same call ``import_redata_public_locations``
    makes - and inherits its degrade-to-empty contract: unconfigured,
    unreachable, or not deployed all mean "no locations", never an exception.

    Returns:
        ``(locations, note)`` - the Locations to pin (possibly empty) and a
        human-readable account of where they came from or why there are none.
    """
    from urbanlens.dashboard.services.demo.locations import import_location_entries, merge_into_manifest, redata_demo_locations

    existing = pool_locations()
    if existing:
        return existing, f"{len(existing)} location(s) already in the manifest"

    entries = redata_demo_locations()
    if not entries:
        return [], "REData's public-locations catalog returned nothing (unconfigured, unreachable, or not deployed there yet) - seeded with no catalog pins"

    import_location_entries(entries)
    # Best-effort: writes only when UL_DEMO_LOCATIONS_FILE names a path, so the
    # rows are resolved directly below rather than read back through it.
    merge_into_manifest(entries)
    locations = _locations_for_entries(entries)
    return locations, f"imported {len(locations)} of {len(entries)} location(s) from REData's public-locations catalog"


def _locations_for_entries(entries: list[dict[str, Any]]) -> list[Location]:
    """Resolve just-imported export entries to their Location rows.

    :func:`~.locations.pool_locations` does this from the manifest, which is
    the demo instance's path; an instance with no manifest configured still has
    the rows, and this is how it finds them.

    Args:
        entries: Entries in export format, as handed to ``import_location_entries``.

    Returns:
        The matching Locations, in entry order, skipping any that did not land.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.location.queryset import quantize_coordinate

    resolved: list[Location] = []
    for entry in entries:
        latitude, longitude = entry.get("latitude"), entry.get("longitude")
        if latitude is None or longitude is None:
            continue
        location = Location.objects.filter(
            latitude=quantize_coordinate(latitude, "latitude"),
            longitude=quantize_coordinate(longitude, "longitude"),
        ).first()
        if location is not None:
            resolved.append(location)
    return resolved


def seed_landmark_pin(profile: Profile, landmark: dict[str, str] | None = None) -> Pin:
    """Give ``profile`` a pin on one named real place.

    Args:
        profile: Owner of the pin.
        landmark: Coordinates and address components, defaulting to
            :data:`HUDSON_RIVER_STATE_HOSPITAL`.

    Returns:
        The pin, existing or created - re-running never gives one profile two
        pins on the same place.
    """
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    landmark = landmark or HUDSON_RIVER_STATE_HOSPITAL
    location, _ = Location.objects.get_exact_or_create(
        landmark["latitude"],
        landmark["longitude"],
        defaults={
            "official_name": landmark["name"],
            "locality": landmark["locality"],
            "administrative_area_level_1": landmark["administrative_area_level_1"],
            "country": landmark["country"],
        },
    )
    # Same reason the catalog import creates one: a pinned location with no wiki
    # is a dead end, and holding the pin is what earns access to it.
    Wiki.objects.get_or_create(location=location, defaults={"name": landmark["name"]})
    pin, _ = Pin.objects.get_or_create(
        profile=profile,
        location=location,
        defaults={"name": landmark["name"], "name_is_user_provided": True},
    )
    return pin


def seed_dev_environment(*, username: str = "demo", password: str, ttl_hours: int = 24 * 365) -> dict[str, Any]:
    """Seed one ephemeral dev environment with an account somebody can log into.

    A freshly created environment (the `infrastructure` repo's ``bin/dev_env.py``)
    has an empty database, so
    every page it serves is an empty state and nothing about the product can be
    seen without first building an account and content by hand. This is the same
    content the public demo instance is seeded with, under a fixed username and
    a password derived from the environment's own slug, plus one named landmark
    pin so the map is never empty even when the catalog is unreachable.

    Deliberately not gated on ``UL_DEMO_MODE``: a dev environment is not a demo
    instance and must not wear the demo banner or expose the demo-login
    endpoint. What the management commands' demo-mode guard actually protects -
    real coordinates merging into a database holding real user data - is
    covered here by refusing to run in staging or production at all.

    Args:
        username: Login account name. Deliberately outside
            :data:`~urbanlens.dashboard.services.demo.DEMO_USERNAME_PREFIX`, so
            ``purge_demo_accounts`` cannot delete the account somebody was given.
        password: Login password, chosen by the caller so it can be reported.
        ttl_hours: Nominal account lifetime. Nothing purges this account; it is
            recorded for parity with the demo seeder.

    Returns:
        A JSON-safe summary: the credentials, what was seeded, and the catalog's
        own account of itself.

    Raises:
        RuntimeError: Called in staging or production, where this would write
            real coordinates and a shared-password account into real data.
    """
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    environment = str(app_settings.environment_name).lower()
    if environment in {"production", "staging"}:
        raise RuntimeError(f"seed_dev_environment refuses to run in the {environment} environment - it creates a shared-password account and imports real coordinates.")

    existing = User.objects.filter(username=username).first()
    if existing is not None:
        return {"username": username, "password": password, "created": False, "detail": "an account by that name already exists; nothing was seeded"}

    locations, catalog_note = ensure_location_pool()
    user = seed_demo_account(ttl_hours=ttl_hours, username=username, password=password, locations=locations)
    with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"), transaction.atomic():
        landmark = seed_landmark_pin(user.profile)

    from urbanlens.dashboard.models.pin.model import Pin

    return {
        "username": user.username,
        "password": password,
        "created": True,
        "pins": Pin.objects.filter(profile=user.profile).count(),
        "landmark": landmark.name,
        "catalog": catalog_note,
    }
