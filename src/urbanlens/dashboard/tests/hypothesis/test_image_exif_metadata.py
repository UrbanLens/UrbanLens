"""Tests for the EXIF-only photo metadata fields and their extraction.

Covers:
- extract_gps_altitude/extract_gps_orientation/extract_camera_info/
  extract_lens_model/extract_shutter_speed/extract_aperture/extract_focal_length
- process_image_upload() wiring those (and exif_latitude/exif_longitude) onto
  the row, and that the coordinate/altitude/orientation trio is write-once -
  a re-run must not overwrite a value that's already there.
"""

from __future__ import annotations

from fractions import Fraction
import io
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media.images import (
    _flatten_xmp,
    extract_aperture,
    extract_camera_info,
    extract_focal_length,
    extract_gps_altitude,
    extract_gps_orientation,
    extract_lens_model,
    extract_shutter_speed,
)
from urbanlens.dashboard.tasks import process_image_upload

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-exif-")

_GPS_IFD = 0x8825
_EXIF_IFD = 0x8769


def _jpeg_bytes(
    *,
    gps_altitude: tuple[float, int] | None = None,
    make: str | None = None,
    model: str | None = None,
    lens_model: str | None = None,
    exposure_time: Fraction | None = None,
    fnumber: Fraction | None = None,
    focal_length: Fraction | None = None,
) -> bytes:
    """Build an in-memory JPEG carrying whichever EXIF tags were requested."""
    exif = PILImage.Exif()
    if gps_altitude is not None:
        altitude, ref = gps_altitude
        gps = exif.get_ifd(_GPS_IFD)
        gps[5] = ref
        gps[6] = Fraction(altitude).limit_denominator(1000)
    if make is not None:
        exif[0x010F] = make
    if model is not None:
        exif[0x0110] = model
    exif_ifd = exif.get_ifd(_EXIF_IFD)
    if lens_model is not None:
        exif_ifd[0xA434] = lens_model
    # Pillow's TIFF writer only knows how to serialize its own IFDRational for a
    # scalar RATIONAL tag - a bare stdlib Fraction crashes tobytes() (it infers
    # the wrong tag type). A Fraction *tuple* (the GPS DMS fixtures elsewhere in
    # this codebase) doesn't hit the same path, so this only bites scalars.
    if exposure_time is not None:
        exif_ifd[0x829A] = IFDRational(exposure_time.numerator, exposure_time.denominator)
    if fnumber is not None:
        exif_ifd[0x829D] = IFDRational(fnumber.numerator, fnumber.denominator)
    if focal_length is not None:
        exif_ifd[0x920A] = IFDRational(focal_length.numerator, focal_length.denominator)
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


class ExtractGpsAltitudeTests(TestCase):
    def test_reads_altitude_above_sea_level(self):
        altitude = extract_gps_altitude(io.BytesIO(_jpeg_bytes(gps_altitude=(123.4, 0))))
        self.assertAlmostEqual(altitude, 123.4, places=1)

    def test_negates_altitude_below_sea_level(self):
        altitude = extract_gps_altitude(io.BytesIO(_jpeg_bytes(gps_altitude=(5.0, 1))))
        self.assertAlmostEqual(altitude, -5.0, places=1)

    def test_none_when_absent(self):
        self.assertIsNone(extract_gps_altitude(io.BytesIO(_jpeg_bytes())))


class FlattenXmpTests(TestCase):
    """_flatten_xmp reduces Image.getxmp()'s nested dict to local-name -> value."""

    def test_flattens_nested_description(self):
        data = {
            "xmpmeta": {
                "RDF": {
                    "Description": {
                        "{http://ns.google.com/photos/1.0/panorama/}PosePitchDegrees": "1.25",
                        "PoseRollDegrees": "-0.4",
                    }
                }
            }
        }
        out: dict = {}
        _flatten_xmp(data, out)
        self.assertEqual(out["posepitchdegrees"], "1.25")
        self.assertEqual(out["poserolldegrees"], "-0.4")

    def test_first_value_wins_on_duplicate_local_names(self):
        data = {"a": {"X:Key": "first"}, "b": {"Y:Key": "second"}}
        out: dict = {}
        _flatten_xmp(data, out)
        self.assertEqual(out["key"], "first")


