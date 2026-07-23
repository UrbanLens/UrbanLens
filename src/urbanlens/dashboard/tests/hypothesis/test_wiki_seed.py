"""Tests for seeding a wiki's article from a confidently-matched Wikipedia article.

Covers:
- The pure HTML-extract-to-Markdown conversion (services.wiki_seed).
- seed_wiki_article_from_wikipedia's guards: no wiki, no cache, empty cache,
  existing article (never overwritten).
- The two trigger points: models.cache.signals firing on a "wikipedia"
  LocationCache write, and WikiCreationService.create_for_pin seeding
  immediately when a match is already cached at wiki-creation time.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki_seed import _attribution_line, _extract_html_to_markdown, _infobox_markdown, seed_pin_article_from_wikipedia, seed_wiki_article_from_wikipedia

_ARTICLE_DATA = {
    "title": "Eighteenth District School",
    "extract": "<p>The <b>Eighteenth District School</b> is a historic building.</p><h2>History</h2><p>Built in 1900.</p>",
    "url": "https://en.wikipedia.org/wiki/Eighteenth_District_School",
    "thumbnail": "",
    "description": "Historic school",
    "page_id": 123,
}


# -- Pure functions - plain pytest, no DB needed (unlike everything else in
# this file, which needs Postgres via the Django TestCase below). -----------


def test_extract_paragraph_and_bold_italic() -> None:
    md = _extract_html_to_markdown("<p>Hello <b>world</b>, this is <i>great</i>.</p>")
    assert md == "Hello **world**, this is *great*."  # nosec B101


def test_extract_headings() -> None:
    md = _extract_html_to_markdown("<h2>Top</h2><h3>Sub</h3>")
    assert md == "## Top\n\n### Sub"  # nosec B101


def test_extract_unordered_and_ordered_lists() -> None:
    md = _extract_html_to_markdown("<ul><li>a</li><li>b</li></ul><ol><li>x</li><li>y</li></ol>")
    assert "- a\n- b" in md  # nosec B101
    assert "1. x\n2. y" in md  # nosec B101


def test_extract_definition_list() -> None:
    md = _extract_html_to_markdown("<dl><dt>Term</dt><dd>Meaning</dd></dl>")
    assert md == "**Term**\n: Meaning"  # nosec B101


def test_extract_blockquote() -> None:
    md = _extract_html_to_markdown("<blockquote>Quoted text</blockquote>")
    assert md == "> Quoted text"  # nosec B101


def test_extract_empty_input_returns_empty_string() -> None:
    assert _extract_html_to_markdown("") == ""  # nosec B101


def test_extract_blank_paragraphs_are_skipped() -> None:
    md = _extract_html_to_markdown("<p>Real content.</p><p>   </p>")
    assert md == "Real content."  # nosec B101


def test_attribution_includes_title_and_url() -> None:
    line = _attribution_line({"title": "Some Article", "url": "https://en.wikipedia.org/wiki/Some_Article"})
    assert "[Wikipedia](https://en.wikipedia.org/wiki/Some_Article)" in line  # nosec B101
    assert "(Some Article)" in line  # nosec B101
    assert "CC BY-SA 4.0" in line  # nosec B101


def test_attribution_no_url_returns_empty_string() -> None:
    assert _attribution_line({"title": "Some Article", "url": ""}) == ""  # nosec B101


def test_infobox_markdown_renders_a_bullet_list() -> None:
    md = _infobox_markdown([["Established", "1900"], ["Country", "US"]])
    assert md == "- **Established:** 1900\n- **Country:** US"  # nosec B101


def test_infobox_markdown_empty_list_returns_empty_string() -> None:
    assert _infobox_markdown([]) == ""  # nosec B101


def test_infobox_markdown_none_returns_empty_string() -> None:
    assert _infobox_markdown(None) == ""  # nosec B101


def test_infobox_markdown_skips_pairs_with_blank_label_or_value() -> None:
    md = _infobox_markdown([["", "value"], ["label", ""], ["Real", "Row"]])
    assert md == "- **Real:** Row"  # nosec B101


def test_infobox_markdown_collapses_internal_whitespace() -> None:
    """A <br>-separated value (from a multi-line infobox cell) must not break the bullet list."""
    md = _infobox_markdown([["Type", "A\n  B"]])
    assert md == "- **Type:** A B"  # nosec B101


def test_infobox_markdown_never_produces_an_empty_table_header() -> None:
    """Regression guard: the previous GFM-table rendering's mandatory blank
    header row used to render as an empty <tr> once parsed into the article
    editor - see docs/prompts/completed.md. A bullet list has no header row."""
    md = _infobox_markdown([["Established", "1900"]])
    assert "| | |" not in md  # nosec B101


def test_infobox_markdown_malformed_pairs_are_ignored() -> None:
    assert _infobox_markdown([["only_one"], "not_a_list", ["a", "b", "c"]]) == ""  # nosec B101


def test_lead_image_markdown_renders_an_image() -> None:
    from urbanlens.dashboard.services.wiki_seed import _lead_image_markdown

    md = _lead_image_markdown({"title": "Eighteenth District School", "thumbnail": "https://upload.wikimedia.org/thumb.jpg"})
    assert md == "![Eighteenth District School](https://upload.wikimedia.org/thumb.jpg)"  # nosec B101


def test_lead_image_markdown_no_thumbnail_returns_empty_string() -> None:
    from urbanlens.dashboard.services.wiki_seed import _lead_image_markdown

    assert _lead_image_markdown({"title": "Some Article", "thumbnail": ""}) == ""  # nosec B101
    assert _lead_image_markdown({"title": "Some Article"}) == ""  # nosec B101


def test_lead_image_markdown_sanitizes_brackets_in_title() -> None:
    """A literal `]` in the title must not be able to close the Markdown image's alt text early."""
    from urbanlens.dashboard.services.wiki_seed import _lead_image_markdown

    md = _lead_image_markdown({"title": "Foo [bar]", "thumbnail": "https://example.test/x.jpg"})
    assert md == "![Foo (bar)](https://example.test/x.jpg)"  # nosec B101


