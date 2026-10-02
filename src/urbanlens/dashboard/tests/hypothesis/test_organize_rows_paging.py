"""The Organize page's label rows arrive a page at a time (P66).

The tab renders its first page, a sentinel row fetches the next as it scrolls into view, and anything that
needs every row - a filter, the tree view, select-all - asks for the rest in one request. A write
re-renders as many rows as the client had loaded, which it says in ``X-Org-Rows-Loaded``.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q
from django.test import override_settings
from django.urls import reverse
import lxml.html
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.labels.organize_rows import (
    ROWS_LOADED_HEADER,
    organize_rows_page,
    organize_rows_queryset,
)

_PAGE = 3


class PageSizeSettingTests(TestCase):
    def test_the_page_size_is_a_real_setting(self) -> None:
        """The other tests override it, which would invent it if production lacked it."""
        self.assertTrue(hasattr(settings, "ORGANIZE_ROWS_PAGE_SIZE"))
        self.assertGreater(settings.ORGANIZE_ROWS_PAGE_SIZE, 0)


@override_settings(ORGANIZE_ROWS_PAGE_SIZE=_PAGE)
class _RowsFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other = Profile.objects.get(user=baker.make(User))
        self.client.force_login(self.user)
        # A new profile starts with default labels, and global ones are everyone's; both would land on these pages.
        Label.objects.filter(Q(profile__isnull=True) | Q(profile=self.profile)).delete()

    def _tags(self, count: int, *, profile: Profile | None = None, prefix: str = "tag") -> list[Label]:
        return [
            ensure_label(profile=profile or self.profile, name=f"{prefix} {i:02d}", kind=KIND_TAG) for i in range(count)
        ]

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> lxml.html.HtmlElement:
        response = self.client.get(url, params or {}, headers=headers or {})
        self.assertEqual(response.status_code, 200)
        return lxml.html.fragment_fromstring(response.content.decode() or "<div></div>", create_parent="div")

    @staticmethod
    def _ids(root: lxml.html.HtmlElement) -> list[int]:
        return [int(card.get("data-id")) for card in root.find_class("tag-card")]

    @staticmethod
    def _more(root: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
        found = root.find_class("organize-rows-more")
        return found[0] if found else None

    def _rows_url(self, url_kind: str = "tag") -> str:
        return reverse("label.rows", kwargs={"label_kind": url_kind})

    def _walk(self, url_kind: str = "tag") -> list[int]:
        """Every id the tab shows, following each sentinel the way htmx does."""
        root = self._get(self._rows_url(url_kind))
        ids = self._ids(root)
        for _ in range(100):
            more = self._more(root)
            if more is None:
                return ids
            root = self._get(more.get("hx-get"))
            ids += self._ids(root)
        self.fail("the sentinel never ran out")
        return ids

    def _expected(self, kind: str = KIND_TAG) -> list[int]:
        return list(organize_rows_queryset(kind, self.profile).values_list("pk", flat=True))


class FirstPageTests(_RowsFixture):
    def test_the_first_page_is_capped_and_offers_the_rest(self) -> None:
        self._tags(_PAGE + 4)

        root = self._get(self._rows_url())

        self.assertEqual(len(self._ids(root)), _PAGE)
        more = self._more(root)
        self.assertIsNotNone(more)
        self.assertIn("4 more", more.text_content())

    def test_a_tab_that_fits_on_one_page_has_no_sentinel(self) -> None:
        self._tags(_PAGE)

        root = self._get(self._rows_url())

        self.assertEqual(len(self._ids(root)), _PAGE)
        self.assertIsNone(self._more(root))

    def test_the_sentinel_swaps_only_itself(self) -> None:
        """The panel around it carries ``hx-target`` for its own deferred load, and htmx inherits it: without an
        explicit target the next page would replace the whole list."""
        self._tags(_PAGE + 1)

        more = self._more(self._get(self._rows_url()))

        self.assertEqual(more.get("hx-target"), "this")
        self.assertEqual(more.get("hx-swap"), "outerHTML")
        self.assertIn("intersect", more.get("hx-trigger"))

    def test_the_active_tab_paints_one_page_and_a_sentinel(self) -> None:
        self._tags(_PAGE + 2)

        response = self.client.get(reverse("organize.index"), {"tab": "tags"})

        root = lxml.html.fromstring(response.content.decode())
        rows = root.get_element_by_id("tag-rows")
        self.assertEqual(len(self._ids(rows)), _PAGE)
        self.assertIsNotNone(self._more(rows))
        self.assertTrue(
            root.get_element_by_id("category-rows").find_class("organize-section-loading"),
            "an inactive tab still defers",
        )


class WalkTests(_RowsFixture):
    def test_walking_the_pages_yields_every_label_once_in_display_order(self) -> None:
        # Two orders, and a global tag sharing an own tag's name and order: only the pk tells those two apart.
        made = [
            ensure_label(profile=self.profile, name=name, kind=KIND_TAG, order=order)
            for name, order in (("delta", 2), ("alpha", 0), ("charlie", 2), ("bravo", 0), ("echo", 0), ("foxtrot", 1))
        ]
        made.append(ensure_label(profile=None, name="bravo", kind=KIND_TAG, order=0))
        names = {label.pk: label.name for label in made}

        walked = self._walk()

        self.assertEqual(walked, self._expected())
        self.assertEqual(len(walked), len(set(walked)))
        self.assertEqual(
            [names[pk] for pk in walked if pk in names],
            ["charlie", "delta", "foxtrot", "alpha", "bravo", "bravo", "echo"],
        )

    def test_every_kind_pages(self) -> None:
        for kind, url_kind in (
            (KIND_TAG, "tag"),
            (KIND_CATEGORY, "category"),
            (KIND_STATUS, "status"),
            (KIND_USER, "people"),
            (KIND_MEDIA, "media"),
        ):
            with self.subTest(kind=kind):
                for i in range(_PAGE * 2 + 1):
                    ensure_label(profile=self.profile, name=f"{kind} {i}", kind=kind)

                walked = self._walk(url_kind)

                self.assertEqual(walked, self._expected(kind))
                self.assertGreater(len(walked), _PAGE * 2)

    def test_a_label_created_before_the_cursor_is_not_shown_twice(self) -> None:
        self._tags(_PAGE * 2)
        first = self._get(self._rows_url())

        ensure_label(profile=self.profile, name="aaa first", kind=KIND_TAG, order=99)
        second = self._get(self._more(first).get("hx-get"))

        self.assertFalse(set(self._ids(first)) & set(self._ids(second)))
        self.assertEqual(
            self._ids(first) + self._ids(second),
            [pk for pk in self._expected() if pk != Label.objects.get(name="aaa first").pk],
        )

    def test_deleting_the_last_label_shown_leaves_no_gap(self) -> None:
        """The cursor is the last row's sort key, not its pk, so the row itself can be gone."""
        self._tags(_PAGE * 2)
        first = self._get(self._rows_url())
        Label.objects.filter(pk=self._ids(first)[-1]).delete()

        second = self._get(self._more(first).get("hx-get"))

        self.assertEqual(self._ids(first)[:-1] + self._ids(second), self._expected())

    def test_the_rest_comes_in_one_request(self) -> None:
        self._tags(_PAGE * 3)
        first = self._get(self._rows_url())

        rest = self._get(self._more(first).get("data-rest-url"))

        self.assertIsNone(self._more(rest))
        self.assertEqual(self._ids(first) + self._ids(rest), self._expected())

    def test_a_filter_can_reach_a_label_the_first_page_did_not_show(self) -> None:
        """The client filters what is in the DOM, so it loads the rest first; the label it is looking for has to
        be in that response."""
        self._tags(_PAGE * 2)
        wanted = ensure_label(profile=self.profile, name="zzz needle", kind=KIND_TAG)
        first = self._get(self._rows_url())
        self.assertNotIn(wanted.pk, self._ids(first))

        rest = self._get(self._more(first).get("data-rest-url"))

        self.assertIn(wanted.pk, self._ids(rest))

    def test_an_empty_continuation_is_not_an_empty_tab(self) -> None:
        tags = self._tags(_PAGE + 1)
        first = self._get(self._rows_url())
        Label.objects.filter(pk=tags[-1].pk).delete()

        second = self._get(self._more(first).get("hx-get"))

        self.assertEqual(self._ids(second), [])
        self.assertFalse(second.find_class("tag-empty"))
        self.assertIsNone(self._more(second))

    def test_a_malformed_cursor_is_refused(self) -> None:
        for params in (
            {"after_order": "x", "after_name": "a", "after_id": "1"},
            {"after_order": "1"},
            {"after_id": "1", "after_name": "a"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self._rows_url(), params).status_code, 400)


