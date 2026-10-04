"""A WKT or WKB geometry nested past what GEOS's readers can recurse through is refused before GEOS sees it.

GEOS reads both formats recursively, and tens of thousands of nested collections overflow its stack: thirty thousand
are a 600 KB WKT line. The process dies with SIGSEGV, and in ``media-worker`` it takes the task in the other slot with
it. Each test parses in a child process, so the crash fails the test rather than the test run.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

_DEPTH = 60_000

_CHILD = """
import sys
import django
django.setup()
from django.test.utils import override_settings
from urbanlens.dashboard.services.import_formats import wkt_wkb
with override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"), open(sys.argv[1], "rb") as handle:
    try:
        pins = list(getattr(wkt_wkb, sys.argv[2])(handle, None))
    except Exception as exc:
        print("refused", type(exc).__name__)
    else:
        print("pins", *sorted((round(pin["longitude"], 6), round(pin["latitude"], 6)) for pin in pins))
"""


def _nested_wkt(depth: int) -> str:
    return "GEOMETRYCOLLECTION(" * depth + "POINT(1 2)" + ")" * depth


def _nested_wkb(depth: int) -> bytes:
    collection = b"\x01" + struct.pack("<II", 7, 1)
    return collection * depth + b"\x01" + struct.pack("<Idd", 1, 1, 2)


class DeeplyNestedGeometryTests(SimpleTestCase):
    def _parse_in_a_child(self, content: bytes, parser: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(content)
        self.addCleanup(os.unlink, handle.name)
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, handle.name, parser],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"the parse killed its process: {result.stderr[-1500:]}")
        return result.stdout.strip().splitlines()[-1]

    def test_a_wkt_line_is_skipped_and_the_next_line_read(self) -> None:
        content = f"{_nested_wkt(_DEPTH)}\nPOINT (3 4)\n".encode()

        self.assertEqual(self._parse_in_a_child(content, "iter_wkt_pins"), "pins (3.0, 4.0)")

    def test_a_long_wkt_line_is_skipped_too(self) -> None:
        """Past the length the WKT is read without GEOS's WKT reader at all."""
        content = f"{_nested_wkt(_DEPTH * 4)}\nPOINT (3 4)\n".encode()

        self.assertEqual(self._parse_in_a_child(content, "iter_wkt_pins"), "pins (3.0, 4.0)")

    def test_a_hex_wkb_line_is_skipped_and_the_next_line_read(self) -> None:
        point = struct.pack("<BIdd", 1, 1, 3, 4).hex()
        content = f"{_nested_wkb(_DEPTH).hex()}\n{point}\n".encode()

        self.assertEqual(self._parse_in_a_child(content, "iter_wkb_pins"), "pins (3.0, 4.0)")

    def test_a_binary_wkb_file_is_refused(self) -> None:
        self.assertEqual(
            self._parse_in_a_child(_nested_wkb(_DEPTH), "iter_wkb_pins"), "refused UnreadableGeometryError"
        )

    def test_shallow_nesting_is_still_read(self) -> None:
        """Anti-vacuity: the refusal is for the depth, not for nesting."""
        self.assertEqual(self._parse_in_a_child(_nested_wkt(20).encode(), "iter_wkt_pins"), "pins (1.0, 2.0)")
        self.assertEqual(self._parse_in_a_child(_nested_wkb(20), "iter_wkb_pins"), "pins (1.0, 2.0)")
