# Generic imports
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING
from uuid import uuid4

# Django Imports
from django.db import IntegrityError, models as django_models, transaction
from django.db.models import UUIDField
from django.db.models.fields import SlugField
from django.utils.text import slugify

# App Imports
from urbanlens.dashboard.models.abstract.queryset import DashboardManager, FrontendDashboardManager, PublicDashboardManager

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SLUG_LENGTH = 255

class DashboardModel(django_models.Model):
    """
    A base model that all other models in this app inherit from.
    """

    created = django_models.DateTimeField(auto_now_add=True)
    updated = django_models.DateTimeField(auto_now=True)
    objects: DashboardManager = DashboardManager()

    if TYPE_CHECKING:
        id: int

    class Meta:
        """
        Metadata about this model (such as the table name)

        Attributes:
            db_table (str):
                The name of the table in the DB
            unique_together (list of str):
                A list of attributes which form unique keys
            indexes (list of Index):
                A list of indexes to create on the table

        """

        abstract = True
        app_label = "dashboard"
        
class FrontendDashboardModel(DashboardModel):
    """
    A base model that has the capability of being sent to the frontend.
    """
    uuid = django_models.UUIDField(default=uuid4, unique=True, editable=False)
    objects: FrontendDashboardManager = FrontendDashboardManager()

    class Meta(DashboardModel.Meta):
        abstract = True
        
        
class PublicDashboardModel(FrontendDashboardModel):
    """
    A base model that users can visit on the frontend.
    
    Concrete models must implement ``_slugify_base()`` returning the raw text
    from which the slug is derived, and may override ``_slugify_qs()`` to
    scope uniqueness checks (e.g. per-user instead of globally).

    The ``slug`` field intentionally has no ``unique=True`` here - models that
    need global uniqueness (Location, Profile) should override the field.
    Models that need scoped uniqueness (Pin, unique per-profile) rely on a
    ``UniqueConstraint`` in their Meta instead.
    """

    # URL slug - uniqueness constraints are set on each concrete model.
    slug = django_models.SlugField(max_length=_DEFAULT_MAX_SLUG_LENGTH, null=True, blank=True)

    objects: PublicDashboardManager = PublicDashboardManager()
    
    def _slug_max_length(self) -> int:
        """Return the max_length of the slug field as declared on this model."""
        try:
            return self.__class__._meta.get_field("slug").max_length or _DEFAULT_MAX_SLUG_LENGTH  # noqa: SLF001
        except Exception:
            return _DEFAULT_MAX_SLUG_LENGTH

    def _slugify_qs(self):
        """Return the queryset used to check for slug uniqueness.

        Override to scope uniqueness checks (e.g. ``filter(profile=self.profile)``).
        """
        qs = self.__class__.objects.all()
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

    def _slugify_base(self) -> str:
        """Return the raw text from which the slug is derived.

        Must be overridden by every concrete model. Should return a non-empty
        fallback string (e.g. ``self.name or "item"``).
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _slugify_base().",
        )

    def _generate_slug(self) -> str:
        """Derive a unique slug for this instance.

        Runs inside an atomic block so concurrent writers see each other's
        reservations and cannot claim the same candidate simultaneously.
        Automatically truncates the base so appending a numeric suffix never
        exceeds the field's declared ``max_length``.
        """
        max_len = self._slug_max_length()
        raw_base = slugify(self._slugify_base()) or "item"

        qs = self._slugify_qs()
        # Reserve room for the longest suffix we might need.
        # Start with the full (possibly truncated) base and walk up.
        base = raw_base[:max_len]
        candidate = base
        while qs.filter(slug=candidate).exists():
            # Not used for cryptographic purposes
            n = random.randint(2, 90_000) # noqa: S311
            suffix = f"-{n}"
            candidate = raw_base[: max_len - len(suffix)] + suffix

        return candidate

    def ensure_slug(self) -> str:
        """Ensure this instance has a URL slug, generating one if needed.

        Persists the slug immediately when the instance already exists in the
        database (``self.pk`` is set). For unsaved instances, only sets the
        attribute - the caller is responsible for saving.

        Returns:
            The instance slug (never empty).
        """
        if not self.slug:
            self.regenerate_slug()
        return self.slug

    def regenerate_slug(self) -> str:
        """Force a new slug to be generated and persisted, replacing any existing one.

        Useful when the source field (e.g. name or username) has changed and
        the old slug is stale. Always saves when ``self.pk`` is set.

        Returns:
            The new slug.
        """
        # Handle race condition for slug creation
        for _ in range(20):
            try:
                self.slug = self._generate_slug()
                if self.pk:
                    self.save(update_fields=["slug"])
                break
            except IntegrityError as e:
                if "duplicate key value violates unique constraint" in str(e):
                    continue
                raise
        return self.slug
    
    def save(self, *args, **kwargs) -> None:
        """Auto-generate a unique slug from the username if not already set."""
        self.ensure_slug()
        # TODO: This could result in 2 saves
        super().save(*args, **kwargs)

    class Meta(FrontendDashboardModel.Meta):
        abstract = True
