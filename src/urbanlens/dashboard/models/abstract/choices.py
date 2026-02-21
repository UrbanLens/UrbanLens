"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    choices.py                                                                                           *
*        Path:    /dashboard/models/abstract/choices.py                                                                *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

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
        option = choice_name.capitalize()
        return option in cls.values

    @classmethod
    def invalid(cls, choice_name: str) -> bool:
        """
        Convenience method
        Determine if a given choice is valid
        """
        option = choice_name.capitalize()
        return option not in cls.values

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
