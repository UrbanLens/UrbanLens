"""Deleting a batch of photos must not cost a query per photo.

`_delete_owned_images` calls `delete_stored_file(image, also_deleting=batch_pks)`
once per image, and each of those asks `file_still_referenced` whether any other
row still points at the same stored file - a query carrying the whole batch as
an `exclude(pk__in=...)`. So a batch of N costs O(N) queries each carrying an
N-element parameter list, and `image_ids` is parsed with no cap at all, scoped
only by the requesting profile. Selecting a whole library is one request.

The reference rule itself is load-bearing and is not what this is about: pin
sharing and deduplicated uploads deliberately let several rows share one stored
file, so deleting the file when the first of them goes would silently break
every other copy. Answering that question for a whole batch is one query,
though, not one per row.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

#: Two sizes far enough apart that a per-row query is unmistakable.
_SMALL = 5
_LARGE = 30


class BulkPhotoDeleteScalingTests(TestCase):
    """One more photo in the batch must not cost another round trip."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        location = baker.make(Location, latitude="41.5", longitude="-72.5")
        self.pin = baker.make(Pin, profile=self.profile, location=location)

    def _photos(self, count: int, prefix: str) -> list[Image]:
        return [
            baker.make(
                Image,
                pin=self.pin,
                profile=self.profile,
                media_type=MediaKind.PHOTO,
                # Distinct stored names, so no two rows legitimately share a file
                # and every one of them reaches the reference check.
                image=f"pin_images/{prefix}-{index}.jpg",
            )
            for index in range(count)
        ]

    def _delete(self, images: list[Image]) -> tuple[int, int]:
        """Delete *images* as a batch, returning (statements, total parameters)."""
        from urbanlens.dashboard.controllers.image_gallery import _delete_owned_images

        queryset = Image.objects.filter(pk__in=[image.pk for image in images])
        with CaptureQueriesContext(connection) as captured:
            _delete_owned_images(queryset, unlink_from_pin_when_on_wiki=False)
        statements = captured.captured_queries
        # Placeholders stand in for parameters, so counting them measures how
        # much the batch is being carried into each statement.
        return len(statements), sum(query["sql"].count("%s") + query["sql"].count(", ") for query in statements)

    def test_deleting_a_batch_does_not_carry_the_batch_into_every_row_s_queries(self) -> None:
        """The cost must grow with the batch, not with the batch squared.

        One statement legitimately names every row - the DELETE itself. What
        must not happen is a *per row* statement that also names every row,
        which is what asking the shared-file question inside the loop did.
        """
        small_statements, small_params = self._delete(self._photos(_SMALL, "small"))
        large_statements, large_params = self._delete(self._photos(_LARGE, "large"))

        size_ratio = _LARGE / _SMALL
        param_ratio = large_params / max(small_params, 1)
        self.assertLess(
            param_ratio,
            size_ratio * 2,
            f"deleting {_SMALL} photos carried {small_params} parameters across {small_statements} statements "
            f"and {_LARGE} carried {large_params} across {large_statements} - growing {param_ratio:.1f}x for a "
            f"{size_ratio:.0f}x bigger batch, so each row's queries are still carrying the whole batch",
        )
