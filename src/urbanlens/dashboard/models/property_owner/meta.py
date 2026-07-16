from urbanlens.dashboard.models import abstract


class OwnerVisibility(abstract.TextChoices):
    """Who can see and edit an Owner/PropertySale record.

    PRIVATE records are attached to a single Pin and only visible there, to
    that pin's own profile - never shared with other users or the wiki.
    SHARED records are attached to a Location and visible/editable by anyone
    with a pin there, matching the wiki's own access rule
    (``services.wiki_access.location_visible_to``).
    """

    PRIVATE = "private", "Private (only visible on your own pin)"
    SHARED = "shared", "Shared (visible to everyone with a pin here)"


class OwnerSource(abstract.TextChoices):
    """Where an Owner/PropertySale record's data came from.

    OFFICIAL is reserved for records populated by a future automated,
    location-scoped data source (e.g. a property-records API/plugin) - it is
    never directly user-editable (see the edit views' guard), so a user
    can't silently corrupt data that every other viewer of that location
    relies on. Only ``SHARED`` records may be ``OFFICIAL`` (enforced by a DB
    constraint) - an authoritative fact about a place is inherently a
    location-level fact, never private to one user's pin.
    """

    USER = "user", "User contributed"
    OFFICIAL = "official", "Official"
