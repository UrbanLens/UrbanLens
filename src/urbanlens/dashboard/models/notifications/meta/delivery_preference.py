"""Delivery preference choices for notification preferences."""

from __future__ import annotations

from django.utils.translation import gettext as _

from urbanlens.dashboard.models.abstract.choices import TextChoices


class DeliveryPreference(TextChoices):
    """How a user wants to receive a given notification type."""

    NONE = "none", _("None")
    SITE = "site", _("Notification")
    EMAIL = "email", _("Email")
    BOTH = "both", _("Notification and email")

    @property
    def includes_site(self) -> bool:
        """Whether this choice writes an in-app notification."""
        return self in (DeliveryPreference.SITE, DeliveryPreference.BOTH)

    @property
    def includes_email(self) -> bool:
        """Whether this choice sends an email."""
        return self in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH)
