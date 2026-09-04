"""QuerySet and Manager for the Image model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Case, IntegerField, Q, Sum, Value, When

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


def _own_contribution_q() -> Q:
    """Build the ORM form of ``Image.is_own_contribution``.

    Three conjuncts, each ruling out a real row shape that ``source`` alone
    misreads:

    * ``profile`` is set. ``services.photos.photo_enrichment`` writes
      profile-less rows - Google Maps/Street View/Satellite imagery fetched for
      a place, belonging to nobody.
    * ``source`` names a personal library (see
      ``ImageSource.personal_library``). This is what excludes a photograph the
      profile merely up-voted, and ``LINKED_URL`` bytes fetched because a page
      referred to them.
    * No ``media_source_key``. Flickr is both a connected account and a Media
      gallery panel, and ``media_materialize`` translates an unrecognised panel
      key to ``UPLOAD`` - so a materialised row can carry a personal-library
      value in ``source`` and must still be excluded.

    Migration 0030 wrote ``""`` rather than NULL, so both spellings mean
    "absent" and this has to say so. Normalising the column to NULL would let
    that half collapse to a plain ``isnull`` test.

    Returns:
        A ``Q`` matching rows whose ``profile`` is the photographer.
    """
    from urbanlens.dashboard.models.images.model import ImageSource

    return Q(profile__isnull=False) & Q(source__in=ImageSource.personal_library()) & (Q(media_source_key__isnull=True) | Q(media_source_key=""))


#: Instance attributes :func:`prime_viewer_scope` fills in, and
#: ``ImageQuerySet._viewer_scoped`` reads.
_FRIEND_IDS_ATTR = "_ul_visible_friend_ids"
_PINNED_LOCATION_IDS_ATTR = "_ul_visible_pinned_location_ids"
_TRIP_IDS_ATTR = "_ul_visible_trip_ids"


def prime_viewer_scope(profile: Profile) -> None:
    """Resolve a viewer's relationship sets once, for a caller about to reuse them.

    ``visible_to`` is eager: it resolves the viewer's friends, pinned locations,
    trip memberships and reachable wikis before it can build its filter. A page
    that calls it several times for one viewer pays for all four each time -
    album detail does it four times, measured at 60 queries for a 30-photo vault
    album.

    Explicit rather than automatic, and this is the important part: caching on
    first read would change what ``visible_to`` means for every caller,
    including one that creates a pin and then asks about visibility in the same
    breath. Priming says "I am about to ask the same question repeatedly and
    nothing between the asks will change the answer", which is a claim only the
    caller can make.

    The primed values live on the ``Profile`` instance, so they cannot outlive
    the request that loaded it and nothing has to invalidate them. Every caller
    that should share them must be passed the *same* instance.

    Args:
        profile: The viewer whose sets to resolve.
    """
    from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.trips.model import TripMembership
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids_cached

    accepted = FriendshipStatus.ACCEPTED
    friends = set(Friendship.objects.filter(from_profile=profile, status=accepted).values_list("to_profile_id", flat=True)) | set(
        Friendship.objects.filter(to_profile=profile, status=accepted).values_list("from_profile_id", flat=True),
    )
    # setattr rather than direct assignment: these are not declared on Profile,
    # and django-stubs is right to flag that.
    setattr(profile, _FRIEND_IDS_ATTR, friends)
    setattr(profile, _PINNED_LOCATION_IDS_ATTR, set(Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True)))
    setattr(profile, _TRIP_IDS_ATTR, set(TripMembership.objects.trip_ids_for(profile)))
    # Fills the same instance attribute _shared_within_reach_of reads through.
    visible_wiki_location_ids_cached(profile)


def _named_this_viewer(viewer_profile: Profile) -> Q:
    """Containers whose membership *is* the consent, so settings do not apply.

    A direct message and a safety check-in both work by naming one person. The
    owner did not publish to an audience and then filter it; they picked this
    individual, which is the same act ``photo_upload_visibility`` exists to let
    them perform. That setting reads "who can see the photos you upload to
    locations" - a check-in photo is not a location contribution, and a safety
    partner is typically a stranger to the uploader by design, so consulting it
    here denies the photos to exactly the person the feature exists to inform.

    Direct messages never reach this function - ``MediaGateView`` admits the two
    participants earlier, before any queryset filtering.

    Args:
        viewer_profile: The profile doing the looking.

    Returns:
        A ``Q`` matching photos in containers this viewer was named on.
    """
    from urbanlens.dashboard.models.safety.model import SafetyCheckin

    return Q(safety_checkin__in=SafetyCheckin.objects.shared_with(viewer_profile)) | Q(safety_checkin__in=SafetyCheckin.objects.partnered_with(viewer_profile))


def _shared_within_reach_of(viewer_profile: Profile) -> Q:
    """The container gate: shared deliberately, into a wiki this viewer can reach.

    A photo is private until its owner shares it, and being on a wiki is that
    act - somebody put it there deliberately. This is the *first* of two gates,
    not the only one: ``photo_upload_visibility`` then decides which of the
    people who can reach that wiki may actually see it. Both have to say yes.

    *Which* container matters as much as whether. Wiki access is a place-domain
    rule, so reaching one wiki says nothing about any other; asking only
    ``wiki__isnull=False`` let a permissive upload setting carry a photo to
    somebody whose only pin is somewhere else entirely - and denied a safety
    check-in's photos to the partner watching it, since a check-in has no wiki.
    Reachability is asked of the model that owns the container rather than
    restated here.

    Filing a photo under a pin is not sharing. An explicit pin share hands the
    recipient their own row, which they see as its owner rather than through here.
    Direct messages are handled before this gate: sending is itself the consent,
    so ``MediaGateView`` admits the two participants without consulting settings.

    Putting a pin on a shared itinerary is not sharing its photos either. It
    used to be: every member of every trip the pin appeared on reached that
    pin's whole gallery, live, so a photo uploaded months later joined the
    exposure on its own and leaving the trip was the only way out. Adding a
    place to an itinerary says where the group is going - it is not a decision
    about each photo, which is what ``docs/GOALS.md`` requires before one
    person's pin data reaches another. No trip surface ever rendered those
    photos; the grant only widened ``visible_to`` wherever it was called,
    including the media gate.

    Args:
        viewer_profile: The profile doing the looking.

    Returns:
        A ``Q`` matching photos in containers within this viewer's reach.
    """
    # Uses a primed value when a caller has said it is about to resolve this
    # viewer repeatedly (see prime_viewer_scope), and reads fresh otherwise.
    # Never populates: reading through the self-caching variant would make every
    # caller a cacher, and a request that pins a place and then asks what it can
    # see would get the answer from before the pin.
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids, visible_wiki_location_ids_if_primed

    primed = visible_wiki_location_ids_if_primed(viewer_profile)
    location_ids = visible_wiki_location_ids(viewer_profile) if primed is None else primed
    return Q(wiki__location_id__in=location_ids)


class ImageQuerySet(abstract.FrontendDashboardQuerySet):
    def visible_to(self, viewer_profile: Profile | None) -> Self:
        """Filter to images the given viewer is allowed to see.

        Enforces two independent settings:
        - The uploader's ``photo_upload_visibility`` (who can see my photos).
        - The viewer's own ``viewer_photo_filter`` (whose photos I want to see).

        Both are gated behind the photo having been shared into a wiki the viewer
        can actually reach - see :func:`_shared_within_reach_of`. Containers that
        work by naming one person answer separately, since picking somebody is
        itself the consent the settings exist to express - see
        :func:`_named_this_viewer`.

        Images uploaded by the viewer are always included regardless of settings
        (even while ``pending_scan`` - the owner can watch their own upload go
        from "processing" to visible). Everyone else's ``pending_scan`` photos
        are excluded outright: the malware scan has not cleared it, and the
        stored file may still carry the uploader's raw, unstripped bytes - see
        ``Image.pending_scan``. ``authorize_image`` enforces the same rule for a
        direct media-URL fetch; this is what keeps it out of a gallery listing
        in the first place, rather than appearing as a broken image.
        If ``viewer_profile`` is None (anonymous), nothing is returned.

        Unlike an ordinary queryset method this one is **eager**: the
        relationship rules can't be expressed in SQL, so the allowed-uploader
        set is resolved immediately from whatever ``self`` already narrows to.
        Narrow first - ``Image.objects.filter(pk=n).visible_to(p)`` inspects one
        uploader, ``Image.objects.visible_to(p).filter(pk=n)`` inspects every
        uploader on the site for the same answer.
        """
        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        if viewer_profile is None:
            # Nothing. Wiki access is earned by having pinned the place, which a
            # signed-out visitor cannot have done, so no container is in reach -
            # and the most permissive setting is labelled "Anyone (Logged In)",
            # which does not describe them either.
            return self.none()

        # 1. Determine which uploader profiles this viewer is allowed to see photos from,
        #    based on the VIEWER's own photo filter preference.
        viewer_filter = viewer_profile.viewer_photo_filter
        if viewer_filter == VisibilityChoice.NO_ONE:
            # Viewer has opted out of all other users' photos.
            return self.filter(profile=viewer_profile)

        # 2. Start with all images, then restrict by uploader's upload_visibility.
        #    We only have ORM access to the uploader's setting directly; relationship
        #    checks (friends, common pins, etc.) happen per-uploader so we rely on
        #    a Python-level filter over the resulting set when advanced checks are needed.
        #
        #    For scalability we pre-compute the set of allowed uploader IDs.
        allowed_uploader_ids = self._allowed_uploader_ids(viewer_profile, viewer_filter)

        # Both gates, and in this order: the photo must have been shared, and the
        # uploader's setting must admit this viewer. Sharing alone is not enough -
        # that is what the setting is for - and a permissive setting alone is not
        # enough either, which is the half that was missing: a photo filed under a
        # pin was reachable by anyone the setting happened to admit, and the
        # default admits whoever pinned the same place.
        others_visible = Q(pending_scan=False) & (_named_this_viewer(viewer_profile) | (Q(profile_id__in=allowed_uploader_ids) & _shared_within_reach_of(viewer_profile)))
        return self.filter(Q(profile=viewer_profile) | others_visible)

    def _allowed_uploader_ids(self, viewer_profile: Profile, viewer_filter: str) -> set[int]:
        """Return the set of profile IDs whose photos this viewer may see.

        Takes into account both the viewer's filter preference and each
        uploader's own upload-visibility setting.
        """
        from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

        # Scope candidate uploaders to those who actually have an image in
        # *this* queryset (the gallery being rendered), not every uploader on
        # the whole site - keeps the cost proportional to the gallery size.
        uploaders = Profile.objects.filter(pk__in=self.values_list("profile_id", flat=True).distinct()).exclude(pk=viewer_profile.pk).values_list("pk", "photo_upload_visibility")

        viewer_friend_ids = self._get_friend_ids(viewer_profile)
        viewer_loc_ids = self._get_location_ids(viewer_profile)
        viewer_trip_ids = self._get_trip_ids(viewer_profile)

        allowed: set[int] = set()
        for uploader_id, upload_vis in uploaders:
            # a) Uploader's own restriction
            if not self._relationship_allows(upload_vis, uploader_id, viewer_friend_ids, viewer_loc_ids, viewer_trip_ids):
                continue
            # b) Viewer's own filter
            if not self._relationship_allows(viewer_filter, uploader_id, viewer_friend_ids, viewer_loc_ids, viewer_trip_ids):
                continue
            allowed.add(uploader_id)
        return allowed

    # -- Helpers ----------------------------------------------------------------

    @staticmethod
    def _viewer_scoped(profile: Profile, attribute: str, compute: Callable[[], set[int]]) -> set[int]:
        """Read a viewer-scoped id set, using a primed value when there is one.

        These three sets describe the *viewer*, not the queryset, so every
        ``visible_to`` call in one render wants the same answer - and a page can
        make several. Album detail resolves the same visibility four times
        (``visible_album_item_pairs``, ``album_images_page``,
        ``eligible_images_for``, and the picker payload), which cost four copies
        of all three lookups.

        **Opt-in, and never self-populating.** An earlier version cached on
        first read, which silently changed what ``visible_to`` means: a caller
        that creates a pin and then asks about visibility got the answer from
        before the pin existed. That is not a hypothetical -
        ``test_gaining_a_pin_at_the_far_place_grants_the_photo`` is exactly that
        sequence, and its docstring says the gate must track reachability rather
        than anything cached earlier. So a value is used only when a caller has
        explicitly said it is about to resolve the same viewer repeatedly, via
        :func:`prime_viewer_scope`, and the default stays a fresh read.

        Args:
            profile: The viewing profile the set describes.
            attribute: Instance attribute a primed value would be under.
            compute: Builds the set when nothing is primed.

        Returns:
            The viewer's id set.
        """
        primed = getattr(profile, attribute, None)
        return compute() if primed is None else primed

    def _get_friend_ids(self, profile: Profile) -> set[int]:
        def compute() -> set[int]:
            from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

            accepted = FriendshipStatus.ACCEPTED
            return set(Friendship.objects.filter(from_profile=profile, status=accepted).values_list("to_profile_id", flat=True)) | set(Friendship.objects.filter(to_profile=profile, status=accepted).values_list("from_profile_id", flat=True))

        return self._viewer_scoped(profile, _FRIEND_IDS_ATTR, compute)

    def _get_location_ids(self, profile: Profile) -> set[int]:
        def compute() -> set[int]:
            from urbanlens.dashboard.models.pin.model import Pin

            return set(Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True))

        return self._viewer_scoped(profile, _PINNED_LOCATION_IDS_ATTR, compute)

    def _get_trip_ids(self, profile: Profile) -> set[int]:
        def compute() -> set[int]:
            from urbanlens.dashboard.models.trips.model import TripMembership

            return set(TripMembership.objects.trip_ids_for(profile))

        return self._viewer_scoped(profile, _TRIP_IDS_ATTR, compute)

    def _relationship_allows(
        self,
        visibility: str,
        uploader_id: int,
        viewer_friend_ids: set[int],
        viewer_loc_ids: set[int],
        viewer_trip_ids: set[int],
    ) -> bool:
        """Evaluate one VisibilityChoice for a (viewer, uploader) pair.

        Bulk twin of ``Profile.visibility_permits`` - the viewer's friend/
        location/trip id sets are pre-computed once so the per-uploader work
        stays bounded. Accepted friends qualify for every option except
        NO_ONE, matching the per-pair evaluator.

        Args:
            visibility: The VisibilityChoice being evaluated (either side's).
            uploader_id: Profile id of the image uploader.
            viewer_friend_ids: The viewer's accepted-friend profile ids.
            viewer_loc_ids: Location ids the viewer has pinned.
            viewer_trip_ids: Trip ids the viewer is a member of.

        Returns:
            True when the relationship satisfies the visibility requirement.
        """
        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        if visibility == VisibilityChoice.ANYONE:
            return True
        if visibility == VisibilityChoice.NO_ONE:
            return False
        if uploader_id in viewer_friend_ids:
            return True
        if visibility == VisibilityChoice.FRIENDS:
            return False

        def common_pin() -> bool:
            from urbanlens.dashboard.models.pin.model import Pin

            uploader_loc_ids = set(Pin.objects.filter(profile_id=uploader_id, location__isnull=False).values_list("location_id", flat=True))
            return bool(viewer_loc_ids & uploader_loc_ids)

        def common_friend() -> bool:
            return bool(viewer_friend_ids & self._get_friend_ids_by_id(uploader_id))

        def common_trip() -> bool:
            return bool(viewer_trip_ids & self._get_trip_ids_by_id(uploader_id))

        if visibility == VisibilityChoice.COMMON_PIN:
            return common_pin()
        if visibility == VisibilityChoice.COMMON_FRIEND:
            return common_friend()
        if visibility == VisibilityChoice.COMMON_TRIP:
            return common_trip()
        if visibility == VisibilityChoice.ANYTHING_IN_COMMON:
            return common_pin() or common_friend() or common_trip()
        return False

    def _get_friend_ids_by_id(self, profile_id: int) -> set[int]:
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        accepted = FriendshipStatus.ACCEPTED
        return set(Friendship.objects.filter(from_profile_id=profile_id, status=accepted).values_list("to_profile_id", flat=True)) | set(Friendship.objects.filter(to_profile_id=profile_id, status=accepted).values_list("from_profile_id", flat=True))

    def _get_trip_ids_by_id(self, profile_id: int) -> set[int]:
        from urbanlens.dashboard.models.trips.model import TripMembership

        return set(TripMembership.objects.trip_ids_for(profile_id))

    def with_coords(self) -> Self:
        """Filter to images that have GPS and the owner has not hidden from the map."""
        return self.filter(latitude__isnull=False, longitude__isnull=False, map_hidden=False)

    def uploaded_by(self, profile: Profile) -> Self:
        """Filter to images uploaded by a given profile, newest first.

        Args:
            profile: The uploader whose photos to return.

        Returns:
            Filtered queryset ordered by upload time descending.
        """
        return self.filter(profile=profile).order_by("-created")

    def own_contributions(self) -> Self:
        """Filter to rows whose ``profile`` is the photographer, not the up-voter.

        The distinction ``source`` cannot draw: a photo imported from the
        uploader's own Immich server or Google Photos library carries that
        provider's name in ``source`` while still being their own picture,
        whereas a row materialised from somebody else's provider search carries
        the up-voter in ``profile``. Ownership questions - concealment, who may
        withdraw a photo from a wiki, whether a contribution earns reputation -
        all want this rather than ``source == UPLOAD``.

        Fails closed: a future integration that sets no ``media_source_key`` is
        treated as personal, so it is concealed rather than exposed.

        Returns:
            Rows the profile actually contributed.
        """
        return self.filter(_own_contribution_q())

    def provider_media(self) -> Self:
        """Filter to rows materialised from a provider's results - the complement.

        Returns:
            Rows whose ``profile`` is an up-voter rather than the photographer.
        """
        return self.exclude(_own_contribution_q())

    def with_file(self) -> Self:
        """Filter out rows whose stored file is missing.

        ``ImageField`` is non-null with a blank default, so a row can exist with
        no file behind it - the wiki gallery endpoint already excludes these.
        Any template that reaches ``image.url`` on one raises ``ValueError``
        rather than rendering an empty tile, which takes the whole page down.

        Applied in the queryset rather than skipped while rendering so that a
        paginated caller slices the page it actually returns.

        Returns:
            The queryset without file-less rows.
        """
        return self.exclude(image="")

    def photos(self) -> Self:
        """Filter to photos only - Vault Photos' scope, excluding videos/documents."""
        from urbanlens.dashboard.models.images.model import MediaKind

        return self.filter(media_type=MediaKind.PHOTO)

    def documents(self) -> Self:
        """Filter to documents only - Vault Documents' scope."""
        from urbanlens.dashboard.models.images.model import MediaKind

        return self.filter(media_type=MediaKind.DOCUMENT)

    def needs_attention(self, profile: Profile) -> Self:
        """Filter to a profile's unfiled photos awaiting organization.

        These are photos the user uploaded that are not yet tied to a visit and
        have not been dismissed - the pool the Memories "needs attention" queue
        surfaces so they can be confirmed, pinned, or manually logged. Photos
        uploaded directly to a pin/wiki gallery are excluded; only bare
        Vault-page uploads (no pin, no wiki) qualify. Photos staged as scan
        candidates (``pin_suggestion`` set) are also excluded - those belong to
        the Locations review queue, not this one, until their suggestion is
        accepted or rejected.

        Args:
            profile: The uploader whose unfiled photos to return.

        Returns:
            Filtered queryset ordered by upload time descending.
        """
        return self.filter(
            profile=profile,
            visit__isnull=True,
            organize_dismissed=False,
            pin__isnull=True,
            wiki__isnull=True,
            pin_suggestion__isnull=True,
        ).order_by("-created")

    def copied_from_others(self) -> Self:
        """Filter to photos this queryset's owner holds a copy of, but did not author.

        Keyed on ``copied_from_profile`` (set at copy time - see
        ``services.photos.wiki_copy.copy_wiki_photo_to_pin``), not on comparing
        ``profile`` to some other row's uploader: a copy is *owned* by the
        profile that made it (that's what lets it survive the original being
        deleted), so "whose photo is this really" has to be answered by a
        separate field, not by ``profile`` itself. Callers scope to one owner
        first (e.g. ``.filter(profile=viewer)``) - this only narrows further.
        """
        return self.filter(copied_from_profile__isnull=False)


class ImageManager(abstract.FrontendDashboardManager.from_queryset(ImageQuerySet)):
    pass


class MediaRelevanceQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for MediaRelevance models."""

    def for_gallery(self, profile: Profile, location: Location, source: str) -> MediaRelevanceQuerySet:
        """Every relevance mark one profile holds for one provider's gallery at a location.

        Args:
            profile: The marking profile.
            location: The location whose Media gallery is being viewed.
            source: The provider key (e.g. ``"wikimedia"``).

        Returns:
            Matching marks; chain ``.filter(item_key=...)`` for a single item.
        """
        return self.filter(profile=profile, location=location, source=source)

    def vote_scores(self, location: Location, source: str) -> dict[str, int]:
        """Net community vote score per item for one provider's gallery at a location.

        On the community wiki, a relevance mark is read as a vote: every
        ``is_relevant=True`` row counts ``+1`` and every ``is_relevant=False``
        row counts ``-1``, summed across all contributing profiles. Because
        :class:`MediaRelevance` is keyed by Location (not Pin), a relevance
        mark made on any user's Private Pin page for this place is already part
        of this aggregate - that's how a pin-detail thumbs-up "carries over" to
        the wiki with no extra bookkeeping.

        Args:
            location: The location whose Media gallery is being scored.
            source: The provider key (e.g. ``"wikimedia"``, ``"photos"``).

        Returns:
            Mapping of ``item_key`` to its net score. Items with no marks at
            all are simply absent (treat a missing key as ``0``).
        """
        rows = self.filter(location=location, source=source).values("item_key").annotate(score=Sum(Case(When(is_relevant=True, then=Value(1)), default=Value(-1), output_field=IntegerField())))
        return {row["item_key"]: row["score"] or 0 for row in rows}


class MediaRelevanceManager(abstract.DashboardManager.from_queryset(MediaRelevanceQuerySet)):
    """Custom query manager for MediaRelevance models."""
