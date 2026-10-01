"""Importing this module registers every concrete undo handler."""

from urbanlens.dashboard.services.undo.handlers import (  # noqa: F401
    label,
    label_membership,
    markup_map,
    photo_mutation,
    pin,
    pin_list,
    pin_mutation,
    safety_checkin,
    saved_filter,
    trip,
    wiki,
    wiki_mutation,
)
