"""A pin-detail panel gets card chrome exactly when it renders standalone.

Reported from staging: "Water & Hydrology is not styled the same as the other
cards". It was not alone - eight standalone panels had the same defect, and it
only stood out on that pin because the other seven had no data there and were
removed by their own 204.

The mechanism: a standalone panel's placeholder is a `card card--secondary` div
that the loaded panel replaces via `hx-swap="outerHTML"`, so the loaded markup
has to bring its own card. `_simple_info_panel.html` adds that class only when
`nested` is falsy - and panels were declaring `nested` themselves inside
`render_context`, where they cannot possibly know the answer. Whether a panel
ends up inside a tab strip (which supplies the chrome) or standalone is decided
by `_TABBED_PANEL_KEYS` in the controller, so the controller now sets it.

The invariant is the interesting part rather than the eight instances: chrome
follows placement, and placement is one dict away from changing.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.pin import _TABBED_PANEL_KEYS
from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, panel_sources


class PanelChromeInvariantTests(SimpleTestCase):
    """Placement decides chrome, and nothing else may."""

    def test_no_panel_decides_its_own_chrome(self) -> None:
        """``render_context`` must not be the thing that sets ``nested``.

        Left in place, a panel moved into or out of a tab strip keeps whatever
        chrome its own module happened to declare, which is how eight standalone
        panels ended up with no card.
        """
        import inspect

        offenders = []
        for key, source in panel_sources().items():
            if not isinstance(source, InfoPanelSource):
                continue
            try:
                body = inspect.getsource(source.render_context)
            except (OSError, TypeError):
                continue
            if '"nested"' in body or "'nested'" in body:
                offenders.append(key)

        self.assertEqual(
            sorted(offenders),
            [],
            "these panels set 'nested' in render_context; chrome is the controller's decision - see _TABBED_PANEL_KEYS",
        )

    def test_every_tabbed_key_is_a_real_panel(self) -> None:
        """A stale key in the tab dicts silently drops a panel out of its strip."""
        registered = {key for key, source in panel_sources().items() if isinstance(source, InfoPanelSource)}

        self.assertEqual(sorted(_TABBED_PANEL_KEYS - registered), [], "tab strips name panels that no longer exist")


class PanelChromeRenderingTests(TestCase):
    """The rendered markup, not just the flag."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _render(self, *, nested: bool) -> str:
        from django.template.loader import render_to_string

        return render_to_string(
            "dashboard/partials/pins/_simple_info_panel.html",
            {"section_id": "x-section", "icon": "public", "title": "X", "nested": nested, "facts": [], "chips": [], "meta": []},
        )

    def test_a_standalone_panel_brings_its_own_card(self) -> None:
        markup = self._render(nested=False)

        self.assertIn("card", markup, "a standalone panel replaces a card placeholder and must supply the card itself")

    def test_a_tabbed_panel_does_not_double_the_card(self) -> None:
        markup = self._render(nested=True)

        self.assertIn("nested", markup)
        self.assertNotIn('class="simple-info-panel card', markup, "a tabbed panel would nest a card inside the strip's own card")
