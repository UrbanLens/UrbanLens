"""A JPEG's pixel size is read from its frame header, never by decoding it (P324).

Pillow appears here only to write fixtures and to say what size a decoder sees.
"""

from __future__ import annotations

import ast
import io
import pathlib
import struct
from typing import Any

from PIL import Image as PILImage

from hypothesis import example, given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.media import jpeg_header
from urbanlens.dashboard.services.media.jpeg_header import jpeg_dimensions

_hyp = settings(max_examples=60, deadline=None)

_SOI = b"\xff\xd8"
#: Every start-of-frame marker: baseline, extended, progressive and lossless, Huffman and arithmetic.
_SOF_MARKERS = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


def _jpeg(width: int, height: int, *, mode: str = "RGB", **save: Any) -> bytes:
    buffer = io.BytesIO()
    PILImage.new(mode, (width, height)).save(buffer, format="JPEG", **save)
    return buffer.getvalue()


def _decoded_size(data: bytes) -> tuple[int, int]:
    with PILImage.open(io.BytesIO(data)) as decoded:
        decoded.load()
        return decoded.size


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def _frame(width: int, height: int, *, marker: int = 0xC0) -> bytes:
    """A one-component frame header: precision, height, width, component count, then the component."""
    return _segment(marker, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")


_encodings = st.fixed_dictionaries(
    {
        "mode": st.sampled_from(["RGB", "L", "CMYK"]),
        "progressive": st.booleans(),
        "optimize": st.booleans(),
        "quality": st.integers(min_value=1, max_value=100),
    },
    optional={
        "subsampling": st.sampled_from([0, 1, 2]),
        "comment": st.binary(min_size=1, max_size=64),
        "restart_marker_blocks": st.integers(min_value=1, max_value=8),
        "exif": st.just(PILImage.Exif().tobytes()),
    },
)


class ReaderIsNotADecoderTests(SimpleTestCase):
    def test_the_reader_imports_nothing(self) -> None:
        """No image library and no native parser: what runs is the slicing in the module itself."""
        tree = ast.parse(pathlib.Path(jpeg_header.__file__).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }

        self.assertEqual(imported, {"__future__"})


class PillowWrittenJpegTests(SimpleTestCase):
    """The size read from the header is the size a decoder sees."""

    @_hyp
    @given(
        width=st.integers(min_value=1, max_value=2048),
        height=st.integers(min_value=1, max_value=2048),
        encoding=_encodings,
    )
    @example(width=512, height=3, encoding={"mode": "RGB", "progressive": False, "optimize": False, "quality": 75})
    @example(width=1, height=1, encoding={"mode": "RGB", "progressive": True, "optimize": False, "quality": 75})
    @example(width=4, height=4, encoding={"mode": "L", "progressive": False, "optimize": True, "quality": 75})
    def test_the_size_is_the_one_pillow_decodes(self, width: int, height: int, encoding: dict[str, Any]) -> None:
        options = {name: value for name, value in encoding.items() if name != "mode"}
        data = _jpeg(width, height, mode=encoding["mode"], **options)

        self.assertEqual(jpeg_dimensions(data), _decoded_size(data))
        self.assertEqual(jpeg_dimensions(data), (width, height))

    @_hyp
    @given(
        width=st.integers(min_value=1, max_value=600),
        height=st.integers(min_value=1, max_value=600),
        progressive=st.booleans(),
        cut=st.integers(min_value=0),
    )
    def test_a_copy_cut_short_reads_as_unknown_or_its_true_size(
        self, width: int, height: int, progressive: bool, cut: int
    ) -> None:
        data = _jpeg(width, height, progressive=progressive)

        self.assertIn(jpeg_dimensions(data[: cut % (len(data) + 1)]), {None, (width, height)})

    def test_the_header_alone_is_enough(self) -> None:
        """Nothing past the frame header is read: no tables, no scan, no end-of-image marker."""
        self.assertEqual(jpeg_dimensions(_SOI + _frame(640, 480)), (640, 480))


class HandWrittenHeaderTests(SimpleTestCase):
    def test_every_start_of_frame_marker_is_read(self) -> None:
        for marker in _SOF_MARKERS:
            with self.subTest(marker=f"{marker:02X}"):
                self.assertEqual(jpeg_dimensions(_SOI + _frame(300, 7, marker=marker)), (300, 7))

    def test_dht_jpg_and_dac_share_the_range_and_are_not_frames(self) -> None:
        decoys = b"".join(_segment(marker, struct.pack(">BHHB", 8, 1, 1, 1)) for marker in (0xC4, 0xC8, 0xCC))

        self.assertEqual(jpeg_dimensions(_SOI + decoys + _frame(300, 7, marker=0xC2)), (300, 7))

    def test_fill_bytes_and_standalone_markers_before_the_frame_are_stepped_over(self) -> None:
        tem, rst0, rst7 = b"\xff\x01", b"\xff\xd0", b"\xff\xd7"
        data = _SOI + b"\xff\xff\xff" + tem + rst0 + rst7 + _segment(0xE0, b"JFIF\x00") + b"\xff\xff" + _frame(9, 2)

        self.assertEqual(jpeg_dimensions(data), (9, 2))

    def test_only_the_first_frame_counts(self) -> None:
        self.assertEqual(jpeg_dimensions(_SOI + _frame(2, 2) + _frame(800, 600)), (2, 2))

    def test_a_frame_cut_anywhere_is_unknown(self) -> None:
        data = _SOI + _frame(640, 480)
        for end in range(len(data)):
            with self.subTest(end=end):
                self.assertIsNone(jpeg_dimensions(data[:end]))

    def test_a_height_left_to_a_dnl_marker_is_unknown(self) -> None:
        self.assertIsNone(jpeg_dimensions(_SOI + _frame(640, 0)))

    def test_a_zero_width_is_unknown(self) -> None:
        self.assertIsNone(jpeg_dimensions(_SOI + _frame(0, 480)))

    def test_a_frame_header_too_short_to_hold_its_fields_is_unknown(self) -> None:
        self.assertIsNone(jpeg_dimensions(_SOI + _segment(0xC0, struct.pack(">BHH", 8, 480, 640))))

    def test_a_scan_or_the_end_before_any_frame_is_unknown(self) -> None:
        for marker in (0xDA, 0xD9, 0xD8):
            with self.subTest(marker=f"{marker:02X}"):
                self.assertIsNone(jpeg_dimensions(_SOI + bytes((0xFF, marker)) + _frame(640, 480)))

    def test_a_segment_length_under_two_is_unknown(self) -> None:
        self.assertIsNone(jpeg_dimensions(_SOI + b"\xff\xe0\x00\x01" + _frame(640, 480)))

    def test_bytes_between_segments_are_unknown(self) -> None:
        self.assertIsNone(jpeg_dimensions(_SOI + b"\x00" + _frame(640, 480)))


class NotAJpegTests(SimpleTestCase):
    def test_other_formats_and_garbage_are_unknown(self) -> None:
        def encoded(format_name: str) -> bytes:
            buffer = io.BytesIO()
            PILImage.new("RGB", (640, 480)).save(buffer, format=format_name)
            return buffer.getvalue()

        for name, data in {
            "empty": b"",
            "SOI only": _SOI,
            "PNG": encoded("PNG"),
            "WebP": encoded("WEBP"),
            "GIF": encoded("GIF"),
            "text": b"not a jpeg",
            "P320's fixture": b"\xff\xd8\xff\xe0fake-jpeg-bytes",
        }.items():
            with self.subTest(name):
                self.assertIsNone(jpeg_dimensions(data))

    @_hyp
    @given(st.binary(max_size=4096))
    def test_arbitrary_bytes_never_raise(self, data: bytes) -> None:
        self._assert_unknown_or_a_size(jpeg_dimensions(data))

    @_hyp
    @given(st.binary(max_size=4096))
    def test_arbitrary_bytes_after_a_start_of_image_never_raise(self, data: bytes) -> None:
        self._assert_unknown_or_a_size(jpeg_dimensions(_SOI + data))

    @_hyp
    @given(st.lists(st.tuples(st.integers(min_value=0), st.integers(min_value=0, max_value=255)), max_size=12))
    def test_a_corrupted_jpeg_never_raises(self, damage: list[tuple[int, int]]) -> None:
        data = bytearray(_jpeg(64, 48, progressive=True, comment=b"x"))
        for position, value in damage:
            data[position % len(data)] = value

        self._assert_unknown_or_a_size(jpeg_dimensions(bytes(data)))

    def _assert_unknown_or_a_size(self, size: tuple[int, int] | None) -> None:
        if size is not None:
            width, height = size
            self.assertTrue(1 <= width <= 0xFFFF and 1 <= height <= 0xFFFF, size)
