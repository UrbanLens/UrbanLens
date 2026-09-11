"""One account must not be able to fill the volume Postgres lives on.

`materialize_media_item` downloads an external photo and stores it with
`QuotaExemption.EXTERNAL_MEDIA`, which is deliberate: the cache exists so the
gallery survives a provider's URL rotting, and the person who upvoted an item
into it did not author the photo and should not be charged for caching it.

The exemption is about *attribution*, but it was also the only bound. The module
docstring says so in its own words - "Only the per-item `_MAX_DOWNLOAD_BYTES` cap
applies" - and per-item is not a bound on an account: 20MB times however many
distinct external photos one user cares to upvote is unbounded, written to the
shared media volume, with the storage quota explicitly off. Filling that
filesystem takes the database down with it, which is every user's availability
lost to one user's clicking (N21 H19).

So the exemption stays and a rolling per-profile ceiling goes beside it. Nobody
is charged for caching someone else's photo; nobody can cache an unbounded
number of them in a day either.

`wiki_media.WikiMediaVoteView`'s docstring said this "costs the *voter's* storage
quota" - it never did, and that claim is corrected here rather than left as the
thing a reader would check first.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item

#: The Django setting the ceiling lives behind. `UL_EXTERNAL_MEDIA_DAILY_BYTES`
#: is the environment variable that feeds it and is not what override_settings
#: takes - the trap `test_map_document_cap.py` documents.
SETTING_NAME = "EXTERNAL_MEDIA_DAILY_BYTES"

#: Small enough to state the rule without downloading half a gigabyte.
TEST_CEILING = 1000

_FAKE_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]


def _ok_response(content: bytes = b"fake-jpeg-bytes") -> mock.Mock:
    """A download that succeeds, returning *content*."""
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.raw.read.return_value = content
    response.is_redirect = False
    return response


class _MaterializeCase(TestCase):
    """A profile that can materialize, with the network stubbed out."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location)
        dns = mock.patch("socket.getaddrinfo", return_value=_FAKE_DNS_RESULT)
        dns.start()
        self.addCleanup(dns.stop)

    def materialize(self, url: str = "https://example.test/photo.jpg", content: bytes = b"x" * 100) -> Image:
        """Materialize one item, returning the row.

        Args:
            url: Source url, which is also the dedupe key.
            content: Bytes the provider returns.

        Returns:
            The created or reused Image.
        """
        with mock.patch(
            "urbanlens.dashboard.services.media.media_materialize.requests.get", return_value=_ok_response(content)
        ) as fetch:
            self.last_fetch = fetch
            return materialize_media_item(
                location=self.location, profile=self.profile, source="wikimedia", url=url, caption=""
            )

    def spend(self, total: int, *, profile: object = None, age: timedelta = timedelta()) -> None:
        """Record *total* bytes already materialized, without downloading them.

        Args:
            total: Bytes to attribute.
            profile: Whose allowance to spend. Defaults to this case's profile.
            age: How long ago, for the rolling-window tests.
        """
        image = baker.make(
            Image,
            profile=profile or self.profile,
            location=self.location,
            file_size=total,
            quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA,
        )
        if age:
            Image.objects.filter(pk=image.pk).update(created=timezone.now() - age)


class TheCeilingExistsTests(_MaterializeCase):
    """Before anything can be bounded, something has to name the bound."""

    def test_a_setting_names_the_daily_maximum(self) -> None:
        from django.conf import settings

        self.assertTrue(
            hasattr(settings, SETTING_NAME),
            f"{SETTING_NAME} does not exist, so nothing bounds how much external media one account can cache - "
            "and an override_settings of that name would configure a ceiling no code reads.",
        )

    def test_the_name_is_one_production_reads(self) -> None:
        """Overriding it must move the number the serving code asks for."""
        from urbanlens.dashboard.services.media import media_materialize

        with override_settings(**{SETTING_NAME: 7}):
            self.assertEqual(media_materialize.daily_external_media_bytes(), 7)


@override_settings(**{SETTING_NAME: TEST_CEILING})
class TheCeilingIsEnforcedTests(_MaterializeCase):
    """The rule itself, from both sides."""

    def test_a_profile_under_the_ceiling_materializes(self) -> None:
        self.spend(TEST_CEILING - 500)

        image = self.materialize(content=b"x" * 100)

        self.assertIsNotNone(image.pk)

    def test_a_profile_over_the_ceiling_is_refused(self) -> None:
        self.spend(TEST_CEILING + 1)

        with self.assertRaises(MaterializeError):
            self.materialize()

    def test_the_refusal_happens_before_the_download(self) -> None:
        """Otherwise the bytes are already spent by the time it is refused."""
        self.spend(TEST_CEILING + 1)

        with (
            mock.patch("urbanlens.dashboard.services.media.media_materialize.requests.get") as fetch,
            self.assertRaises(MaterializeError),
        ):
            materialize_media_item(
                location=self.location,
                profile=self.profile,
                source="wikimedia",
                url="https://example.test/a.jpg",
                caption="",
            )

        fetch.assert_not_called()

    def test_another_profiles_caching_does_not_spend_mine(self) -> None:
        """The ceiling is per account, or it is a site-wide denial of service."""
        other = baker.make(User)
        self.spend(TEST_CEILING + 1, profile=other.profile)

        self.assertIsNotNone(self.materialize().pk)

    def test_yesterdays_caching_does_not_count(self) -> None:
        """Rolling, not cumulative - a cumulative cap is a permanent ban."""
        self.spend(TEST_CEILING + 1, age=timedelta(days=2))

        self.assertIsNotNone(self.materialize().pk)

    def test_only_exempt_rows_count_against_it(self) -> None:
        """An ordinary upload is charged to the quota instead; counting it here
        would charge it twice and refuse a user their own photos."""
        baker.make(
            Image, profile=self.profile, location=self.location, file_size=TEST_CEILING + 1, quota_exempt_reason=""
        )

        self.assertIsNotNone(self.materialize().pk)

    def test_a_dedupe_hit_is_free(self) -> None:
        """Re-voting the same photo stores nothing, so it must not be refused -
        and the refusal must not run before the dedupe check finds it."""
        first = self.materialize(url="https://example.test/same.jpg")
        self.spend(TEST_CEILING + 1)

        with mock.patch("urbanlens.dashboard.services.media.media_materialize.requests.get") as fetch:
            again = materialize_media_item(
                location=self.location,
                profile=self.profile,
                source="wikimedia",
                url="https://example.test/same.jpg",
                caption="",
            )

        self.assertEqual(again.pk, first.pk)
        fetch.assert_not_called()
