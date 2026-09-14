"""Which pins and wikis a photo is attached to.
``Image`` carries a single ``pin`` and a single ``wiki`` foreign key, which says a photo belongs to at most one of each.
Deleting a pin takes its own attachment rows and nothing else - the ``Image`` survives, along with every other thing citing it - so a photo cited by a floorplan cannot be destroyed by tidying up somewhere unrelated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CheckConstraint, ForeignKey, Index, Q, UniqueConstraint

from urbanlens.dashboard.models import abstract


class ImageAttachmentQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for ImageAttachment models."""


class ImageAttachmentManager(abstract.DashboardManager.from_queryset(ImageAttachmentQuerySet)):
    """Custom query manager for ImageAttachment models."""


class ImageAttachment(abstract.DashboardModel):
    """One photo's attachment to one pin, or to one wiki.
    Exactly one of ``pin``/``wiki`` is set: a row is an attachment to a specific thing, and "attached to both" is two rows, which is what makes counting references to a photo a single query rather than a special case per owner.

    Attributes:
        image: The photo.
        pin: The pin it is attached to, when this is a pin attachment.
        wiki: The wiki it is attached to, when this is a wiki attachment.
        added_by: Who attached it. Kept because a wiki is contributed to by
            several people, and "whose contribution was this" is not answerable
            from the wiki itself.
    """

    image = ForeignKey("dashboard.Image", on_delete=CASCADE, related_name="attachments")
    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="image_attachments")
    wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, null=True, blank=True, related_name="image_attachments")
    added_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="image_attachments_added")

    if TYPE_CHECKING:
        image_id: int
        pin_id: int | None
        wiki_id: int | None
        added_by_id: int | None

    objects = ImageAttachmentManager()

    def __str__(self) -> str:
        return f"{self.image_id}->{'pin ' + str(self.pin_id) if self.pin_id else 'wiki ' + str(self.wiki_id)}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_image_attachments"
        constraints = [
            # Enforced rather than documented: a row with both set would be counted twice by every
            # reference count, and a row with neither attaches the photo to nothing while still
            # keeping it alive - which is the one thing that would defeat collecting unreferenced
            # photos.
            CheckConstraint(
                condition=(Q(pin__isnull=False) & Q(wiki__isnull=True)) | (Q(pin__isnull=True) & Q(wiki__isnull=False)),
                name="ck_image_attachment_one_owner",
            ),
            UniqueConstraint(fields=["image", "pin"], condition=Q(pin__isnull=False), name="uq_image_attachment_pin"),
            UniqueConstraint(fields=["image", "wiki"], condition=Q(wiki__isnull=False), name="uq_image_attachment_wiki"),
        ]
        indexes = [
            Index(fields=["pin"], name="idxdb_imgatt_pin"),
            Index(fields=["wiki"], name="idxdb_imgatt_wiki"),
        ]
