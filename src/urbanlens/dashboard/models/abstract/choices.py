# Generic imports
from __future__ import annotations

# Django Imports
from django.db import models


class TextChoices(models.TextChoices):
    """
    Override the default TextChoices in django to provide extra functionality without substantively changing its usecase
    """

    @classmethod
    def valid(cls, choice_name: str) -> bool:
        """
        Convenience method
        Determine if a given choice is valid
        """
        return choice_name.lower() in cls.values

    @classmethod
    def invalid(cls, choice_name: str) -> bool:
        """
        Convenience method
        Determine if a given choice is valid
        """
        return choice_name.lower() not in cls.values

    @classmethod
    def get_name(cls, choice: str) -> str | None:
        """
        Convenience method
        Get the name of the choice, given a value
        """
        # Make sure choice is lowercase
        value = choice.lower()

        # Iterate over all choices
        for member in cls:
            # Check values
            if member.value == value:
                # Return the first one found
                return member.name

        # None found
        return None


class SecurityLevel(TextChoices):
    """How prevalent a security feature is at a location."""

    UNKNOWN = "unknown", "Unknown"
    NO = "no", "No"
    SOME = "some", "Some"
    EVERYWHERE = "everywhere", "Everywhere"


class IndoorOutdoor(TextChoices):
    """Whether a location is enclosed, open-air, or a mix of both.

    Shared between :class:`~urbanlens.dashboard.models.pin.model.Pin` and
    :class:`~urbanlens.dashboard.models.wiki.model.Wiki` - each stores its own
    value rather than deferring to Location, mirroring how ``pin_type`` is
    independently classified on both. Groundwork for a future indoor/outdoor
    aware feature; left unset (``None``) until something actually classifies
    it, rather than defaulting to a guess.
    """

    INSIDE = "inside", "Inside"
    OUTSIDE = "outside", "Outside"
    BOTH = "both", "Both"
