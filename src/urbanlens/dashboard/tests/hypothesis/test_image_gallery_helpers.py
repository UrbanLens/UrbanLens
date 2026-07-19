"""Tests for image_gallery/services.images helper functions.

Covers:
- _dms_to_decimal() - DMS→decimal conversion with N/S/E/W refs
- extract_gps_coords() - EXIF GPS extraction with mock PIL
- extract_taken_at() - EXIF DateTimeOriginal extraction with mock PIL
- image_to_gallery_json() - dict serialisation of Image instances
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import io
from unittest.mock import MagicMock, patch

from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.images import _dms_to_decimal, extract_gps_coords, extract_taken_at, image_to_gallery_json

_hyp = hyp_settings(max_examples=60, deadline=None)


# ---------------------------------------------------------------------------
# _dms_to_decimal
# ---------------------------------------------------------------------------

class DmsToDecimalTests(SimpleTestCase):
    """_dms_to_decimal converts degree/minute/second tuples to signed float."""

    def test_north_positive(self):
        # 40° 26' 46.302" N
        result = _dms_to_decimal((40, 26, 46.302), "N")
        self.assertAlmostEqual(result, 40.446195, places=4)

    def test_south_negative(self):
        result = _dms_to_decimal((33, 51, 21.6), "S")
        self.assertLess(result, 0)
        self.assertAlmostEqual(result, -33.856, places=3)

    def test_east_positive(self):
        result = _dms_to_decimal((74, 0, 21.6), "E")
        self.assertGreater(result, 0)

    def test_west_negative(self):
        result = _dms_to_decimal((74, 0, 21.6), "W")
        self.assertLess(result, 0)

    def test_zero_lat(self):
        result = _dms_to_decimal((0, 0, 0), "N")
        self.assertAlmostEqual(result, 0.0, places=6)

    def test_minutes_and_seconds_zero(self):
        result = _dms_to_decimal((45, 0, 0), "N")
        self.assertAlmostEqual(result, 45.0, places=6)

    def test_north_south_symmetry(self):
        north = _dms_to_decimal((10, 30, 0), "N")
        south = _dms_to_decimal((10, 30, 0), "S")
        self.assertAlmostEqual(north, -south, places=6)

    def test_east_west_symmetry(self):
        east = _dms_to_decimal((120, 0, 0), "E")
        west = _dms_to_decimal((120, 0, 0), "W")
        self.assertAlmostEqual(east, -west, places=6)

    @given(
        deg=st.floats(min_value=0, max_value=89, allow_nan=False),
        mins=st.floats(min_value=0, max_value=59, allow_nan=False),
        secs=st.floats(min_value=0, max_value=59, allow_nan=False),
    )
    @_hyp
    def test_north_always_nonnegative(self, deg: float, mins: float, secs: float):
        result = _dms_to_decimal((deg, mins, secs), "N")
        self.assertGreaterEqual(result, 0.0)

    @given(
        deg=st.floats(min_value=0, max_value=89, allow_nan=False),
        mins=st.floats(min_value=0, max_value=59, allow_nan=False),
        secs=st.floats(min_value=0, max_value=59, allow_nan=False),
    )
    @_hyp
    def test_south_always_nonpositive(self, deg: float, mins: float, secs: float):
        result = _dms_to_decimal((deg, mins, secs), "S")
        self.assertLessEqual(result, 0.0)


# ---------------------------------------------------------------------------
# extract_gps_coords - via mocked PIL
# ---------------------------------------------------------------------------

class ExtractGpsCoordsMockTests(SimpleTestCase):
    """extract_gps_coords extracts GPS from EXIF via mocked PIL objects."""

    def _make_file_with_gps(self, lat_dms, lat_ref, lng_dms, lng_ref):
        """Return a mock file object with a PIL image that yields GPS EXIF data."""
        gps_data = {
            "GPSLatitude": lat_dms,
            "GPSLatitudeRef": lat_ref,
            "GPSLongitude": lng_dms,
            "GPSLongitudeRef": lng_ref,
        }

        mock_exif = MagicMock()
        mock_exif.__bool__ = lambda self: True
        mock_exif.get_ifd.return_value = {
            k: v for k, v in {
                "GPSLatitude": gps_data["GPSLatitude"],
                "GPSLatitudeRef": gps_data["GPSLatitudeRef"],
                "GPSLongitude": gps_data["GPSLongitude"],
                "GPSLongitudeRef": gps_data["GPSLongitudeRef"],
            }.items()
        }

        mock_img = MagicMock()
        mock_img.getexif.return_value = mock_exif

        mock_file = io.BytesIO(b"fake_image_data")

        from PIL.ExifTags import GPSTAGS
        # The function uses GPSTAGS.get(k, k) to decode keys, so we need to
        # supply numeric tag keys matching the GPSTAGS mapping.
        # Build a reverse map: name → tag int
        reverse_gpstags = {v: k for k, v in GPSTAGS.items()}

        raw_ifd = {
            reverse_gpstags.get("GPSLatitude", "GPSLatitude"): lat_dms,
            reverse_gpstags.get("GPSLatitudeRef", "GPSLatitudeRef"): lat_ref,
            reverse_gpstags.get("GPSLongitude", "GPSLongitude"): lng_dms,
            reverse_gpstags.get("GPSLongitudeRef", "GPSLongitudeRef"): lng_ref,
        }
        mock_exif.get_ifd.return_value = raw_ifd

        return mock_file, mock_img, mock_exif

    def test_returns_coords_for_valid_gps(self):
        mock_file, mock_img, _ = self._make_file_with_gps(
            (40, 26, 46), "N",
            (74, 0, 21), "W",
        )
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_gps_coords(mock_file)
        self.assertIsNotNone(result)
        lat, lng = result
        self.assertGreater(lat, 0)   # N
        self.assertLess(lng, 0)      # W

    def test_returns_none_when_no_exif(self):
        mock_img = MagicMock()
        mock_img.getexif.return_value = None
        mock_file = io.BytesIO(b"fake")
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_gps_coords(mock_file)
        self.assertIsNone(result)

    def test_returns_none_when_no_gps_ifd(self):
        mock_exif = MagicMock()
        mock_exif.__bool__ = lambda self: True
        mock_exif.get_ifd.return_value = {}
        mock_img = MagicMock()
        mock_img.getexif.return_value = mock_exif
        mock_file = io.BytesIO(b"fake")
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_gps_coords(mock_file)
        self.assertIsNone(result)

    def test_returns_none_on_exception(self):
        mock_file = io.BytesIO(b"not a real image")
        with patch(
            "urbanlens.dashboard.services.images.PILImage.open",
            side_effect=Exception("cannot identify image file"),
        ):
            result = extract_gps_coords(mock_file)
        self.assertIsNone(result)

    def test_file_seeked_back_after_extraction(self):
        """File position must be reset to 0 after extraction so callers can re-read."""
        mock_img = MagicMock()
        mock_img.getexif.return_value = None
        mock_file = MagicMock()
        mock_file.seek.return_value = None
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            extract_gps_coords(mock_file)
        # seek(0) called in the finally block
        mock_file.seek.assert_called_with(0)

    def test_returns_none_for_nan_gps(self):
        """Zero-denominator EXIF rationals (some phones write these for 'GPS on, no fix')
        decode to NaN; that must not reach callers, who store it as a DB decimal."""
        mock_file, mock_img, _ = self._make_file_with_gps(
            (float("nan"), 0, 0), "N",
            (74, 0, 21), "W",
        )
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_gps_coords(mock_file)
        self.assertIsNone(result)

    def test_returns_none_for_infinite_gps(self):
        mock_file, mock_img, _ = self._make_file_with_gps(
            (40, 26, 46), "N",
            (float("inf"), 0, 0), "W",
        )
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_gps_coords(mock_file)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# extract_taken_at - via mocked PIL
# ---------------------------------------------------------------------------

class ExtractTakenAtMockTests(SimpleTestCase):
    """extract_taken_at extracts EXIF DateTimeOriginal via mocked PIL objects."""

    def _make_file_with_exif_ifd(self, exif_ifd: dict):
        mock_exif = MagicMock()
        mock_exif.__bool__ = lambda self: True
        mock_exif.get_ifd.return_value = exif_ifd
        mock_img = MagicMock()
        mock_img.getexif.return_value = mock_exif
        mock_file = io.BytesIO(b"fake_image_data")
        return mock_file, mock_img

    def test_returns_datetime_for_valid_exif(self):
        mock_file, mock_img = self._make_file_with_exif_ifd({0x9003: "2020:06:15 08:30:00"})
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_taken_at(mock_file)
        self.assertIsNotNone(result)
        self.assertEqual((result.year, result.month, result.day), (2020, 6, 15))
        self.assertEqual((result.hour, result.minute, result.second), (8, 30, 0))

    def test_returns_none_when_no_exif(self):
        mock_img = MagicMock()
        mock_img.getexif.return_value = None
        mock_file = io.BytesIO(b"fake")
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_taken_at(mock_file)
        self.assertIsNone(result)

    def test_returns_none_when_no_datetime_original_tag(self):
        mock_file, mock_img = self._make_file_with_exif_ifd({})
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_taken_at(mock_file)
        self.assertIsNone(result)

    def test_returns_none_for_unparseable_value(self):
        mock_file, mock_img = self._make_file_with_exif_ifd({0x9003: "not-a-date"})
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_taken_at(mock_file)
        self.assertIsNone(result)

    def test_returns_none_on_exception(self):
        mock_file = io.BytesIO(b"not a real image")
        with patch(
            "urbanlens.dashboard.services.images.PILImage.open",
            side_effect=Exception("cannot identify image file"),
        ):
            result = extract_taken_at(mock_file)
        self.assertIsNone(result)

    def test_file_seeked_back_after_extraction(self):
        mock_img = MagicMock()
        mock_img.getexif.return_value = None
        mock_file = MagicMock()
        mock_file.seek.return_value = None
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            extract_taken_at(mock_file)
        mock_file.seek.assert_called_with(0)

    @given(dt=st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2099, 12, 31)))
    @hyp_settings(max_examples=40, deadline=None)
    def test_round_trips_arbitrary_datetime(self, dt: datetime):
        exif_value = dt.strftime("%Y:%m:%d %H:%M:%S")
        mock_file, mock_img = self._make_file_with_exif_ifd({0x9003: exif_value})
        with patch("urbanlens.dashboard.services.images.PILImage.open", return_value=mock_img):
            result = extract_taken_at(mock_file)
        self.assertIsNotNone(result)
        expected = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
        self.assertEqual(result.replace(tzinfo=None), expected.replace(tzinfo=None, microsecond=0))


# ---------------------------------------------------------------------------
# image_to_gallery_json
# ---------------------------------------------------------------------------

class ImageToGalleryJsonTests(SimpleTestCase):
    """image_to_gallery_json serialises Image model instances to map-layer dicts."""

    def _make_image(self, lat=None, lng=None, caption=None, profile=None):
        img = MagicMock()
        img.pk = 42
        img.image.url = "/media/test.jpg"
        img.caption = caption
        img.latitude = Decimal(str(lat)) if lat is not None else None
        img.longitude = Decimal(str(lng)) if lng is not None else None
        img.profile = profile
        img.profile_id = profile.pk if profile else None
        return img

    def _make_request(self):
        req = MagicMock()
        req.build_absolute_uri.side_effect = lambda url: f"http://testserver{url}"
        return req

    def _make_profile(self, pk=1, username="testuser"):
        p = MagicMock()
        p.pk = pk
        p.username = username
        return p

    def test_id_included(self):
        img = self._make_image(lat=10.0, lng=20.0)
        result = image_to_gallery_json(img, self._make_request())
        self.assertEqual(result["id"], 42)

    def test_url_built_from_absolute_uri(self):
        img = self._make_image()
        result = image_to_gallery_json(img, self._make_request())
        self.assertIn("http://testserver", result["url"])

    def test_caption_none_becomes_empty_string(self):
        img = self._make_image(caption=None)
        result = image_to_gallery_json(img, self._make_request())
        self.assertEqual(result["caption"], "")

    def test_caption_preserved(self):
        img = self._make_image(caption="A great photo")
        result = image_to_gallery_json(img, self._make_request())
        self.assertEqual(result["caption"], "A great photo")

    def test_coords_none_when_not_set(self):
        img = self._make_image(lat=None, lng=None)
        result = image_to_gallery_json(img, self._make_request())
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["longitude"])

    def test_coords_float_when_set(self):
        img = self._make_image(lat=51.5, lng=-0.12)
        result = image_to_gallery_json(img, self._make_request())
        self.assertAlmostEqual(result["latitude"], 51.5)
        self.assertAlmostEqual(result["longitude"], -0.12)

    def test_uploader_username_from_profile(self):
        profile = self._make_profile(username="alice")
        img = self._make_image()
        img.profile = profile
        result = image_to_gallery_json(img, self._make_request())
        self.assertEqual(result["uploader"], "alice")

    def test_uploader_empty_when_no_profile(self):
        img = self._make_image()
        img.profile = None
        result = image_to_gallery_json(img, self._make_request())
        self.assertEqual(result["uploader"], "")

    def test_is_mine_true_for_owner(self):
        profile = self._make_profile(pk=7)
        img = self._make_image()
        img.profile_id = 7
        result = image_to_gallery_json(img, self._make_request(), viewer_profile=profile)
        self.assertTrue(result["is_mine"])

    def test_is_mine_false_for_other_user(self):
        viewer = self._make_profile(pk=99)
        img = self._make_image()
        img.profile_id = 7
        result = image_to_gallery_json(img, self._make_request(), viewer_profile=viewer)
        self.assertFalse(result["is_mine"])

    def test_is_mine_false_when_no_viewer(self):
        img = self._make_image()
        img.profile_id = 7
        result = image_to_gallery_json(img, self._make_request(), viewer_profile=None)
        self.assertFalse(result["is_mine"])
