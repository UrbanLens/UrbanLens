"""Tests for the structural half of media authorization.

The media gate is default-deny, which is only safe if nothing can quietly land
outside it. Two mechanisms hold that up, and this module tests both:

- ``dashboard.checks.check_media_authorizers`` fails ``manage.py check`` when a
  model stores files under a directory no authorizer covers, so a new media
  field cannot ship without someone deciding who may read it.
- The upload-path callables file each upload under a random directory, so a
  stored path cannot be derived from the name it was uploaded under - fuzzing
  ``/media/pin_images/IMG_4821.jpg`` never names a file that exists.
"""

from __future__ import annotations

from django.db.models import FileField, ImageField
from hypothesis import given, settings as hypothesis_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.checks import check_media_authorizers
from urbanlens.dashboard.models.images.model import Image, pin_image_thumbnail_path, pin_image_upload_path
from urbanlens.dashboard.services.media.access import MEDIA_FAMILY_ATTR, authorize_media, registered_families

# Filenames a browser, phone, or import can realistically hand over. Excludes
# path separators and NUL, which Django's storage layer rejects before this.
_FILENAMES = st.text(alphabet=st.characters(blacklist_characters="/\\\x00", blacklist_categories=("Cs",)), min_size=1, max_size=200)


class MediaFamilyCheckTests(SimpleTestCase):
    """``check_media_authorizers`` is what makes forgetting an authorizer loud."""

    def test_every_media_field_in_the_project_is_covered(self):
        """The check passes as the project stands - no unauthorized family ships."""
        self.assertEqual(check_media_authorizers(), [], "a model stores files under a directory the media gate would refuse")

    def test_an_unregistered_directory_is_reported(self):
        """A file field under a directory nobody authorized is an error.

        Proved against a stand-in field rather than a real model, so the test
        keeps working when the registry gains families.
        """
        field = FileField(upload_to="brand_new_family/")
        field.name = "attachment"

        family, hint = _resolve(field)

        self.assertIsNone(hint, "a plain string upload_to resolves without needing a declaration")
        self.assertEqual(family, "brand_new_family")
        self.assertNotIn(family, registered_families(), "the fixture directory must not be one somebody registered")
        self.assertFalse(authorize_media(_AnyProfile(), "brand_new_family/leaked.png"), "an unregistered family must be refused, not served")

    def test_a_callable_upload_to_must_declare_its_directory(self):
        """A callable's prefix cannot be read statically, so it has to say."""

        def undeclared_path(instance: object, filename: str) -> str:
            return f"somewhere/{filename}"

        field = ImageField(upload_to=undeclared_path)
        field.name = "photo"

        family, hint = _resolve(field)

        self.assertIsNone(family)
        self.assertIn("does not declare", hint or "")

    def test_the_image_upload_callables_declare_their_directory(self):
        for callable_ in (pin_image_upload_path, pin_image_thumbnail_path):
            with self.subTest(callable_=callable_.__name__):
                self.assertEqual(getattr(callable_, MEDIA_FAMILY_ATTR, None), "pin_images")


class UnguessableUploadPathTests(SimpleTestCase):
    """A stored path must not be derivable from the uploaded filename."""

    @given(filename=_FILENAMES)
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_two_uploads_of_one_filename_never_share_a_path(self, filename: str):
        """The same name uploaded twice lands in two different directories.

        Without this, the first person to upload ``IMG_4821.jpg`` occupies the
        one path everybody else's ``IMG_4821.jpg`` would be guessed at.
        """
        first = pin_image_upload_path(Image(), filename)
        second = pin_image_upload_path(Image(), filename)

        self.assertNotEqual(first, second)

    @given(filename=_FILENAMES)
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_the_stored_path_carries_enough_randomness_to_resist_guessing(self, filename: str):
        stored = pin_image_upload_path(Image(), filename)

        self.assertTrue(stored.startswith("pin_images/"), stored)
        _, bucket, token, _name = stored.split("/", 3)
        self.assertRegex(bucket, r"^[A-Za-z0-9_-]{2}$")
        self.assertRegex(token, r"^[A-Za-z0-9_-]{10,}$")

    @given(filename=_FILENAMES)
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_a_thumbnail_path_is_not_derivable_from_its_original(self, filename: str):
        """Holding one URL must not hand you the other."""
        original = pin_image_upload_path(Image(), filename)
        thumbnail = pin_image_thumbnail_path(Image(), filename)

        self.assertTrue(thumbnail.startswith("pin_images/thumbs/"), thumbnail)
        original_dir = original.split("/", 3)[1:3]
        thumbnail_dir = thumbnail.split("/", 4)[2:4]
        self.assertNotEqual(original_dir, thumbnail_dir)

    @given(filename=st.sampled_from(["PXL_20260709_123456.jpg", "IMG_4821.JPG", "DSC00042.jpeg", "20260709_101112.jpg"]))
    def test_a_camera_filename_survives_into_storage(self, filename: str):
        """The attribution heuristic reads the stored name, so the stem stays last.

        ``services.media.images.is_camera_generated_filename`` anchors its match
        at the start of the basename; prefixing the random token onto the
        filename instead of putting it in a directory would silently stop every
        camera-named upload from being attributed to its uploader.
        """
        from urbanlens.dashboard.services.media.images import is_camera_generated_filename

        stored = pin_image_upload_path(Image(), filename)

        self.assertTrue(is_camera_generated_filename(stored), stored)

    def test_an_overlong_name_still_fits_the_column(self):
        """Random directory plus trimmed stem stays inside ``max_length``."""
        max_length = Image._meta.get_field("image").max_length
        # Room for the ~8-character suffix Storage.get_available_name appends
        # when a name collides.
        headroom = 8

        stored = pin_image_thumbnail_path(Image(), "x" * 500 + ".jpeg")

        self.assertLessEqual(len(stored) + headroom, max_length)


class _AnyProfile:
    """Stand-in for a Profile - an unregistered family is refused before use."""

    pk = 1


def _resolve(field: FileField) -> tuple[str | None, str | None]:
    """Resolve a field's media family the way the check does.

    Args:
        field: The file field to inspect.

    Returns:
        ``(family, hint)`` - see ``dashboard.checks._declared_family``.
    """
    from urbanlens.dashboard.checks import _declared_family

    return _declared_family(field)
