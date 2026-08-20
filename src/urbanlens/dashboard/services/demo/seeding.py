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

**No outbound anything.** Seeding runs with Celery dispatch patched out, and the
profiles are written with ``external_apis_enabled``/``ai_enabled`` off *before*
any content exists, so a later worker pass cannot pick the rows up and start
calling paid APIs on their behalf. A blank ``user.email`` is load-bearing rather
than cosmetic: it is what keeps account mail, invite lookups and the purge's own
deletion notice silent.
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


def _make_user(seed: str, index: int, display_name: str) -> User:
    """Create one active demo user with no email and a real random password.

    A real password rather than ``set_unusable_password()``: an unusable one
    routes the very next request into the "set a password" prompt, which is not
    a demo.

    Args:
        seed: Session token.
        index: 0 for the login account, 1+ for personas.
        display_name: Shown around the UI.

    Returns:
        The created user.
    """
    user = User.objects.create_user(
        username=demo_username(seed, index),
        email="",
        password=secrets.token_urlsafe(32),
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


def seed_demo_account(*, ttl_hours: int = 24) -> User:
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

    Returns:
        The login account's user.
    """
    seed = secrets.token_hex(4)
    expires_at = timezone.now() + timedelta(hours=ttl_hours)

    with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"), transaction.atomic():
        owner_user = _make_user(seed, 0, "Alex Rivera")
        owner = _prepare_profile(
            owner_user,
            bio="Trying out UrbanLens. Mostly mills, hospitals and anything with a good stair.",
            expires_at=expires_at,
        )

        personas = []
        for index, (name, bio) in enumerate(_PERSONAS, start=1):
            persona_user = _make_user(seed, index, name)
            personas.append(_prepare_profile(persona_user, bio=bio, expires_at=expires_at))

        pool = pool_locations()
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
        social.seed_wiki_comments(pins_by_profile)
        social.seed_direct_messages(owner, personas)
        social.seed_group_chat(owner, personas)
        for profile, pins in pins_by_profile.items():
            social.seed_visits(profile, pins)
        social.seed_trip(owner, personas, pool)
        social.seed_pin_lists(owner, owner_pins)
        social.seed_achievements_and_activity([owner, *personas])

        logger.info("demo: seeded account %s with %d pins and %d personas", owner_user.username, len(owner_pins), len(personas))
        if not owner_pins:
            # Expected until public locations have been imported. Logged rather
            # than raised: a demo instance must still come up and sign people in.
            logger.warning("demo: the location pool is empty - seeded %s with no pins", owner_user.username)

    return owner_user
