"""QuerySet and Manager for the Image model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Q

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


class ImageQuerySet(abstract.FrontendDashboardQuerySet):
    def visible_to(self, viewer_profile: Profile | None) -> Self:
        """Filter to images the given viewer is allowed to see.

        Enforces two independent settings:
        - The uploader's ``photo_upload_visibility`` (who can see my photos).
        - The viewer's own ``viewer_photo_filter`` (whose photos I want to see).

        Images uploaded by the viewer are always included regardless of settings.
        If ``viewer_profile`` is None (anonymous), only images from users who
        set ``photo_upload_visibility=ANYONE`` are returned.
        """
        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        if viewer_profile is None:
            return self.filter(profile__photo_upload_visibility=VisibilityChoice.ANYONE)

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

        return self.filter(
            Q(profile=viewer_profile) | Q(profile_id__in=allowed_uploader_ids),
        )

    def _allowed_uploader_ids(self, viewer_profile: Profile, viewer_filter: str) -> set[int]:
        """Return the set of profile IDs whose photos this viewer may see.

        Takes into account both the viewer's filter preference and each
        uploader's own upload-visibility setting.
        """
        from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice

        # Fetch all uploaders who have at least one image (excluding the viewer themselves).
        uploaders = (
            Profile.objects.exclude(pk=viewer_profile.pk)
            .filter(
                uploaded_images__isnull=False,
            )
            .distinct()
            .values_list("pk", "photo_upload_visibility")
        )

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

    def _get_friend_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        accepted = FriendshipStatus.ACCEPTED
        return set(Friendship.objects.filter(from_profile=profile, status=accepted).values_list("to_profile_id", flat=True)) | set(Friendship.objects.filter(to_profile=profile, status=accepted).values_list("from_profile_id", flat=True))

    def _get_location_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.pin.model import Pin

        return set(Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True))

    def _get_trip_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.trips.model import TripMembership

        return set(TripMembership.objects.filter(profile=profile).values_list("trip_id", flat=True))

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

        return set(TripMembership.objects.filter(profile_id=profile_id).values_list("trip_id", flat=True))

    def with_coords(self) -> Self:
        """Filter to images that have GPS coordinates (suitable for the map layer)."""
        return self.filter(latitude__isnull=False, longitude__isnull=False)

    def uploaded_by(self, profile: Profile) -> Self:
        """Filter to images uploaded by a given profile, newest first.

        Args:
            profile: The uploader whose photos to return.

        Returns:
            Filtered queryset ordered by upload time descending.
        """
        return self.filter(profile=profile).order_by("-created")

    def needs_attention(self, profile: Profile) -> Self:
        """Filter to a profile's unfiled photos awaiting organization.

        These are photos the user uploaded that are not yet tied to a visit and
        have not been dismissed - the pool the Memories "needs attention" queue
        surfaces so they can be confirmed, pinned, or manually logged. Photos
        uploaded directly to a pin/wiki gallery are excluded; only bare
        Memories-page uploads (no pin, no wiki) qualify. Photos staged as scan
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


class ImageManager(abstract.FrontendDashboardManager.from_queryset(ImageQuerySet)):
    pass
