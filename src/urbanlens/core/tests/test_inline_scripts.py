"""The inline-script guard sees every spelling of a script a browser runs."""

from __future__ import annotations

from urbanlens.core.tests.inline_scripts import executable_blocks, inline_blocks
from urbanlens.core.tests.testcase import SimpleTestCase


class InlineScriptSpellingTests(SimpleTestCase):
    def test_tag_case_and_closing_whitespace_do_not_hide_a_block(self) -> None:
        page = b"<SCRIPT>a()</SCRIPT ><Script type='module'>b()</script\n>"
        self.assertEqual(executable_blocks(page), [b"a()", b"b()"])

    def test_a_spaced_src_is_not_inline(self) -> None:
        self.assertEqual(inline_blocks(b'<script src = "/x.js"></script>'), [])

    def test_json_islands_are_inline_but_not_executable(self) -> None:
        page = b'<script type="application/json" id="cfg">{}</script>'
        self.assertEqual((inline_blocks(page), executable_blocks(page)), ([b"{}"], []))

    def test_a_longer_tag_name_does_not_close_a_block(self) -> None:
        self.assertEqual(inline_blocks(b"<script>x = '</scripts>'; y()</script>"), [b"x = '</scripts>'; y()"])
