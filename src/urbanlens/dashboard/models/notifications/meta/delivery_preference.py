"""Delivery preference choices for notification preferences."""
from __future__ import annotations

from django.utils.translation import gettext as _

from urbanlens.dashboard.models.abstract.choices import TextChoices


class DeliveryPreference(TextChoices):
    """How a user wants to receive a given notification type."""

    NONE = "none", _("Do not deliver.")
    SITE = "site", _("In-site notification only.")
    EMAIL = "email", _("Email only.")
    BOTH = "both", _("In-site and email.")