class LoadedExtentTests(_RowsFixture):
    def test_the_header_asks_for_what_was_loaded(self) -> None:
        self._tags(_PAGE * 3)
        for value, expected, more in (
            ("7", 7, True),
            ("all", _PAGE * 3, False),
            ("1", _PAGE, True),
            ("junk", _PAGE, True),
            ("99999999999999999999", _PAGE * 3, False),
        ):
            with self.subTest(value=value):
                root = self._get(self._rows_url(), headers={ROWS_LOADED_HEADER: value})
                self.assertEqual(len(self._ids(root)), expected)
                self.assertEqual(self._more(root) is not None, more)

    def test_all_from_the_start_is_every_row(self) -> None:
        self._tags(_PAGE * 2)

        root = self._get(self._rows_url(), {"all": "1"})

        self.assertEqual(self._ids(root), self._expected())
        self.assertIsNone(self._more(root))

    def test_a_bulk_write_rerenders_what_the_client_had_loaded(self) -> None:
        tags = self._tags(_PAGE * 3)
        response = self.client.post(
            reverse("label.bulk_delete", kwargs={"label_kind": "tag"}),
            json.dumps({"ids": [tags[0].pk]}),
            content_type="application/json",
            headers={ROWS_LOADED_HEADER: str(_PAGE * 2)},
        )
        root = lxml.html.fragment_fromstring(response.content.decode(), create_parent="div")

        self.assertEqual(self._ids(root), self._expected()[: _PAGE * 2])
        self.assertIsNotNone(self._more(root))

    def test_a_form_write_rerenders_what_the_client_had_loaded(self) -> None:
        tags = self._tags(_PAGE * 3)
        response = self.client.post(
            reverse("label.delete", kwargs={"label_kind": "tag", "label_id": tags[0].pk}),
            headers={ROWS_LOADED_HEADER: "all"},
        )
        root = lxml.html.fragment_fromstring(response.content.decode(), create_parent="div")

        self.assertEqual(self._ids(root), self._expected())
        self.assertIsNone(self._more(root))

    def test_a_write_rerenders_from_the_top_whatever_its_url_says(self) -> None:
        """A cursor means "continue the list", which a write's re-render never does; nor may it refuse after writing."""
        tags = self._tags(_PAGE * 2)
        url = reverse("label.delete", kwargs={"label_kind": "tag", "label_id": tags[-1].pk})
        response = self.client.post(f"{url}?after_order=x&after_name=a")
        self.assertEqual(response.status_code, 200)
        root = lxml.html.fragment_fromstring(response.content.decode(), create_parent="div")

        self.assertEqual(self._ids(root), self._expected()[:_PAGE])
        self.assertFalse(Label.objects.filter(pk=tags[-1].pk).exists())

    def test_a_write_without_the_header_renders_the_first_page(self) -> None:
        tags = self._tags(_PAGE * 3)
        response = self.client.post(reverse("label.delete", kwargs={"label_kind": "tag", "label_id": tags[0].pk}))
        root = lxml.html.fragment_fromstring(response.content.decode(), create_parent="div")

        self.assertEqual(len(self._ids(root)), _PAGE)


