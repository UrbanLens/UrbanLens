"""CategoryQuerySet and CategoryManager."""

from django.db.models import Manager, QuerySet


class CategoryQuerySet(QuerySet):
    """Custom QuerySet for Category."""

    def ordered(self) -> "CategoryQuerySet":
        """Return categories sorted by -order then name."""
        return self.order_by("-order", "name")

    def with_icon(self) -> "CategoryQuerySet":
        """Return only categories that have an icon set."""
        return self.exclude(icon__isnull=True).exclude(icon="")


class CategoryManager(Manager.from_queryset(CategoryQuerySet)):
    """Manager for Category using CategoryQuerySet."""
