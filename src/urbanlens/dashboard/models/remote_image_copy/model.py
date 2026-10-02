"""A third-party image kept on this site, so viewers never fetch it from its provider and it outlives the provider's copy."""

from __future__ import annotations

from django.db.models import CharField, DateTimeField, FileField, PositiveIntegerField, PositiveSmallIntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.remote_image_copy.queryset import RemoteImageCopyManager
from urbanlens.dashboard.services.media.access import declares_media_family


@declares_media_family("remote_copies")
def remote_image_copy_path(instance: RemoteImageCopy, filename: str) -> str:
    """Where a stored copy lives, fanned out by digest. Migrations serialize ``upload_to`` by reference."""
    return f"remote_copies/{instance.url_digest[:2]}/{filename}"


class RemoteImageCopy(abstract.DashboardModel):
    """One remote image, keyed by its URL, with where it came from.

    A row exists once this site has emitted a link to its copy; that is what lets the copy endpoint fetch the source,
    so it cannot be pointed at an arbitrary URL. It has no expiry: once stored, the source is never fetched again.
    """

    url_digest = CharField(max_length=64, unique=True, editable=False, help_text="SHA-256 of source_url.")
    source_url = TextField(help_text="The image's address at its provider.")
    edition = CharField(max_length=16, blank=True, default="", help_text="Which edition of a changing picture this is, e.g. the month of a current-imagery export.")
    provider = CharField(max_length=64, blank=True, default="", help_text="Which feature or provider the image came from.")
    page_url = TextField(blank=True, default="", help_text="The provider's page for the image, when known.")
    file = FileField(upload_to=remote_image_copy_path, max_length=255, blank=True, default="")
    thumb_file = FileField(upload_to=remote_image_copy_path, max_length=255, blank=True, default="", help_text="A gallery tile's size, made from the same download when the copy is larger.")
    content_type = CharField(max_length=50, blank=True, default="")
    file_size = PositiveIntegerField(null=True, blank=True)
    checksum = CharField(max_length=64, blank=True, default="", help_text="SHA-256 of the stored file.")
    fetched_at = DateTimeField(null=True, blank=True, help_text="When the source was downloaded and stored.")
    failed_attempts = PositiveSmallIntegerField(default=0)
    last_failed_at = DateTimeField(null=True, blank=True)

    objects = RemoteImageCopyManager()

    def __str__(self) -> str:
        return f"RemoteImageCopy({self.provider}, {self.source_url[:60]})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_remote_image_copies"
