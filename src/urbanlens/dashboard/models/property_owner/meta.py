from urbanlens.dashboard.models import abstract


class OwnerSource(abstract.TextChoices):
    """Where a WikiOwner/WikiPropertySale record's data came from.
    OFFICIAL is reserved for records populated by a future automated, location-scoped data source (e.g. a property-records API/plugin) - it is never directly user-editable (see the wiki-scoped edit views' guards), so a user can't silently corrupt data that every other viewer relies on.
    """

    USER = "user", "User contributed"
    OFFICIAL = "official", "Official"
