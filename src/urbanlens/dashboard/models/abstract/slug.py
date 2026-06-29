"""Abstract mixin for auto-generated URL slugs."""

from __future__ import annotations

from uuid import uuid4

from django.db.models import UUIDField
from django.db.models.fields import SlugField
from django.utils.text import slugify

from urbanlens.dashboard.models.abstract.model import Model


class HasSlug(Model):
    """Mixin that auto-generates unique URL slugs.

    Concrete models must define a ``slug`` field and implement
    `_slug_source_text`, or override `_generate_slug` /
    `ensure_slug` for fully custom behaviour.
    """
    # Public-facing identifier. Non-sequential so users cannot infer location counts
    # or enumerate other locations from a known URL.
    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    # URL slug - globally unique. Auto-generated from name on first save.
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)
        
    def _slugify_qs(self):
        qs = self.__class__.objects.all()
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs
        
    def _slugify_base(self) -> str:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _slugify_base() or override _generate_slug().",
        )

    def _generate_slug(self) -> str:
        """Derive a slug that is unique."""
        base = slugify(self._slugify_base())[:255]
        candidate = base
        n = 2
        qs = self._slugify_qs()
        while qs.filter(slug=candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def ensure_slug(self) -> str:
        """Ensure this instance has a URL slug, generating one if needed.

        Returns:
            The instance slug (never empty).
        """
        if not self.slug:
            self.slug = self._generate_slug()
            self.save(update_fields=["slug"])
        return self.slug

    class Meta(Model.Meta):
        abstract = True