class ExtractGpsOrientationTests(TestCase):
    """extract_gps_orientation reads the XMP GPano/drone pitch+roll pair, if present."""

    def _image_with_xmp(self, xmp: dict | None):
        img = PILImage.new("RGB", (60, 40), color=(1, 2, 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        opened = PILImage.open(buf)
        opened.getxmp = mock.Mock(return_value=xmp)
        return opened

    def test_reads_gpano_pitch_and_roll(self):
        xmp = {"Description": {"GPano:PosePitchDegrees": "2.5", "GPano:PoseRollDegrees": "-1.0"}}
        with mock.patch(
            "urbanlens.dashboard.services.media.images.PILImage.open", return_value=self._image_with_xmp(xmp)
        ):
            result = extract_gps_orientation(io.BytesIO(b"not a real jpeg, open() is mocked"))
        self.assertEqual(result, (2.5, -1.0))

    def test_none_when_xmp_absent(self):
        with mock.patch(
            "urbanlens.dashboard.services.media.images.PILImage.open", return_value=self._image_with_xmp(None)
        ):
            result = extract_gps_orientation(io.BytesIO(b"not a real jpeg, open() is mocked"))
        self.assertIsNone(result)

    def test_none_when_only_one_axis_present(self):
        xmp = {"Description": {"GPano:PosePitchDegrees": "2.5"}}
        with mock.patch(
            "urbanlens.dashboard.services.media.images.PILImage.open", return_value=self._image_with_xmp(xmp)
        ):
            result = extract_gps_orientation(io.BytesIO(b"not a real jpeg, open() is mocked"))
        self.assertIsNone(result)


class ExtractCameraLensExposureTests(TestCase):
    def test_extract_camera_info_reads_make_and_model(self):
        make, model = extract_camera_info(io.BytesIO(_jpeg_bytes(make="Canon", model="EOS R5")))
        self.assertEqual((make, model), ("Canon", "EOS R5"))

    def test_extract_camera_info_none_when_absent(self):
        self.assertEqual(extract_camera_info(io.BytesIO(_jpeg_bytes())), (None, None))

    def test_extract_lens_model_reads_tag(self):
        self.assertEqual(
            extract_lens_model(io.BytesIO(_jpeg_bytes(lens_model="RF24-105mm F4 L IS USM"))), "RF24-105mm F4 L IS USM"
        )

    def test_extract_lens_model_none_when_absent(self):
        self.assertIsNone(extract_lens_model(io.BytesIO(_jpeg_bytes())))

    def test_extract_shutter_speed_formats_fast_speed_as_fraction(self):
        self.assertEqual(extract_shutter_speed(io.BytesIO(_jpeg_bytes(exposure_time=Fraction(1, 250)))), "1/250")

    def test_extract_shutter_speed_formats_long_exposure_in_seconds(self):
        self.assertEqual(extract_shutter_speed(io.BytesIO(_jpeg_bytes(exposure_time=Fraction(2, 1)))), "2s")

    def test_extract_shutter_speed_none_when_absent(self):
        self.assertIsNone(extract_shutter_speed(io.BytesIO(_jpeg_bytes())))

    def test_extract_aperture_reads_fnumber(self):
        self.assertAlmostEqual(extract_aperture(io.BytesIO(_jpeg_bytes(fnumber=Fraction(28, 10)))), 2.8, places=1)

    def test_extract_aperture_none_when_absent(self):
        self.assertIsNone(extract_aperture(io.BytesIO(_jpeg_bytes())))

    def test_extract_focal_length_reads_tag(self):
        self.assertAlmostEqual(
            extract_focal_length(io.BytesIO(_jpeg_bytes(focal_length=Fraction(50, 1)))), 50.0, places=1
        )

    def test_extract_focal_length_none_when_absent(self):
        self.assertIsNone(extract_focal_length(io.BytesIO(_jpeg_bytes())))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class ProcessImageUploadExifMetadataTests(TestCase):
    """process_image_upload() writes the new exif_* fields, and never overwrites them once set."""

    def _make_image_row(self, content: bytes) -> Image:
        profile = User.objects.create(username=f"u{Image.objects.count()}").profile
        return Image.objects.create(
            image=SimpleUploadedFile("shot.jpg", content, content_type="image/jpeg"), profile=profile
        )

    def test_camera_and_lens_fields_are_populated(self):
        content = _jpeg_bytes(
            make="Canon",
            model="EOS R5",
            lens_model="RF50mm F1.2",
            exposure_time=Fraction(1, 500),
            fnumber=Fraction(12, 10),
            focal_length=Fraction(50, 1),
        )
        row = self._make_image_row(content)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()
        self.assertEqual(row.exif_camera_make, "Canon")
        self.assertEqual(row.exif_camera_model, "EOS R5")
        self.assertEqual(row.exif_lens_model, "RF50mm F1.2")
        self.assertEqual(row.exif_shutter_speed, "1/500")
        self.assertAlmostEqual(float(row.exif_aperture), 1.2, places=1)
        self.assertAlmostEqual(float(row.exif_focal_length), 50.0, places=1)

    def test_exif_coordinates_are_write_once(self):
        """A reprocess must not clobber an already-recorded exif_latitude/exif_longitude."""
        row = self._make_image_row(_jpeg_bytes())
        row.exif_latitude, row.exif_longitude = 12.5, -34.5
        row.save(update_fields=["exif_latitude", "exif_longitude"])

        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()

        self.assertEqual(float(row.exif_latitude), 12.5)
        self.assertEqual(float(row.exif_longitude), -34.5)

    def test_missing_metadata_leaves_new_fields_null(self):
        row = self._make_image_row(_jpeg_bytes())
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_image_upload(row.pk)
        row.refresh_from_db()
        for field in (
            "exif_altitude",
            "exif_pitch",
            "exif_roll",
            "exif_camera_make",
            "exif_camera_model",
            "exif_lens_model",
            "exif_shutter_speed",
            "exif_aperture",
            "exif_focal_length",
            "exif_floor",
        ):
            self.assertIsNone(getattr(row, field), field)
