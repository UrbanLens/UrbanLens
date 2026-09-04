"""Who owns a photo: ``Image.is_own_contribution`` and its ORM twin.

Several independent gates ask the same question - may a concealed viewer see
this, may this profile withdraw it from a wiki, does it earn reputation, does it
count towards an upload achievement - and every one of them used to answer it
with ``source == ImageSource.UPLOAD``. That is wrong in both directions:

* A photo picked out of the user's own Immich server, Google Photos library or
  Flickr account is their own picture, but carries the provider's name in
  ``source``. Gating on ``UPLOAD`` showed a stranger's personal photos to a
  concealed viewer.
* A row materialised from somebody else's provider search can carry ``UPLOAD``
  in ``source`` anyway, because ``media_materialize._translated_source`` falls
  back to it for an unrecognised panel key.

So ownership is three conjuncts, and the completeness test at the bottom is what
keeps them honest: adding an ``ImageSource`` without deciding which side it falls
on fails the build, rather than silently defaulting to "not personal" - which is
the direction that leaks.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource

#: Every source that is not a personal library, and why. Spelled out rather than
#: derived, so the completeness test compares two independently-written lists.
NOT_PERSONAL: dict[str, str] = {
    ImageSource.LINKED_URL: "bytes fetched because a page referred to them",
    ImageSource.YELP: "somebody else's photograph, up-voted",
    ImageSource.GOOGLE_IMAGES: "somebody else's photograph, up-voted",
    ImageSource.GOOGLE_MAPS: "provider imagery fetched for the place",
    ImageSource.WIKIMEDIA: "somebody else's photograph, up-voted",
    ImageSource.WIKIPEDIA_MEDIA: "somebody else's photograph, up-voted",
    ImageSource.SMITHSONIAN: "archive holding",
    ImageSource.LIBRARY_OF_CONGRESS: "archive holding",
    ImageSource.INTERNET_ARCHIVE: "archive holding",
    ImageSource.DIGITAL_COMMONWEALTH: "archive holding",
    ImageSource.LOOPNET: "listing photography",
    ImageSource.CRIS: "state survey photography",
    ImageSource.EXTERNAL_API: "candidate photo an external app submitted",
    ImageSource.GOOGLE_STREET_VIEW: "provider imagery fetched for the place",
    ImageSource.GOOGLE_SATELLITE: "provider imagery fetched for the place",
}


class PersonalLibraryCoverageTests(SimpleTestCase):
    """Every source is classified, deliberately."""

    def test_the_two_sets_partition_every_source(self) -> None:
        classified = ImageSource.personal_library() | set(NOT_PERSONAL)

        self.assertEqual(
            classified,
            set(ImageSource.values),
            "An ImageSource is not classified as personal or not. Decide: leaving a personal-library integration out shows a stranger's own photos to a concealed viewer, and it fails silently.",
        )

    def test_the_two_sets_do_not_overlap(self) -> None:
        self.assertEqual(ImageSource.personal_library() & set(NOT_PERSONAL), set())

    def test_the_connected_account_pickers_are_personal(self) -> None:
        # Named individually so a later edit cannot quietly drop one.
        for source in (ImageSource.UPLOAD, ImageSource.IMMICH, ImageSource.GOOGLE_PHOTOS, ImageSource.FLICKR):
            with self.subTest(source=source):
                self.assertIn(source, ImageSource.personal_library())


class IsOwnContributionTests(TestCase):
    """The row-level predicate, and the queryset form agreeing with it."""

    def _image(self, **kwargs) -> Image:
        defaults = {"profile": baker.make("dashboard.Profile"), "source": ImageSource.UPLOAD, "image": "pin_images/x.png"}
        return baker.make(Image, **{**defaults, **kwargs})

    def test_a_form_upload_is_owned(self) -> None:
        self.assertTrue(self._image().is_own_contribution)

    def test_a_connected_library_import_is_owned(self) -> None:
        for source in (ImageSource.IMMICH, ImageSource.GOOGLE_PHOTOS, ImageSource.FLICKR):
            with self.subTest(source=source):
                self.assertTrue(self._image(source=source).is_own_contribution)

    def test_a_materialised_provider_row_is_not_owned(self) -> None:
        self.assertFalse(self._image(source=ImageSource.WIKIMEDIA, media_source_key="wikimedia").is_own_contribution)

    def test_a_materialised_row_that_fell_back_to_upload_is_not_owned(self) -> None:
        """``_translated_source`` yields UPLOAD for an unrecognised panel key.

        Without the ``media_source_key`` conjunct this row would read as the
        up-voter's own photograph purely because the panel was unrecognised.
        """
        self.assertFalse(self._image(source=ImageSource.UPLOAD, media_source_key="some_new_panel").is_own_contribution)

    def test_a_materialised_flickr_row_is_not_owned(self) -> None:
        """Flickr is both a connected account and a Media gallery panel."""
        self.assertFalse(self._image(source=ImageSource.FLICKR, media_source_key="flickr").is_own_contribution)

    def test_profile_less_enrichment_imagery_is_nobodys(self) -> None:
        self.assertFalse(self._image(profile=None, source=ImageSource.GOOGLE_STREET_VIEW).is_own_contribution)

    def test_bytes_fetched_from_a_pasted_url_are_not_owned(self) -> None:
        self.assertFalse(self._image(source=ImageSource.LINKED_URL).is_own_contribution)

    def test_the_queryset_agrees_with_the_property_row_for_row(self) -> None:
        """The two are written separately and must not drift.

        A page filters with the queryset form and then renders each row through
        the property; disagreement is a photo listed but marked withdrawable, or
        the reverse.
        """
        rows = [
            self._image(),
            self._image(source=ImageSource.IMMICH),
            self._image(source=ImageSource.FLICKR, media_source_key="flickr"),
            self._image(source=ImageSource.WIKIMEDIA, media_source_key="wikimedia"),
            self._image(source=ImageSource.LINKED_URL),
            self._image(profile=None, source=ImageSource.GOOGLE_SATELLITE),
        ]
        owned_ids = set(Image.objects.filter(pk__in=[row.pk for row in rows]).own_contributions().values_list("pk", flat=True))

        for row in rows:
            with self.subTest(source=row.source, key=row.media_source_key, has_profile=row.profile_id is not None):
                self.assertEqual(row.pk in owned_ids, row.is_own_contribution)

    def test_provider_media_is_the_exact_complement(self) -> None:
        rows = [self._image(), self._image(source=ImageSource.WIKIMEDIA, media_source_key="wikimedia"), self._image(source=ImageSource.LINKED_URL)]
        scope = Image.objects.filter(pk__in=[row.pk for row in rows])

        owned = set(scope.own_contributions().values_list("pk", flat=True))
        provider = set(scope.provider_media().values_list("pk", flat=True))

        self.assertEqual(owned | provider, {row.pk for row in rows})
        self.assertEqual(owned & provider, set())