class VisibilityTests(_RowsFixture):
    def test_another_accounts_labels_never_appear_on_any_page(self) -> None:
        self._tags(_PAGE * 2)
        theirs = {label.pk for label in self._tags(_PAGE * 2, profile=self.other, prefix="tag")}

        self.assertFalse(set(self._walk()) & theirs)

    def test_a_cursor_built_from_another_accounts_label_reveals_nothing_of_theirs(self) -> None:
        self._tags(_PAGE)
        theirs = self._tags(_PAGE * 2, profile=self.other, prefix="aaa")
        params = {"after_order": theirs[0].order, "after_name": theirs[0].name, "after_id": theirs[0].pk, "all": "1"}

        ids = self._ids(self._get(self._rows_url(), params))

        self.assertFalse(set(ids) & {label.pk for label in theirs})
        self.assertTrue(set(ids) <= set(self._expected()))

    def test_global_tags_page_but_global_categories_never_appear(self) -> None:
        self._tags(_PAGE * 2)
        global_tag = ensure_label(profile=None, name="zz global tag", kind=KIND_TAG)
        global_category = ensure_label(profile=None, name="zz global category", kind=KIND_CATEGORY)
        for i in range(_PAGE * 2):
            ensure_label(profile=self.profile, name=f"category {i}", kind=KIND_CATEGORY)

        self.assertIn(global_tag.pk, self._walk("tag"))
        self.assertNotIn(global_category.pk, self._walk("category"))

    def test_a_global_tag_on_a_later_page_offers_the_same_actions(self) -> None:
        self._tags(_PAGE * 2)
        global_tag = ensure_label(profile=None, name="zz global tag", kind=KIND_TAG)

        def card_titles() -> set[str]:
            root = self._get(self._rows_url(), {"all": "1"}, {ROWS_LOADED_HEADER: "all"})
            card = next(card for card in root.find_class("tag-card") if card.get("data-id") == str(global_tag.pk))
            return {el.get("title") for el in card.iter() if el.get("title")}

        self.assertNotIn(global_tag.pk, self._ids(self._get(self._rows_url())))
        as_user = card_titles()
        self.assertIn("Customize your display of this global label", as_user)
        self.assertNotIn("Edit global label", as_user)
        self.assertNotIn("Delete", as_user)

        self.client.force_login(self.admin)
        self.assertIn("Edit global label", card_titles())


