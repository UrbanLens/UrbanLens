"""QuerySet and Manager for the Image model."""
from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.db.models import Manager, Q, QuerySet

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice


class ImageQuerySet(QuerySet):
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
        uploaders = Profile.objects.exclude(pk=viewer_profile.pk).filter(
            uploaded_images__isnull=False,
        ).distinct().values_list("pk", "photo_upload_visibility")

        viewer_friend_ids = self._get_friend_ids(viewer_profile)
        viewer_loc_ids = self._get_location_ids(viewer_profile)
        viewer_trip_ids = self._get_trip_ids(viewer_profile)

        allowed: set[int] = set()
        for uploader_id, upload_vis in uploaders:
            # a) Uploader's own restriction
            if not self._uploader_allows(
                upload_vis, viewer_profile, uploader_id,
                viewer_friend_ids, viewer_loc_ids, viewer_trip_ids,
            ):
                continue
            # b) Viewer's own filter
            if not self._viewer_allows(
                viewer_filter, viewer_profile, uploader_id,
                viewer_friend_ids, viewer_loc_ids, viewer_trip_ids,
            ):
                continue
            allowed.add(uploader_id)
        return allowed

    # -- Helpers ----------------------------------------------------------------

    def _get_friend_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
        accepted = FriendshipStatus.ACCEPTED
        return (
            set(Friendship.objects.filter(from_profile=profile, status=accepted).values_list("to_profile_id", flat=True))
            | set(Friendship.objects.filter(to_profile=profile, status=accepted).values_list("from_profile_id", flat=True))
        )

    def _get_location_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.pin.model import Pin
        return set(Pin.objects.filter(profile=profile, location__isnull=False).values_list("location_id", flat=True))

    def _get_trip_ids(self, profile: Profile) -> set[int]:
        from urbanlens.dashboard.models.trips.model import TripMembership
        return set(TripMembership.objects.filter(profile=profile).values_list("trip_id", flat=True))

    def _uploader_allows(
        self,
        upload_vis: str,
        viewer: Profile,
        uploader_id: int,
        viewer_friend_ids: set[int],
        viewer_loc_ids: set[int],
        viewer_trip_ids: set[int],
    ) -> bool:
        from urbanlens.dashboard.models.profile.model import VisibilityChoice
        if upload_vis == VisibilityChoice.ANYONE:
            return True
        if upload_vis == VisibilityChoice.NO_ONE:
            return False
        if upload_vis == VisibilityChoice.FRIENDS:
            return uploader_id in viewer_friend_ids
        if upload_vis == VisibilityChoice.COMMON_PIN:
            from urbanlens.dashboard.models.pin.model import Pin
            uploader_loc_ids = set(Pin.objects.filter(profile_id=uploader_id, location__isnull=False).values_list("location_id", flat=True))
            return bool(viewer_loc_ids & uploader_loc_ids)
        if upload_vis == VisibilityChoice.COMMON_FRIEND:
            uploader_friend_ids = self._get_friend_ids_by_id(uploader_id)
            return bool(viewer_friend_ids & uploader_friend_ids)
        if upload_vis == VisibilityChoice.COMMON_TRIP:
            uploader_trip_ids = self._get_trip_ids_by_id(uploader_id)
            return bool(viewer_trip_ids & uploader_trip_ids)
        return False

    def _viewer_allows(
        self,
        viewer_filter: str,
        viewer: Profile,
        uploader_id: int,
        viewer_friend_ids: set[int],
        viewer_loc_ids: set[int],
        viewer_trip_ids: set[int],
    ) -> bool:
        from urbanlens.dashboard.models.profile.model import VisibilityChoice
        if viewer_filter == VisibilityChoice.ANYONE:
            return True
        if viewer_filter == VisibilityChoice.NO_ONE:
            return False
        if viewer_filter == VisibilityChoice.FRIENDS:
            return uploader_id in viewer_friend_ids
        if viewer_filter == VisibilityChoice.COMMON_PIN:
            from urbanlens.dashboard.models.pin.model import Pin
            uploader_loc_ids = set(Pin.objects.filter(profile_id=uploader_id, location__isnull=False).values_list("location_id", flat=True))
            return bool(viewer_loc_ids & uploader_loc_ids)
        if viewer_filter == VisibilityChoice.COMMON_FRIEND:
            uploader_friend_ids = self._get_friend_ids_by_id(uploader_id)
            return bool(viewer_friend_ids & uploader_friend_ids)
        if viewer_filter == VisibilityChoice.COMMON_TRIP:
            uploader_trip_ids = self._get_trip_ids_by_id(uploader_id)
            return bool(viewer_trip_ids & uploader_trip_ids)
        return False

    def _get_friend_ids_by_id(self, profile_id: int) -> set[int]:
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
        accepted = FriendshipStatus.ACCEPTED
        return (
            set(Friendship.objects.filter(from_profile_id=profile_id, status=accepted).values_list("to_profile_id", flat=True))
            | set(Friendship.objects.filter(to_profile_id=profile_id, status=accepted).values_list("from_profile_id", flat=True))
        )

    def _get_trip_ids_by_id(self, profile_id: int) -> set[int]:
        from urbanlens.dashboard.models.trips.model import TripMembership
        return set(TripMembership.objects.filter(profile_id=profile_id).values_list("trip_id", flat=True))

    def with_coords(self) -> Self:
        """Filter to images that have GPS coordinates (suitable for the map layer)."""
        return self.filter(latitude__isnull=False, longitude__isnull=False)


class ImageManager(Manager.from_queryset(ImageQuerySet)):
    pass
