"""Deleting a photo removes its bytes, whichever path deleted it.

Django stopped removing a ``FileField``'s file on row delete in 1.3, and nothing
here replaced it: file cleanup lived in ``delete_stored_file``, which the gallery
delete view calls and a cascade does not. So a photo removed by any other route -
a profile being deleted, a queryset ``.delete()``, an orphan sweep - left its
bytes on disk.

That is worse than untidy. ``MediaGateView`` serves a file whose owning row has
gone to any authenticated user (deliberately: see the "Authenticated media gate"
entry in docs/PROBLEMS.md), so bytes left behind by a delete stayed fetchable by
anyone who had ever been given the URL. "I deleted that photo" has to mean it.

The work still goes through :func:`delete_stored_file` rather than unlinking
here: sharing a pin reuses one storage key across several ``Image`` rows, so the
file goes only when the last row pointing at it does.
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=Image, dispatch_uid="image_delete_stored_file")
def remove_stored_file(sender: type[Image], instance: Image, **kwargs) -> None:
    """Remove the deleted photo's file when no other row still points at it.

    Args:
        sender: The Image class.
        instance: The row that has just been deleted.
        **kwargs: Signal arguments, unused.
    """
    if not instance.image:
        return
    from urbanlens.dashboard.services.media.images import delete_stored_file

    try:
        delete_stored_file(instance)
    except OSError:
        # A file that cannot be removed is a tidy-up failure, not a reason to
        # fail the delete the caller asked for - the row is already gone by the
        # time post_delete runs, so raising here would only mislead.
        logger.warning("Could not remove stored file for deleted image %s", instance.pk, exc_info=True)
