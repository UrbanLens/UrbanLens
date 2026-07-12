from urbanlens.dashboard.models import abstract


class PinShareStatus(abstract.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    ALREADY_PINNED = "already_pinned", "Already pinned"
    # Auto-recorded when sharing a MarkupMap reveals this pin (see
    # services.map_pin_share_detection) - never actionable, never
    # materializes a Pin via _create_pin_from_share.
    DETECTED = "detected", "Detected From Shared Map"


class PinShareOrigin(abstract.TextChoices):
    """How a PinShare came to exist."""

    EXPLICIT = "explicit", "Explicit Share"
    MAP_DETECTED = "map_detected", "Detected From Shared Map"
