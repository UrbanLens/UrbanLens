"""Wiki queryset and manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def _record_wiki_created(wiki: Wiki, profile: Profile) -> None:
    """Record the reputation event for promoting a place to a real wiki.

    Called from both claim branches rather than left to a post_save
    subscription: the promotion branch is a queryset ``update()``, which fires
    no signal, and the create branch's signal would fire before
    ``officially_created`` is meaningful on a draft. A rule that only watches
    saves records this at an arbitrary later time, by whoever happened to save
    next.
    """
    from urbanlens.dashboard.services.reputation.scoring import record_event

    record_event(profile, "wiki_created", target=wiki, wiki=wiki)


class WikiQuerySet(abstract.VersionedQuerySet, abstract.PublicDashboardQuerySet):
    """QuerySet for Wiki - the community-editable half of the place model.

    Filters here operate on community data (name, labels). For address/geo
    filtering use LocationQuerySet; for per-user filtering use PinQuerySet.
    """

    def official(self) -> Self:
        """Return only wikis that user-facing surfaces are allowed to know exist.

        Every pinned Location gets a background draft (see
        ``tasks.ensure_draft_wiki_for_location``) so enrichment has somewhere to
        write before anyone clicks "Create Wiki", which means a row existing
        says only "somebody pinned this". ``Wiki.officially_created`` is what
        separates the two, and its contract is that a draft must read as "no
        wiki exists yet" *everywhere*.

        That contract was enforced one call site at a time, and several missed
        it. Prefer this scope over spelling the filter out again, so a surface
        added later inherits the rule instead of having to remember it.
        """
        return self.filter(officially_created=True)

    def root_wikis(self) -> Self:
        """Return only top-level wikis (excludes child wikis)."""
        return self.filter(parent_wiki__isnull=True)

    def child_wikis(self) -> Self:
        """Return only child wikis (community sub-markers nested under a parent wiki)."""
        return self.filter(parent_wiki__isnull=False)

    def by_category(self, category):
        return self.filter(labels__name=category, labels__kind=KIND_CATEGORY)

    def by_name(self, name):
        return self.filter(name__icontains=name)

    def by_created_year(self, year):
        return self.filter(created__year=year)

    def by_updated_year(self, year):
        return self.filter(updated__year=year)

    def filter_by_criteria(self, criteria):
        query = Q()
        if criteria.get("date_added"):
            query &= Q(created__date=criteria["date_added"])
        if criteria.get("tags"):
            tags = criteria["tags"].split(",")
            for tag in tags:
                query &= Q(labels__name__in=[tag], labels__kind=KIND_TAG)
        return self.filter(query)


class WikiManager(abstract.PublicDashboardManager.from_queryset(WikiQuerySet)):
    """Manager for Wiki.

    A wiki page becomes user-visible either when a user explicitly creates it
    (the "Create community wiki" button on the pin detail page, via
    ``WikiCreationService`` -> ``claim_for_location``), or - since a Wiki row
    can now also exist as an unofficial *draft*, auto-created in the
    background by ``tasks.ensure_draft_wiki_for_location`` so enrichment has
    something to populate ahead of time - when a user's create action
    promotes an existing draft. See ``Wiki.officially_created``.

    Use ``get_for_location`` for the common "does this place have a
    user-visible wiki?" lookup; it deliberately ignores draft rows.
    """

    def existing_for_location(self, location: Location | None) -> Wiki | None:
        """The Wiki describing what this Location stands on, draft or official.

        Checks the Location's own row first, then the *place* it resolved onto.
        The second lookup is the dedup that matters: two people pinning
        opposite ends of one property get two Locations, and without it they
        would get two community pages for one real-world thing.

        Args:
            location: The shared Location to look up (None-safe).

        Returns:
            The Wiki, or None.
        """
        if location is None:
            return None
        try:
            return location.wiki
        except ObjectDoesNotExist:
            pass
        if location.place_id is None:
            return None
        return self.filter(place_id=location.place_id).first()

    def get_for_location(self, location: Location | None) -> Wiki | None:
        """Return the location's *official* Wiki, or None when it has no visible wiki yet.

        A draft (``officially_created=False``) counts as "no wiki" here - this
        is the check every user- and API-facing surface should use to decide
        whether to show wiki content vs. a "Create Wiki" affordance.

        Args:
            location: The shared Location to look up (None-safe).

        Returns:
            The official Wiki, or None.
        """
        wiki = self.existing_for_location(location)
        return wiki if (wiki is not None and wiki.officially_created) else None

    def _placeholder_name(self, location: Location) -> str:
        """The fallback wiki name for a location with no official name."""
        return f"Unnamed Location in {location.area_label}" if location.area_label else "Unnamed Location"

    def get_or_create_for_location(self, location: Location, defaults: dict | None = None) -> tuple[Wiki, bool]:
        """Return the Wiki for a Location, creating it (as official) if absent.

        General-purpose get-or-create; a freshly created row defaults to
        ``officially_created=True`` (see the field's own docstring). Most
        callers wanting the "Create Wiki" button's semantics - including
        correctly promoting a pre-existing draft - should use
        ``claim_for_location`` instead. This must never be called as a lazy
        side effect of viewing or editing other content - use
        ``get_for_location`` there.

        Args:
            location: The shared Location to attach the wiki to.
            defaults: Optional field overrides for the created Wiki. A ``name``
                key wins over the location's ``official_name`` fallback.

        Returns:
            Tuple of (Wiki, created).
        """
        if (existing := self.existing_for_location(location)) is not None:
            return existing, False

        defaults = dict(defaults or {})
        name = defaults.pop("name", None) or location.official_name or self._placeholder_name(location)
        wiki = self.create(location=location, place_id=location.place_id, name=name, **defaults)
        return wiki, True

    def get_or_create_draft_for_location(self, location: Location) -> tuple[Wiki, bool]:
        """Return the Wiki for a Location, creating an unofficial draft if absent.

        Used only by ``tasks.ensure_draft_wiki_for_location``, queued whenever
        a pin gets a shared Location, so background enrichment (Google place
        linking, name resolution, boundary generation, Wikipedia seeding) has
        a row to populate before any user has clicked "Create Wiki". The
        draft stays invisible to users (see ``get_for_location``) until
        ``claim_for_location`` promotes it.

        Args:
            location: The shared Location to attach the draft wiki to.

        Returns:
            Tuple of (Wiki, created).
        """
        if (existing := self.existing_for_location(location)) is not None:
            return existing, False

        wiki = self.create(location=location, place_id=location.place_id, name=location.official_name or self._placeholder_name(location), officially_created=False)
        return wiki, True

    def claim_for_location(self, location: Location, profile: Profile) -> tuple[Wiki, bool]:
        """Create or officially claim the Wiki for a Location on behalf of ``profile``.

        Backs the "Create community wiki" button (``WikiCreationService``).
        Three cases:

        - No Wiki row exists yet: create one, official, attributed to
          ``profile``.
        - An unofficial draft exists (auto-created in the background ahead of
          any user action): promote it - flip ``officially_created`` and
          attribute it to ``profile``, since nobody has claimed it yet.
        - An already-official Wiki exists: return it unchanged - never
          re-attribute or re-seed someone else's wiki.

        Args:
            location: The shared Location to claim a wiki for.
            profile: The profile performing the explicit create action.

        Returns:
            Tuple of (Wiki, newly_official). ``newly_official`` is True for
            the first two cases - callers should treat this like a fresh
            creation (seed content, enqueue enrichment) - and False for the
            third.
        """
        wiki = self.existing_for_location(location)
        if wiki is None:
            wiki = self.create(
                location=location,
                place_id=location.place_id,
                name=location.official_name or self._placeholder_name(location),
                created_by=profile,
                officially_created=True,
            )
            _record_wiki_created(wiki, profile)
            return wiki, True

        if wiki.officially_created:
            return wiki, False

        self.filter(pk=wiki.pk).update(officially_created=True, created_by=profile)
        wiki.officially_created = True
        wiki.created_by = profile
        _record_wiki_created(wiki, profile)
        return wiki, True