@override_settings(ORGANIZE_ROWS_PAGE_SIZE=_PAGE)
class PagingPropertyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    @hyp_settings(max_examples=30, deadline=None)
    @given(
        specs=st.lists(
            st.tuples(st.integers(0, 2), st.sampled_from(["a", "b", "c", "d"]), st.booleans()),
            max_size=10,
            unique_by=lambda s: (s[1], s[2]),
        ),
        limit=st.integers(1, 4),
    )
    def test_pages_concatenate_to_the_whole_list(self, specs: list[tuple[int, str, bool]], limit: int) -> None:
        for order, name, is_global in specs:
            Label.objects.create(profile=None if is_global else self.profile, kind=KIND_TAG, name=name, order=order)

        walked: list[int] = []
        page = organize_rows_page(KIND_TAG, self.profile, limit=limit)
        walked += [label.pk for label in page.labels]
        while page.next_cursor is not None:
            self.assertLessEqual(len(page.labels), limit)
            page = organize_rows_page(KIND_TAG, self.profile, after=page.next_cursor, limit=limit)
            walked += [label.pk for label in page.labels]

        self.assertEqual(walked, list(organize_rows_queryset(KIND_TAG, self.profile).values_list("pk", flat=True)))


class CursorUrlTests(_RowsFixture):
    def test_a_name_with_url_syntax_survives_the_round_trip(self) -> None:
        for i in range(_PAGE - 1):
            ensure_label(profile=self.profile, name=f"top {i}", kind=KIND_TAG, order=10)
        odd = ensure_label(profile=self.profile, name="a&b=c?d #e%20+f", kind=KIND_TAG, order=9)
        self._tags(_PAGE)

        first = self._get(self._rows_url())

        self.assertEqual(self._ids(first)[-1], odd.pk)
        self.assertEqual(parse_qs(urlsplit(self._more(first).get("hx-get")).query)["after_name"], [odd.name])
        self.assertEqual(self._walk(), self._expected())
