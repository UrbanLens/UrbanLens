from urbanlens.dashboard.models import abstract


class PinShareStatus(abstract.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    ALREADY_PINNED = "already_pinned", "Already pinned"