def _location() -> Location:
    return baker.make(Location, latitude=40.5, longitude=-74.5)


class SeedWikiArticleFromWikipediaTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_no_wiki_on_location_returns_none(self) -> None:
        location = _location()
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)
        self.assertIsNone(seed_wiki_article_from_wikipedia(location))

    def test_no_cached_wikipedia_row_returns_none(self) -> None:
        location = _location()
        baker.make(Wiki, location=location)
        self.assertIsNone(seed_wiki_article_from_wikipedia(location))
        self.assertIsNone(Article.objects.filter(wiki__location=location).first())

    def test_empty_cached_data_returns_none(self) -> None:
        location = _location()
        baker.make(Wiki, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data={})
        self.assertIsNone(seed_wiki_article_from_wikipedia(location))

    def test_matched_article_seeds_the_wiki(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertIsNotNone(article)
        self.assertEqual(article.wiki_id, wiki.pk)
        self.assertIsNone(article.last_edited_by_id)
        self.assertIn("Eighteenth District School", article.content)
        self.assertIn("## History", article.content)
        self.assertIn("wikipedia.org/wiki/Eighteenth_District_School", article.content)
        self.assertIn("<strong>Eighteenth District School</strong>", article.content_html)
        revision = article.revisions.first()
        self.assertIsNotNone(revision)
        self.assertEqual(revision.edit_summary, "Seeded from Wikipedia")
        self.assertIsNone(revision.editor_id)

    def test_never_overwrites_an_existing_article(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        Article.objects.create(wiki=wiki, content="Someone already wrote this.")
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        result = seed_wiki_article_from_wikipedia(location)

        self.assertIsNone(result)
        article = Article.objects.get(wiki=wiki)
        self.assertEqual(article.content, "Someone already wrote this.")

    def test_matched_article_with_infobox_includes_the_facts_before_the_body(self) -> None:
        """Regression coverage for the "started from Wikipedia" seed missing
        the infobox (docs/prompts/completed.md)."""
        location = _location()
        baker.make(Wiki, location=location)
        data = {**_ARTICLE_DATA, "infobox": [["Established", "1900"], ["Country", "US"]]}
        LocationCache.objects.create(location=location, source="wikipedia", data=data)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertIsNotNone(article)
        self.assertIn("- **Established:** 1900", article.content)
        self.assertIn("- **Country:** US", article.content)
        # The facts list comes before the prose body, and the body/attribution
        # footer both still render normally alongside it.
        self.assertLess(article.content.index("Established"), article.content.index("Eighteenth District School"))
        self.assertIn("## History", article.content)
        self.assertIn("wikipedia.org/wiki/Eighteenth_District_School", article.content)

    def test_matched_article_with_no_infobox_key_omits_the_facts_list(self) -> None:
        """A location cached before this field existed (or a genuinely
        infobox-less article) must still seed normally, with no facts list."""
        location = _location()
        baker.make(Wiki, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertIsNotNone(article)
        self.assertNotIn("- **", article.content)

    def test_matched_article_with_thumbnail_includes_the_lead_image(self) -> None:
        location = _location()
        baker.make(Wiki, location=location)
        data = {**_ARTICLE_DATA, "thumbnail": "https://upload.wikimedia.org/thumb.jpg"}
        LocationCache.objects.create(location=location, source="wikipedia", data=data)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertIsNotNone(article)
        self.assertIn("![Eighteenth District School](https://upload.wikimedia.org/thumb.jpg)", article.content)
        # The lead image comes before the prose body.
        self.assertLess(article.content.index("upload.wikimedia.org"), article.content.index("historic building"))

    def test_matched_article_with_no_thumbnail_omits_the_lead_image(self) -> None:
        location = _location()
        baker.make(Wiki, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        article = seed_wiki_article_from_wikipedia(location)

        self.assertIsNotNone(article)
        self.assertNotIn("![", article.content)


class SeedPinArticleFromWikipediaTests(TestCase):
    """seed_pin_article_from_wikipedia's guards - the pin equivalent of SeedWikiArticleFromWikipediaTests."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_owner_setting_off_returns_none(self) -> None:
        self.profile.auto_create_pin_article_from_wikipedia = False
        self.profile.save(update_fields=["auto_create_pin_article_from_wikipedia"])
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        self.assertIsNone(seed_pin_article_from_wikipedia(pin))
        self.assertFalse(Article.objects.filter(pin=pin).exists())

    def test_no_cached_wikipedia_row_returns_none(self) -> None:
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        self.assertIsNone(seed_pin_article_from_wikipedia(pin))
        self.assertFalse(Article.objects.filter(pin=pin).exists())

    def test_empty_cached_data_returns_none(self) -> None:
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data={})
        self.assertIsNone(seed_pin_article_from_wikipedia(pin))

    def test_matched_article_seeds_the_pin(self) -> None:
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        article = seed_pin_article_from_wikipedia(pin)

        self.assertIsNotNone(article)
        self.assertEqual(article.pin_id, pin.pk)
        self.assertIsNone(article.last_edited_by_id)
        self.assertIn("Eighteenth District School", article.content)
        self.assertIn("## History", article.content)
        revision = article.revisions.first()
        self.assertIsNotNone(revision)
        self.assertEqual(revision.edit_summary, "Seeded from Wikipedia")
        self.assertIsNone(revision.editor_id)

    def test_never_overwrites_an_existing_article(self) -> None:
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        Article.objects.create(pin=pin, content="Someone already wrote this.")
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)

        result = seed_pin_article_from_wikipedia(pin)

        self.assertIsNone(result)
        article = Article.objects.get(pin=pin)
        self.assertEqual(article.content, "Someone already wrote this.")


class WikipediaCacheSignalTriggersSeedingTests(TestCase):
    """models.cache.signals: a fresh "wikipedia" LocationCache write seeds the wiki AND every pin's article."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_caching_a_matched_article_seeds_the_wiki(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertTrue(Article.objects.filter(wiki=wiki).exists())

    def test_caching_a_matched_article_seeds_every_pin_at_the_location(self) -> None:
        location = _location()
        pin_a = baker.make(Pin, profile=self.profile, location=location)
        other_profile = baker.make(User).profile
        pin_b = baker.make(Pin, profile=other_profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertTrue(Article.objects.filter(pin=pin_a).exists())
        self.assertTrue(Article.objects.filter(pin=pin_b).exists())

    def test_pin_owner_opted_out_is_skipped_but_others_still_seed(self) -> None:
        self.profile.auto_create_pin_article_from_wikipedia = False
        self.profile.save(update_fields=["auto_create_pin_article_from_wikipedia"])
        location = _location()
        opted_out_pin = baker.make(Pin, profile=self.profile, location=location)
        other_profile = baker.make(User).profile
        opted_in_pin = baker.make(Pin, profile=other_profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertFalse(Article.objects.filter(pin=opted_out_pin).exists())
        self.assertTrue(Article.objects.filter(pin=opted_in_pin).exists())

    def test_caching_a_no_match_result_does_not_create_an_article(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", {}, query_key="Some Query")

        self.assertFalse(Article.objects.filter(wiki=wiki).exists())
        self.assertFalse(Article.objects.filter(pin=pin).exists())

    def test_other_cache_sources_do_not_trigger_seeding(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "nominatim", _ARTICLE_DATA)

        self.assertFalse(Article.objects.filter(wiki=wiki).exists())
        self.assertFalse(Article.objects.filter(pin=pin).exists())

    def test_location_with_no_wiki_and_no_pins_does_not_crash(self) -> None:
        location = _location()
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA)
        # No assertion beyond "didn't raise" - there's nothing to seed.

    def test_caching_a_matched_article_adds_the_link_to_the_wiki(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        link = wiki.links.get(url=_ARTICLE_DATA["url"])
        self.assertEqual(link.name, "Wikipedia")

    def test_caching_a_matched_article_adds_the_link_to_every_pin(self) -> None:
        location = _location()
        pin_a = baker.make(Pin, profile=self.profile, location=location)
        other_profile = baker.make(User).profile
        pin_b = baker.make(Pin, profile=other_profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertTrue(pin_a.links.filter(url=_ARTICLE_DATA["url"]).exists())
        self.assertTrue(pin_b.links.filter(url=_ARTICLE_DATA["url"]).exists())

    def test_pin_opted_out_of_article_seeding_still_gets_the_link(self) -> None:
        """Link-adding is independent of the article auto-create opt-out."""
        self.profile.auto_create_pin_article_from_wikipedia = False
        self.profile.save(update_fields=["auto_create_pin_article_from_wikipedia"])
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertFalse(Article.objects.filter(pin=pin).exists())
        self.assertTrue(pin.links.filter(url=_ARTICLE_DATA["url"]).exists())

    def test_no_match_result_does_not_add_a_link(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", {}, query_key="Some Query")

        self.assertEqual(wiki.links.count(), 0)
        self.assertEqual(pin.links.count(), 0)

    def test_link_is_not_duplicated_on_repeated_cache_writes(self) -> None:
        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertEqual(pin.links.filter(url=_ARTICLE_DATA["url"]).count(), 1)

    def test_a_previously_removed_link_is_not_recreated(self) -> None:
        from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval

        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)
        PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LINK, value=_ARTICLE_DATA["url"])

        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(location, "wikipedia", _ARTICLE_DATA, query_key="Eighteenth District School")

        self.assertFalse(pin.links.filter(url=_ARTICLE_DATA["url"]).exists())


class WikiCreationSeedsFromAlreadyCachedArticleTests(TestCase):
    """services.locations.creation.WikiCreationService: seed immediately on wiki creation
    when a Wikipedia match was already cached for the location beforehand."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_create_for_pin_seeds_the_article_when_already_cached(self) -> None:
        from urbanlens.dashboard.services.locations.creation import WikiCreationService

        location = _location()
        LocationCache.objects.create(location=location, source="wikipedia", data=_ARTICLE_DATA)
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            wiki, created = WikiCreationService().create_for_pin(pin)

        self.assertTrue(created)
        article = Article.objects.filter(wiki=wiki).first()
        self.assertIsNotNone(article)
        self.assertIn("Eighteenth District School", article.content)

    def test_create_for_pin_without_a_cached_match_creates_no_article(self) -> None:
        from urbanlens.dashboard.services.locations.creation import WikiCreationService

        location = _location()
        pin = baker.make(Pin, profile=self.profile, location=location)

        with self.captureOnCommitCallbacks(execute=True):
            wiki, created = WikiCreationService().create_for_pin(pin)

        self.assertTrue(created)
        self.assertFalse(Article.objects.filter(wiki=wiki).exists())
