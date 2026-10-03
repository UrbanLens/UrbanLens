"""LocationSlugHistory model - slugs a Location's wiki URLs used before its current one."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, ForeignKey, SlugField

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


class LocationSlugHistory(abstract.DashboardModel):
    """A slug a Location was addressed by before, so links to it keep resolving.

    A former slug is never minted again for another Location, and resolves only through the same access gate as the
    current one.
    """

    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="slug_history")
    slug = SlugField(max_length=255, unique=True)

    if TYPE_CHECKING:
        location_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_location_slug_history"
        verbose_name_plural = "location slug history"

    def __str__(self) -> str:
        return self.slug

    @classmethod
    def record(cls, location: Location, former: str | None, current: str | None) -> None:
        """Remember ``former`` for ``location`` after its slug became ``current``.

        The Location's own uuid needs no row, since it always resolves; a slug the Location takes back is dropped.

        Args:
            location: The Location whose slug changed.
            former: The slug it had.
            current: The slug it has now.
        """
        if current:
            cls.objects.filter(location=location, slug=current).delete()
        if former and former != current and former != str(location.uuid):
            cls.objects.get_or_create(slug=former, defaults={"location": location})
