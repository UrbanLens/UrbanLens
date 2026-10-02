"""A browser-viewable rendering of a file an in-app REData proxy serves, kept so the file is downloaded and decoded once."""

from __future__ import annotations

from django.db.models import CharField, FileField, PositiveIntegerField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.proxied_media_render.queryset import ProxiedMediaRenderManager
from urbanlens.dashboard.services.media.access import declares_media_family


@declares_media_family("proxied_renders")
def proxied_media_render_path(instance: ProxiedMediaRender, filename: str) -> str:
    """Where a kept rendering lives, fanned out by digest. Migrations serialize ``upload_to`` by reference."""
    return f"proxied_renders/{instance.render_digest[:2]}/{filename}"


class ProxiedMediaRender(abstract.DashboardModel):
    """One size of one proxied file (a CRIS attachment, a LoopNet photo), as an image a browser can show.

    Rows exist only for renderings that succeeded; a file that cannot be rendered is remembered in the cache instead,
    so a later fix to the renderer gets another try.
    """

    render_digest = CharField(max_length=64, unique=True, editable=False, help_text="SHA-256 of the proxy's cache key and the size.")
    source_key = CharField(max_length=255, help_text="The proxy's cache key for the original file.")
    size = CharField(max_length=16, help_text="Which rendering: a gallery tile's, or the lightbox's.")
    file = FileField(upload_to=proxied_media_render_path, max_length=255)
    content_type = CharField(max_length=50)
    file_size = PositiveIntegerField()

    objects = ProxiedMediaRenderManager()

    def __str__(self) -> str:
        return f"ProxiedMediaRender({self.size}, {self.source_key[:60]})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_proxied_media_renders"
