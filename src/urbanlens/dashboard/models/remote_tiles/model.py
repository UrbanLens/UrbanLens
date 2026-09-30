"""Map tiles from another host, kept on this site so viewers never fetch them from it and they outlive its copy."""

from __future__ import annotations

from django.db.models import CASCADE, BooleanField, CharField, DateTimeField, FileField, ForeignKey, PositiveIntegerField, PositiveSmallIntegerField, TextField, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.remote_tiles.queryset import RemoteTileManager, RemoteTileSourceManager
from urbanlens.dashboard.services.media.access import declares_media_family


class RemoteTileSource(abstract.DashboardModel):
    """One foreign XYZ tile template an overlay draws through this site.

    A row exists once an overlay was given the template; that is what lets the tile endpoint fetch from it, so the
    endpoint cannot be pointed at an arbitrary host.
    """

    template_digest = CharField(max_length=64, unique=True, editable=False, help_text="SHA-256 of template.")
    template = TextField(help_text="The XYZ template at its host, as it was given.")
    provider = CharField(max_length=64, blank=True, default="", help_text="Where the template came from, e.g. an archive import.")
    kept_tiles = PositiveIntegerField(default=0)

    objects = RemoteTileSourceManager()

    def __str__(self) -> str:
        return f"RemoteTileSource({self.provider}, {self.template[:60]})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_remote_tile_sources"


@declares_media_family("remote_tiles")
def remote_tile_path(instance: RemoteTile, filename: str) -> str:
    """Where a kept tile lives. Migrations serialize ``upload_to`` by reference."""
    digest = instance.source.template_digest
    return f"remote_tiles/{digest[:2]}/{digest}/{instance.z}/{instance.x}/{filename}"


class RemoteTile(abstract.DashboardModel):
    """One tile of a :class:`RemoteTileSource`: kept, known not to exist, or failing to download."""

    source = ForeignKey(RemoteTileSource, on_delete=CASCADE, related_name="tiles")
    z = PositiveSmallIntegerField()
    x = PositiveIntegerField()
    y = PositiveIntegerField()
    file = FileField(upload_to=remote_tile_path, max_length=255, blank=True, default="")
    content_type = CharField(max_length=50, blank=True, default="")
    absent = BooleanField(default=False, help_text="The host answered that it has no tile here.")
    fetched_at = DateTimeField(null=True, blank=True)
    failed_attempts = PositiveSmallIntegerField(default=0)
    last_failed_at = DateTimeField(null=True, blank=True)

    objects = RemoteTileManager()

    def __str__(self) -> str:
        return f"RemoteTile({self.source_id}, {self.z}/{self.x}/{self.y})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_remote_tiles"
        constraints = [UniqueConstraint(fields=["source", "z", "x", "y"], name="remote_tile_one_row_per_coordinate")]
