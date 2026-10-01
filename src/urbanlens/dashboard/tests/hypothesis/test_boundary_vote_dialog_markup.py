"""The boundary-vote dialog carries its vote URL, dismissal key and auto-open request for ``boundary-vote.ts``."""

from __future__ import annotations

import re
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse

TEMPLATE = "dashboard/partials/wiki/_boundary_vote_dialog.html"
OPTIONS = [
    {"id": 3, "label": "County parcel", "polygon": {"type": "Polygon", "coordinates": []}, "is_my_choice": False},
    {"id": 4, "label": "OpenStreetMap", "polygon": None, "is_my_choice": True},
]


def _dialog_tag(*, auto_open: bool) -> str:
    html = render_to_string(
        TEMPLATE,
        {"location": SimpleNamespace(slug="old-mill"), "boundary_vote": {"options": OPTIONS, "auto_open": auto_open}},
    )
    match = re.search(r"<dialog\b[^>]*>", html)
    assert match, html
    return match.group(0)


class BoundaryVoteDialogMarkupTests(SimpleTestCase):
    def test_the_dialog_names_its_endpoint_and_dismissal_key(self) -> None:
        tag = _dialog_tag(auto_open=False)
        self.assertIn(f'data-vote-url="{reverse("location.wiki.boundary_vote", args=["old-mill"])}"', tag)
        self.assertIn('data-dismiss-key="ul_boundary_vote_dismissed_old-mill"', tag)

    def test_auto_open_is_asked_for_only_when_the_context_says_so(self) -> None:
        self.assertIn("data-auto-open", _dialog_tag(auto_open=True))
        self.assertNotIn("data-auto-open", _dialog_tag(auto_open=False))

    def test_no_inline_script_or_style(self) -> None:
        html = render_to_string(
            TEMPLATE,
            {"location": SimpleNamespace(slug="old-mill"), "boundary_vote": {"options": OPTIONS, "auto_open": True}},
        )
        self.assertEqual(
            re.findall(r"<script\b[^>]*>", html), ['<script id="boundary-vote-options-data" type="application/json">']
        )
        self.assertNotIn("<style", html)
