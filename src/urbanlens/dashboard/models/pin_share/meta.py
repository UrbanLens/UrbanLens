from urbanlens.dashboard.models import abstract


class PinShareStatus(abstract.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    ALREADY_PINNED = "already_pinned", "Already pinned"
    # Auto-recorded when the place was revealed indirectly - a shared
    # MarkupMap's geometry (see services.map_pin_share_detection) or a trip
    # activity (see services.trip_share_tracking) - never actionable, never
    # materializes a Pin via _create_pin_from_share.
    DETECTED = "detected", "Detected"


class PinShareOrigin(abstract.TextChoices):
    """How a PinShare came to exist."""

    EXPLICIT = "explicit", "Explicit Share"
    MAP_DETECTED = "map_detected", "Detected From Shared Map"
    # Coordinates or a street address were detected in the text of a direct
    # message (see services.dm_location_detection) - the "pin" may be None
    # when the sender never pinned the place themselves.
    DM_DETECTED = "dm_detected", "Detected From Message"
    # A pin/location was revealed to trip members by being added as a trip
    # activity (see services.trip_share_tracking).
    TRIP_ACTIVITY = "trip_activity", "Shared As Trip Activity"
