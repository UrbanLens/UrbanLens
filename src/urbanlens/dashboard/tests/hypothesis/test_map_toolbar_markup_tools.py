"""The map toolbar's markup tools name their tool in ``data-markup-tool`` for the markup toolbar to act on."""

from __future__ import annotations

import re

from django.template import Context, Template
from django.test import SimpleTestCase

TOOLS = {
    "markup_line": "line",
    "markup_arrow": "arrow",
    "markup_freehand": "freehand",
    "markup_text": "text",
    "markup_square": "square",
    "markup_circle": "circle",
    "markup_polygon": "polygon",
}


class MarkupToolButtonTests(SimpleTestCase):
    def test_each_markup_tool_names_its_tool_and_needs_no_global(self) -> None:
        html = Template("{% load map_components %}{% map_toolbar tools panel_id='markup-test-buttons' %}").render(
            Context({"tools": ",".join(TOOLS)})
        )
        buttons = re.findall(r"<button\b[^>]*\bclass=\"map-btn-icon\"[^>]*>", html, re.DOTALL)
        self.assertEqual(len(buttons), len(TOOLS))
        self.assertEqual([re.search(r'data-markup-tool="([a-z]+)"', b).group(1) for b in buttons], list(TOOLS.values()))
        self.assertNotIn("onclick", html)
