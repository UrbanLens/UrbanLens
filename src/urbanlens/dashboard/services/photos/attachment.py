"""Attaching a photo to the pins and wikis it belongs to, and collecting it when nothing does.

A photo is not owned by one place. A picture of a building belongs to the
building's pin and, because child pins are a feature, to the parcel pin above it;
a photo contributed to a community wiki belongs to that wiki and to the
contributor's own pin. :class:`~urbanlens.dashboard.models.images.attachment.ImageAttachment`
holds those as rows so there can be any number of them, and so removing one
removes exactly one.

The counterpart is collection. Detaching the last thing that pointed at a photo
should not leave the bytes behind forever, but "nothing points at it" is a much
narrower condition than it looks, and getting it wrong deletes somebody's
library.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import ImageSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Sources whose rows exist only to back a citation: bytes fetched on a user's
#: behalf because something referred to them. An upload is not in this set, and
#: that is the whole point - a photo somebody uploaded is their Memories library
#: whether or not it is currently attached to anything, and collecting it because
#: the last attachment went away would delete their own picture out from under
#: them.
COLLECTABLE_SOURCES = frozenset({ImageSource.LINKED_URL})


def attach_to_pin(image: Image, pin: Pin, *, added_by: Profile | None = None) -> ImageAttachment:
    """Attach a photo to a pin, if it is not attached already.

    Args:
        image: The photo.
        pin: The pin it belongs to.
        added_by: Who attached it, for wikis and shared plans where that is not
            inferable from the pin.

    Returns:
        The attachment row, existing or new.
    """
    attachment, created = ImageAttachment.objects.get_or_create(image=image, pin=pin, defaults={"added_by": added_by})
    if created:
        logger.debug("Attached image %s to pin %s", image.pk, pin.pk)
    return attachment


def attach_to_wiki(image: Image, wiki: Wiki, *, added_by: Profile | None = None) -> ImageAttachment:
    """Attach a photo to a wiki, if it is not attached already.

    Args:
        image: The photo.
        wiki: The wiki it is contributed to.
        added_by: The contributing profile.

    Returns:
        The attachment row, existing or new.
    """
    attachment, created = ImageAttachment.objects.get_or_create(image=image, wiki=wiki, defaults={"added_by": added_by})
    if created:
        logger.debug("Attached image %s to wiki %s", image.pk, wiki.pk)
    return attachment


def reference_count(image: Image) -> int:
    """How many things currently point at this photo.

    Counts every kind of citation, not only attachments: a floorplan reference
    keeps a photo alive exactly as much as a pin does, which is the reason
    ``FloorplanReference.image`` is SET_NULL rather than CASCADE.

    Args:
        image: The photo.

    Returns:
        The number of references. Zero means nothing would notice it going.
    """
    return (
        ImageAttachment.objects.filter(image=image).count()
        + image.floorplan_references.count()
        + image.album_memberships.count()
        # The columns that predate ImageAttachment, still written and still read.
        + sum(1 for owner in (image.pin_id, image.wiki_id, image.location_id, image.safety_checkin_id, image.visit_id, image.direct_message_id, image.pin_suggestion_id) if owner is not None)
    )


def collect_if_unreferenced(image: Image) -> bool:
    """Delete a fetched photo once nothing refers to it any more.

    Deliberately narrow. Only rows in :data:`COLLECTABLE_SOURCES` are eligible -
    bytes fetched to back a citation, which have no independent reason to exist -
    and only when every kind of reference is gone. An uploaded photo is never
    collected here however unattached it is, because it is its owner's library.

    Args:
        image: The photo to consider.

    Returns:
        True if it was deleted.
    """
    if image.source not in COLLECTABLE_SOURCES:
        return False
    if reference_count(image) > 0:
        return False
    logger.info("Collecting unreferenced fetched image %s (%s)", image.pk, image.source_url or "no source url")
    image.delete()
    return True
