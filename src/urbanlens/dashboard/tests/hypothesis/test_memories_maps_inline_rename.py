"""Memories > Maps: map titles are edit-in-place.

Click the title, it becomes an input, blurring/Enter autosaves via a POST to
the existing markup_map.view_state endpoint (the same one the map editor
widget already autosaves title/viewport changes through - see
controllers/markup.py's _apply_view_state, which only touches whatever
fields are present in the request body). This file covers the page's
rendered markup carries the right hooks for that client-side behavior;
markup_map.view_state's own save/ownership behavior is already covered by
test_markup_map.py.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupMap


class MapTitleInlineEditMarkupTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _get(self):
        return self.client.get(reverse("memories.maps"))

    def test_title_element_carries_the_edit_hooks(self) -> None:
        markup_map = baker.make(MarkupMap, profile=self.profile, title="My Cool Route")
        content = self._get().content.decode()
        self.assertIn("memories-map-card-title--editable", content)
        self.assertIn(f'data-map-uuid="{markup_map.uuid}"', content)
        self.assertIn('data-raw-title="My Cool Route"', content)
        self.assertIn(">My Cool Route<", content)

    def test_untitled_map_shows_placeholder_text_but_empty_raw_title(self) -> None:
        baker.make(MarkupMap, profile=self.profile, title="")
        content = self._get().content.decode()
        self.assertIn('data-raw-title=""', content)
        self.assertIn(">Untitled map<", content)

    def test_page_renders_a_view_state_url_template_for_the_rename_script(self) -> None:
        """The client swaps the placeholder UUID for each card's real uuid at click time."""
        baker.make(MarkupMap, profile=self.profile, title="Some Map")
        content = self._get().content.decode()
        placeholder_url = reverse("markup_map.view_state", args=["00000000-0000-0000-0000-000000000000"])
        self.assertIn(placeholder_url, content)

    def test_raw_title_is_html_escaped(self) -> None:
        """A title containing quotes/angle brackets must not break out of the data attribute."""
        baker.make(MarkupMap, profile=self.profile, title='<script>alert(1)</script> & "quoted"')
        content = self._get().content.decode()
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;", content)
