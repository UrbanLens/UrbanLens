"""Static "how do I…" help content for the assistant (plan §10, batch 4).

This is the *only* source the assistant is allowed to answer a "how do I…"
question from (see ``services.ai.assistant``'s system prompt) - hand-written
and reviewed, never scraped or inferred from a page's own markup. Keyed by
Django URL name so it lines up exactly with
``services.ai.page_context.PageContext.url_name`` and with ``urls.py``
itself, which the contract test in ``dashboard/tests/hypothesis/
test_page_help.py`` walks to make sure every primary-nav page has an entry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageHelp:
    """One page's help content, verbatim what the assistant may quote.

    Attributes:
        title: The page's name, as a user would recognize it.
        key_actions: Short imperative bullets of what a user can do here.
        tips: Additional short notes - gotchas, less-obvious features.
    """

    title: str
    key_actions: tuple[str, ...]
    tips: tuple[str, ...] = ()


#: url_name -> help content. See the module docstring for how this stays in
#: sync with the primary nav.
PAGE_HELP: dict[str, PageHelp] = {
    "home.view": PageHelp(
        title="Home",
        key_actions=(
            "Customize your dashboard - add, remove, and reorder widgets.",
            "Jump into recent activity across your pins, trips, and photos.",
        ),
    ),
    "map.view": PageHelp(
        title="Map",
        key_actions=(
            "Add a pin by clicking the map, or import pins in bulk.",
            "Filter pins by label, priority, danger, visited status, and more from the filter sidebar.",
            "Save a filter as a smart list that stays in sync automatically.",
            "Draw routes, measurements, or shapes with the markup toolbar.",
        ),
        tips=("Click a pin to open its detail page; right-click for quick actions.",),
    ),
    "organize.index": PageHelp(
        title="Organize",
        key_actions=(
            "Manage labels: tags, categories, statuses, people, and priority - the Labels tabs.",
            "Create and edit pin lists and smart lists - the Lists tab.",
            "Build and manage reusable saved filters - the Filters tab.",
        ),
    ),
    "trips.overview": PageHelp(
        title="Trips",
        key_actions=(
            "Create a trip and invite friends; plan activities with scheduling.",
            "RSVP to a trip or an individual activity, and vote on proposed activities.",
            "Switch between list and calendar views.",
            "Connect Google Calendar to import events as trips or export trip activities.",
        ),
    ),
    "memories.view": PageHelp(
        title="Memories",
        key_actions=(
            "Browse a timeline/map of your routes, trips, visits, and photos, including an 'on this day' view.",
            "Log a visit for a pin you've marked visited but haven't logged yet - the Visits tab.",
            "Import GPS tracks or location history.",
            "Review pending pin-location suggestions from imported photos - the Locations tab.",
        ),
        tips=("Photo browsing itself moved to Vault - Memories is the timeline, not the photo library.",),
    ),
    "vault.home": PageHelp(
        title="Vault",
        key_actions=(
            "Browse your photo library and document library, each with their own page.",
            "Create and manage personal albums, independent of any single pin.",
            "Check your storage usage against your quota.",
        ),
    ),
    "safety.home": PageHelp(
        title="Safety Check-ins",
        key_actions=(
            "Start a check-in with an expected return time and emergency contacts before heading out somewhere risky.",
            "Add reusable emergency contacts - registered friends or an external email.",
            "If a check-in is missed, contacts are emailed and can mark you safe from a public, no-login portal.",
        ),
    ),
    "games.overview": PageHelp(
        title="Games",
        key_actions=("Play location-guessing and trivia games built from the community's own places and data.",),
    ),
    "pin.details": PageHelp(
        title="Pin detail",
        key_actions=(
            "Edit the pin's name, description, labels, priority, and security indicators.",
            "Upload and organize photos, documents, and albums for this place.",
            "Log a visit, or review external data enrichment for the location.",
            "Share the pin with a friend, or send it to the community wiki.",
        ),
    ),
    "trips.detail": PageHelp(
        title="Trip detail",
        key_actions=(
            "Add, schedule, and vote on activities.",
            "RSVP for yourself; organizers can set trip-wide defaults.",
            "Comment on the trip, or view it on a map.",
        ),
    ),
    "settings.view": PageHelp(
        title="Settings",
        key_actions=(
            "Manage your account, profile, and privacy preferences.",
            "Turn AI features on or off, and control what external APIs the assistant may use.",
            "Customize keyboard shortcuts under Shortcuts.",
            "Review storage usage and subscription/billing details.",
        ),
    ),
}


def get_page_help(url_name: str) -> PageHelp | None:
    """Return the help content for ``url_name``, or ``None`` if this page has none.

    Args:
        url_name: A Django URL name, e.g. ``"map.view"``.

    Returns:
        The page's :class:`PageHelp`, or ``None`` - never raises for an
        unknown or malformed ``url_name``.
    """
    return PAGE_HELP.get(url_name)
