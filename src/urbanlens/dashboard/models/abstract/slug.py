"""Abstract mixin for auto-generated URL slugs."""

from __future__ import annotations

from uuid import uuid4

from django.db import transaction
from django.db.models import UUIDField
from django.db.models.fields import SlugField
from django.utils.text import slugify

from urbanlens.dashboard.models.abstract.model import Model

_DEFAULT_MAX_LENGTH = 255


class HasSlug(Model):
    """Mixin that auto-generates unique URL slugs.

    Concrete models must implement ``_slugify_base()`` returning the raw text
    from which the slug is derived, and may override ``_slugify_qs()`` to
    scope uniqueness checks (e.g. per-user instead of globally).

    The ``slug`` field intentionally has no ``unique=True`` here - models that
    need global uniqueness (Location, Profile) should override the field.
    Models that need scoped uniqueness (Pin, unique per-profile) rely on a
    ``UniqueConstraint`` in their Meta instead.
    """

    # Public-facing identifier. Non-sequential so users cannot enumerate records.
    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    # URL slug - uniqueness constraints are set on each concrete model.
    slug = SlugField(max_length=_DEFAULT_MAX_LENGTH, null=True, blank=True)

    def _slug_max_length(self) -> int:
        """Return the max_length of the slug field as declared on this model."""
        try:
            return self.__class__._meta.get_field("slug").max_length or _DEFAULT_MAX_LENGTH  # noqa: SLF001
        except Exception:
            return _DEFAULT_MAX_LENGTH

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

        with transaction.atomic():
            qs = self._slugify_qs()
            # Reserve room for the longest suffix we might need.
            # Start with the full (possibly truncated) base and walk up.
            base = raw_base[:max_len]
            candidate = base
            n = 2
            while qs.filter(slug=candidate).exists():
                suffix = f"-{n}"
                candidate = raw_base[: max_len - len(suffix)] + suffix
                n += 1

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
            self.slug = self._generate_slug()
            if self.pk:
                self.save(update_fields=["slug"])
        return self.slug

    def regenerate_slug(self) -> str:
        """Force a new slug to be generated and persisted, replacing any existing one.

        Useful when the source field (e.g. name or username) has changed and
        the old slug is stale. Always saves when ``self.pk`` is set.

        Returns:
            The new slug.
        """
        self.slug = self._generate_slug()
        if self.pk:
            self.save(update_fields=["slug"])
        return self.slug

    class Meta(Model.Meta):
        abstract = True
