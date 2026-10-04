"""Article rendering and persistence.
Security: rendered HTML is always sanitized with nh3 against a fixed allowlist before it is stored or returned - article HTML is community/user input and must never reach a template unsanitized."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import difflib
import html as html_lib
import logging
import re
from typing import TYPE_CHECKING, Any, Protocol

from django.db import transaction
from markdown_it import MarkdownIt
from markdown_it.common.html_re import HTML_TAG_RE
from markdown_it.common.utils import normalizeReference
from markdown_it.helpers import parseLinkDestination, parseLinkTitle
from mdit_py_plugins.footnote import footnote_plugin
import nh3

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    import datetime

    from markdown_it.token import Token

    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Tags allowed to survive sanitization. Everything else is stripped (content
#: kept, tag removed) by nh3.
_ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "ins",
    "kbd",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "section",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

#: Per-tag attribute allowlist for nh3.
_ALLOWED_ATTRIBUTES = {
    # "rel" is intentionally absent: nh3's link_rel manages it on every link.
    "a": {"href", "title", "target", "id", "class"},
    "img": {"src", "alt", "title"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan"},
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
    "li": {"id", "class"},
    "ol": {"class"},
    "hr": {"class"},
    "sup": {"class", "id"},
    "section": {"class"},
    "span": {"class"},
    "div": {"class"},
    "code": {"class"},
    "pre": {"class"},
    "table": {"class"},
    "blockquote": {"class"},
}

_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


@dataclass(slots=True)
class TocEntry:
    """One table-of-contents row extracted from the article's headings."""

    level: int
    title: str
    anchor: str


@dataclass(slots=True)
class RenderedArticle:
    """The sanitized rendering of one article body."""

    html: str = ""
    toc: list[TocEntry] = field(default_factory=list)
    has_references: bool = False


def _build_markdown() -> MarkdownIt:
    """Construct the shared MarkdownIt instance (module-level singleton)."""
    md = MarkdownIt("gfm-like").use(footnote_plugin)
    md.options["linkify"] = True
    return md


_MD = _build_markdown()

#: Where an inline image's address begins: ``![alt](``. Alt text stops at a bracket, so each match attempt ends where
#: the next one starts; the address itself is read the way markdown-it reads it, parentheses and all.
_INLINE_IMAGE = re.compile(r"!\[[^\[\]]*\]\(")
#: A reference-style image: ``![alt][label]``, ``![label][]`` or ``![label]``.
_REFERENCE_IMAGE = re.compile(r"!\[([^\[\]]*)\](?:\[([^\[\]]*)\])?")
#: A raw ``<img ...>`` tag. One with a ``>`` inside a quoted attribute is cut short there and left to rendering.
_HTML_IMAGE_TAG = re.compile(r"<img\b[^<>]*>", re.IGNORECASE)
#: The address in a raw ``<img>`` tag, quoted or bare.
_HTML_IMAGE_SOURCE = re.compile(r"""\ssrc\s*=\s*(?:"(https?://[^"]*)"|'(https?://[^']*)'|(https?://[^\s"'=<>`]+))""", re.IGNORECASE)
_LINK_SPACE = re.compile(r"[ \t\n]*")
#: The gap before a definition's address, which may start the next line, after a block quote's ``>``.
_DEFINITION_SPACE = re.compile(r"[ \t]*(?:\n[ \t>]*)?")
_BACKTICK_RUN = re.compile(r"`+")
#: A remote image in sanitized HTML, whose attributes nh3 always double-quotes.
_RENDERED_IMAGE = re.compile(r'(<img\b[^<>]*?\ssrc=")(https?://[^"]+)(")', re.IGNORECASE)


def _copies_of(urls: set[str]) -> dict[str, str]:
    from urbanlens.dashboard.services.media.remote_copies import RemoteImage, copy_urls

    return copy_urls(RemoteImage(url, "article") for url in urls) if urls else {}


def _code_spans(content: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """Inline code in ``content[start:end]``, one block's inline text: a run of backticks up to the next of its length."""
    runs = [match.span() for match in _BACKTICK_RUN.finditer(content, start, end)]
    by_length: dict[int, list[int]] = {}
    for index, (run_start, run_end) in enumerate(runs):
        by_length.setdefault(run_end - run_start, []).append(index)
    index = 0
    while index < len(runs):
        run_start, run_end = runs[index]
        same = by_length[run_end - run_start]
        following = bisect.bisect_right(same, index)
        if following < len(same):
            yield run_start, runs[same[following]][1]
            index = same[following] + 1
        else:
            index += 1


def _skip(pattern: re.Pattern[str], content: str, position: int, limit: int) -> int:
    match = pattern.match(content, position, limit)
    return match.end() if match else position


class _Spans:
    """Character ranges of article source, merged where they overlap, looked up in logarithmic time."""

    __slots__ = ("_ends", "_starts")

    def __init__(self, ranges: Iterable[tuple[int, int]]) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []
        for start, end in sorted(ranges):
            if self._ends and start < self._ends[-1]:
                self._ends[-1] = max(self._ends[-1], end)
            else:
                self._starts.append(start)
                self._ends.append(end)

    def __iter__(self) -> Iterator[tuple[int, int]]:
        return zip(self._starts, self._ends, strict=True)

    def around(self, position: int) -> tuple[int, int] | None:
        """The range holding *position*, if one does."""
        index = bisect.bisect_right(self._starts, position) - 1
        return (self._starts[index], self._ends[index]) if index >= 0 and position < self._ends[index] else None

    def __contains__(self, position: int) -> bool:
        return self.around(position) is not None


@dataclass(frozen=True, slots=True)
class _SourceImage:
    """Where an image's address sits in article source.

    Attributes:
        start: The address's first character.
        end: Just past its last.
        url: The address a browser loads for it, which is what rendering keys the copy by.
    """

    start: int
    end: int
    url: str


class _ImageScan:
    """Every image address in one article's source that the article displays.

    The block structure comes from markdown-it and the inline syntax from the helpers its own image rule uses. That finds
    what rendering would show without an inline parse, which some text makes take seconds.
    """

    def __init__(self, content: str) -> None:
        """Read the block structure of *content*.

        Args:
            content: Markdown article source.
        """
        self.content = content
        self.env: dict[str, Any] = {}
        blocks: list[Token] = []
        _MD.block.parse(content, _MD, self.env, blocks)
        self._line_starts = [0]
        for line in content.split("\n"):
            self._line_starts.append(self._line_starts[-1] + len(line) + 1)
        # Paragraphs, headings and table cells, which markdown-it reads as Markdown; code, HTML blocks and definitions not.
        self._inline = _Spans(self._lines(token.map) for token in blocks if token.type == "inline" and token.map)
        self._html_blocks = _Spans(self._lines(token.map) for token in blocks if token.type == "html_block" and token.map)
        self._code = _Spans(span for start, end in self._inline for span in _code_spans(content, start, end))

    def _lines(self, lines: list[int]) -> tuple[int, int]:
        return self._line_starts[lines[0]], min(self._line_starts[min(lines[1], len(self._line_starts) - 1)], len(self.content))

    def _escaped(self, position: int) -> bool:
        run = position
        while run and self.content[run - 1] == "\\":
            run -= 1
        return (position - run) % 2 == 1

    def _markdown(self, start: int, end: int) -> tuple[int, int] | None:
        """The block of inline text holding ``content[start:end]``, when it is read as Markdown there."""
        block = self._inline.around(start)
        if block is None or end > block[1] or start in self._code or self._escaped(start):
            return None
        return block

    def _inline_destination(self, position: int, limit: int) -> _SourceImage | None:
        """The address of the inline image whose ``(`` ends at *position*, or None when the rest is not one."""
        content = self.content
        start = _skip(_LINK_SPACE, content, position, limit)
        destination = parseLinkDestination(content, start, limit)
        if not destination.ok:
            return None
        end = _skip(_LINK_SPACE, content, destination.pos, limit)
        if end != destination.pos:
            title = parseLinkTitle(content, end, limit)
            if title.ok:
                end = _skip(_LINK_SPACE, content, title.pos, limit)
        if end >= limit or content[end] != ")":
            return None
        bracketed = content[start] == "<"
        return _SourceImage(start + bracketed, destination.pos - bracketed, _MD.normalizeLink(destination.str))

    def inline_images(self) -> Iterator[_SourceImage]:
        """``![alt](address "title")``.

        Each is read no further than the next ``![alt](``: markdown-it reads an address through up to 32 nested
        parentheses, so text made of nothing but openers would otherwise be read 32 times over. An address or title
        containing one is left to rendering.
        """
        openers = [match.span() for match in _INLINE_IMAGE.finditer(self.content)]
        for index, (start, end) in enumerate(openers):
            block = self._markdown(start, end)
            if block is None:
                continue
            limit = min(block[1], openers[index + 1][0]) if index + 1 < len(openers) else block[1]
            if (image := self._inline_destination(end, limit)) is not None:
                yield image

    def html_images(self) -> Iterator[_SourceImage]:
        """``<img src="address">``, in an HTML block, or in inline text where markdown-it reads it as a tag."""
        for tag in _HTML_IMAGE_TAG.finditer(self.content):
            start, end = tag.span()
            if start not in self._html_blocks and (self._markdown(start, end) is None or not HTML_TAG_RE.match(tag.group())):
                continue
            source = _HTML_IMAGE_SOURCE.search(self.content, start, end)
            if source is not None:
                group = next(index for index in (1, 2, 3) if source.group(index) is not None)
                yield _SourceImage(source.start(group), source.end(group), html_lib.unescape(source.group(group)))

    def _image_labels(self) -> set[str]:
        """The normalized label of every reference an image is drawn from."""
        labels: set[str] = set()
        for match in _REFERENCE_IMAGE.finditer(self.content):
            if self._markdown(*match.span()) is None:
                continue
            alt, label = match.group(1), match.group(2)
            if label is None and self.content.startswith("(", match.end()):
                continue
            labels.add(normalizeReference(label or alt))
        labels.discard("")
        return labels

    def reference_images(self) -> Iterator[_SourceImage]:
        """The definition behind ``![alt][label]``, ``![label][]`` and ``![label]``, wherever it is written.

        A definition a link shares with an image is rewritten too, so the link then opens the copy.
        """
        references: dict[str, dict[str, Any]] = self.env.get("references", {})
        for label in self._image_labels() & references.keys():
            reference = references[label]
            if lines := reference.get("map"):
                image = self._definition(*self._lines(lines), reference["href"])
                if image is not None:
                    yield image

    def _definition(self, start: int, end: int, href: str) -> _SourceImage | None:
        """The address of the definition of *href* written between *start* and *end*.

        Footnote markers can come before it on its line (``[^1]: [label]: address``), so each ``[`` is tried in turn,
        and one that continues past a list item's indent is left to rendering.
        """
        content = self.content
        position = start
        while (opener := content.find("[", position, end)) >= 0:
            position = opener + 1
            while position < end and content[position] not in "[]":
                position += 2 if content[position] == "\\" else 1
            if not content.startswith("]:", position):
                continue
            address = _skip(_DEFINITION_SPACE, content, position + 2, len(content))
            destination = parseLinkDestination(content, address, len(content))
            if destination.ok and _MD.normalizeLink(destination.str) == href:
                bracketed = content[address] == "<"
                return _SourceImage(address + bracketed, destination.pos - bracketed, href)
        return None


def localize_article_images(content: str) -> str:
    """Point every remote image in article source at this site's copy, so the editor never loads it from its host.

    Covers inline, reference-style and raw HTML images. Links stay as they are, unless one shares a reference definition
    with an image, and so does code that shows image syntax. The original address is kept on the copy, keyed the way
    rendering keys it, so both find the same one.

    Args:
        content: Markdown article source.

    Returns:
        The source with each remote image address replaced.
    """
    scan = _ImageScan(content)
    images = sorted([*scan.inline_images(), *scan.html_images(), *scan.reference_images()], key=lambda image: image.start)
    copies = _copies_of({image.url for image in images})
    pieces: list[str] = []
    done = 0
    for image in images:
        copy = copies.get(image.url)
        if copy is None or image.start < done:
            continue
        pieces += (content[done : image.start], copy)
        done = image.end
    pieces.append(content[done:])
    return "".join(pieces)


def _localize_rendered_images(clean_html: str) -> str:
    copies = _copies_of({html_lib.unescape(match.group(2)) for match in _RENDERED_IMAGE.finditer(clean_html)})
    return _RENDERED_IMAGE.sub(lambda match: match.group(1) + copies.get(html_lib.unescape(match.group(2)), match.group(2)) + match.group(3), clean_html)


_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)
_SLUG_DASH = re.compile(r"[\s_-]+")


def _anchor_slug(title: str, used: set[str]) -> str:
    """Derive a unique, URL-safe anchor id for a heading title.

    Args:
        title: The heading's plain text.
        used: Anchors already assigned in this document (mutated in place).

    Returns:
        A unique anchor like ``"history"`` or ``"history-2"``.
    """
    base = _SLUG_DASH.sub("-", _SLUG_STRIP.sub("", title.strip().lower())).strip("-") or "section"
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def render_article(content: str) -> RenderedArticle:
    """Render Markdown article source to sanitized HTML plus a TOC.
    Headings are demoted one level (``#`` becomes ``<h2>``) so the article can never inject a second ``<h1>`` into the page, and each heading receives a stable ``id`` used by the table of contents.

    Args:
        content: The raw Markdown source (may be empty).

    Returns:
        The sanitized HTML, TOC entries, and whether references are present."""
    if not content or not content.strip():
        return RenderedArticle()

    tokens = _MD.parse(content)
    toc: list[TocEntry] = []
    used_anchors: set[str] = set()

    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1:]) if token.tag[1:].isdigit() else 2
            level = min(level + 1, 6)  # demote so the page keeps a single h1
            token.tag = f"h{level}"
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            title = inline.content.strip() if inline is not None and inline.type == "inline" else ""
            anchor = _anchor_slug(title or "section", used_anchors)
            token.attrSet("id", anchor)
            close = tokens[index + 2] if index + 2 < len(tokens) else None
            if close is not None and close.type == "heading_close":
                close.tag = f"h{level}"
            if title:
                toc.append(TocEntry(level=level, title=title, anchor=anchor))

    def _mark_external_links(inline_tokens) -> None:
        for child in inline_tokens or []:
            if child.type == "link_open":
                href = child.attrGet("href") or ""
                if href.startswith(("http://", "https://")):
                    child.attrSet("target", "_blank")
                    child.attrSet("class", "article-external-link")

    for token in tokens:
        if token.type == "inline":
            _mark_external_links(token.children)

    html = _MD.renderer.render(tokens, _MD.options, {})

    has_references = '<section class="footnotes">' in html
    if has_references:
        html = html.replace('<hr class="footnotes-sep" />', "", 1)
        html = html.replace(
            '<section class="footnotes">',
            '<section class="footnotes"><h2 class="article-references-title" id="article-references">References</h2>',
            1,
        )

    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    if has_references:
        toc.append(TocEntry(level=2, title="References", anchor="article-references"))
    return RenderedArticle(html=_localize_rendered_images(clean), toc=toc, has_references=has_references)


# Persistence


def get_article(*, pin: Pin | None = None, wiki: Wiki | None = None) -> Article | None:
    """Fetch the existing article for a pin or wiki, or None.

    Args:
        pin: The pin host (mutually exclusive with ``wiki``).
        wiki: The wiki host.

    Returns:
        The Article row, or None when none has been written yet.
    """
    from urbanlens.dashboard.models.article.model import Article

    if pin is not None:
        return Article.objects.filter(pin=pin).select_related("last_edited_by__user").first()
    if wiki is not None:
        return Article.objects.filter(wiki=wiki).select_related("last_edited_by__user").first()
    return None


def save_article(
    *,
    editor: Profile | None,
    content: str,
    edit_summary: str = "",
    pin: Pin | None = None,
    wiki: Wiki | None = None,
    restored_from: ArticleRevision | None = None,
) -> tuple[Article, ArticleRevision | None]:
    """Persist a new version of a pin/wiki article.

    Args:
        editor: The profile making the edit, or None for a system-initiated save (e.g. seeding a new wiki article from a matched Wikipedia article) - both ``Article.last_edited_by`` and ``ArticleRevision.editor`` are already nullable (SET_NULL) for exactly this...
        content: The complete new Markdown source.
        edit_summary: Optional one-line description of the change.
        pin: Host pin (mutually exclusive with ``wiki``).
        wiki: Host wiki.
        restored_from: When this save restores an older revision, that revision.

    Returns:
        Tuple of (article, revision) - revision is None for a no-op save.

    Raises:
        ValueError: Neither or both hosts were provided.
        Pin.DoesNotExist: The host pin was deleted before the save could hold it."""
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.pin.model import Pin

    if (pin is None) == (wiki is None):
        raise ValueError("Exactly one of pin or wiki must be provided.")

    content = localize_article_images((content or "").replace("\r\n", "\n").rstrip())
    with transaction.atomic():
        # Held until the revision commits, so a delete of the pin (pin_edit.delete_pin) waits or is waited for.
        if pin is not None and not Pin.objects.select_for_update(no_key=True).filter(pk=pin.pk).values_list("pk", flat=True):
            raise Pin.DoesNotExist(f"Pin {pin.pk} was deleted before its article could be saved.")
        article = get_article(pin=pin, wiki=wiki)
        if article is None:
            article = Article(pin=pin, wiki=wiki)
        elif article.content == content:
            return article, None

        rendered = render_article(content)
        article.content = content
        article.content_html = rendered.html
        article.toc = [{"level": entry.level, "title": entry.title, "anchor": entry.anchor} for entry in rendered.toc]
        article.last_edited_by = editor
        article.save()

        revision = ArticleRevision.objects.create(
            article=article,
            editor=editor,
            content=content,
            edit_summary=(edit_summary or "").strip()[:255],
            restored_from=restored_from,
        )
        if wiki is not None and editor is not None:
            from urbanlens.dashboard.services.notifications.change_notifications import announce_wiki_change

            announce_wiki_change(wiki, editor, "restored an earlier version of the article" if restored_from is not None else "edited the article")
    return article, revision


class ArticleConflictError(Exception):
    """Someone else saved the article while this editor was working on it.

    Attributes:
        current_revision_id: The id of the revision that is actually current, so a client can fetch it, show the other edit, and re-base."""

    def __init__(self, current_revision_id: int) -> None:
        """Record which revision is current."""
        super().__init__("This article changed while you were editing.")
        self.current_revision_id = current_revision_id


def latest_revision_id(article: Article | None) -> int | None:
    """Return the id of *article*'s newest revision, or None.

    Args:
        article: The article to inspect, or None when none exists yet.

    Returns:
        The newest ``ArticleRevision`` id, or None when the article is absent or has no revisions."""
    if article is None:
        return None
    latest = article.revisions.order_by("-created").first()
    return latest.id if latest is not None else None


def save_article_checked(
    *,
    editor: Profile | None,
    content: str,
    edit_summary: str = "",
    base_revision_id: int | None,
    viewer: Profile | None = None,
    pin: Pin | None = None,
    wiki: Wiki | None = None,
) -> tuple[Article, ArticleRevision | None]:
    """Save an article, refusing the write if it would clobber a concurrent edit.
    Wraps :func:`save_article` with the optimistic-concurrency check the article editor has always performed, moved here so the internal view and the external API cannot drift on it.

    Args:
        editor: The profile making the edit (None for system saves).
        content: The complete new Markdown source.
        edit_summary: Optional one-line description of the change.
        viewer: Who is saving, when the conflict check must be asked about what they were shown rather than what is stored - a concealed viewer.
        base_revision_id: The revision the editor started from, or None when they believe the article has no revisions yet.
        pin: Host pin (mutually exclusive with ``wiki``).
        wiki: Host wiki.

    Returns:
        Tuple of (article, revision) - revision is None for a no-op save.

    Raises:
        ArticleConflictError: The article moved on since *base_revision_id*.
        ValueError: Neither or both hosts were provided."""
    from urbanlens.dashboard.models.article.model import Article

    with transaction.atomic():
        article = get_article(pin=pin, wiki=wiki)
        if article is not None:
            # Lock the article row for the read-check-write below.
            # Without it the check is a TOCTOU: two editors who both loaded revision R both read
            # `latest_id == R`, both pass, and both append - so one editor's save silently stops
            # being the current article even though the conflict check exists to prevent exactly
            Article.objects.select_for_update().filter(pk=article.pk).first()
        latest_id = latest_revision_id(article)
        # A concealed viewer's editor was handed the newest revision *they* can see as its baseline,
        # so comparing it against the live newest is a conflict they can never clear - a permanent
        # 409 on an article that looks, to them, like nobody else has touched it.
        # Ask the question they can actually answer: has anything changed that they were shown?
        if article is not None and viewer is not None and wiki is not None:
            from urbanlens.dashboard.services.wiki.concealment import concealment_active, visible_rows

            if concealment_active(wiki, viewer):
                newest_visible = visible_rows(article.revisions.all(), wiki, viewer).order_by("-created", "-pk").first()
                latest_id = newest_visible.pk if newest_visible is not None else None
        if latest_id is not None and latest_id != base_revision_id:
            raise ArticleConflictError(latest_id)

        return save_article(editor=editor, content=content, edit_summary=edit_summary, pin=pin, wiki=wiki)


def restore_revision(*, scope_article: Article, revision: ArticleRevision, editor: Profile | None) -> tuple[Article, ArticleRevision | None]:
    """Restore an older revision's content as a new revision.

    Args:
        scope_article: The article being restored. *revision* must belong to it - callers scope the lookup rather than trusting a bare id.
        revision: The revision whose content to restore.
        editor: The profile performing the restore.

    Returns:
        Tuple of (article, revision) - revision is None when the article already held exactly that content.

    Raises:
        ValueError: *revision* does not belong to *scope_article*."""
    if revision.article_id != scope_article.pk:
        raise ValueError("That revision belongs to a different article.")

    return save_article(
        editor=editor,
        content=revision.content,
        edit_summary=f"Restored version from {revision.created:%b %d, %Y %H:%M}",
        pin=scope_article.pin,
        wiki=scope_article.wiki,
        restored_from=revision,
    )


def article_payload(article: Article, viewer: Profile) -> dict[str, Any]:
    """Render one article as the external API's article body.
    An article is host-agnostic - the same row shape backs a pin's private article and a community wiki's - so the payload is built here rather than in either host's view module.

    Args:
        article: The article to render.
        viewer: The requesting profile.

    Returns:
        A JSON-serializable dict with the article's source, rendered HTML, table of contents, word count, masked last editor, update timestamp, and the ``base_revision_id`` a client must echo back on save."""
    # Local import: ``services.wiki.wiki_detail`` imports this module for its own
    # article summary, so a module-level import here would close the cycle.
    from urbanlens.dashboard.services.wiki.wiki_detail import masked_editor_name

    return {
        "id": article.pk,
        # Raw Markdown source, for a client that wants to edit it.
        "content": article.content,
        # Server-rendered and sanitized, for a client that just wants to show it.
        "content_html": article.content_html,
        "toc": article.toc,
        "word_count": article.word_count(),
        "last_edited_by": masked_editor_name(article.last_edited_by, viewer),
        "updated": article.updated.isoformat(),
        # Send this back as ``base_revision_id`` to save without conflicting.
        "base_revision_id": latest_revision_id(article),
    }


# Revision diffs


@dataclass(slots=True)
class DiffRow:
    """One row of a rendered revision diff."""

    kind: str
    text: str


def diff_revisions(old_content: str, new_content: str, *, context: int = 3) -> list[DiffRow]:
    """Line diff between two revision bodies, with limited context.

    Args:
        old_content: The earlier revision's Markdown source.
        new_content: The later revision's Markdown source.
        context: Unchanged lines kept around each change hunk.

    Returns:
        Ordered diff rows; a ``kind="skip"`` row marks elided unchanged spans."""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[DiffRow] = []
    for group in matcher.get_grouped_opcodes(context):
        if rows:
            rows.append(DiffRow(kind="skip", text=""))
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == "equal":
                rows.extend(DiffRow(kind="context", text=line) for line in old_lines[old_start:old_end])
                continue
            if tag in {"replace", "delete"}:
                rows.extend(DiffRow(kind="del", text=line) for line in old_lines[old_start:old_end])
            if tag in {"replace", "insert"}:
                rows.extend(DiffRow(kind="add", text=line) for line in new_lines[new_start:new_end])
    return rows


class AnnotatedRevision(Protocol):
    """An ``ArticleRevision`` carrying the two lengths the history query adds.

    The row builder below is only ever handed rows from the queryset built
    beside it, which annotates both - but nothing in `ArticleRevision` itself
    promises them, and saying so here is more honest than asserting it on the
    model, where it would claim every instance has lengths that only a
    `revision_history_page` queryset supplies.
    """

    pk: int
    edit_summary: str
    editor_id: int | None
    editor: Profile | None
    restored_from_id: int | None
    created: datetime.datetime
    #: LENGTH(content) for this revision.
    content_length: int
    #: The same for the revision before it, or 0 for the first.
    previous_length: int


def revision_history_page(revisions: Any, viewer: Profile) -> tuple[Any, Callable[[AnnotatedRevision], dict[str, Any]]]:
    """A revision-history queryset and its row builder, for paginating in SQL.

    The two callers (pin article, wiki article) both used to materialise the
    whole history and build a row per revision before paginating, so the page
    size bounded the response body and nothing else. Each row needs
    ``size_delta``, which is ``len(content)`` - so every revision's complete
    source was read out of the database to produce one integer per row.

    ``size_delta`` is why this cannot be a plain slice: a row's delta is
    measured against the revision before it, which on the last row of a page is
    on the *next* page. A window function computes it against the true
    neighbour before ``LIMIT`` applies, so paging never changes a number.

    Args:
        revisions: The revision queryset, already scoped and filtered by the
            caller - the wiki door applies concealment, the pin door does not,
            and the window must see exactly the rows the viewer will page
            through for the deltas to add up.
        viewer: The requesting profile, for editor-name masking.

    Returns:
        ``(queryset, row_builder)`` to hand straight to ``paginated_response``.
    """
    from django.db.models import F, Window
    from django.db.models.functions import Lag, Length

    from urbanlens.dashboard.services.wiki.wiki_detail import masked_editor_name

    prepared = revisions.annotate(content_length=Length("content")).annotate(previous_length=Window(expression=Lag(Length("content"), default=0), order_by=[F("created").asc(), F("pk").asc()])).defer("content").order_by("-created", "-pk")
    # One entry per editor rather than per revision: `masked_editor_name` calls
    # `resolve_visible_identities`, which is written to take a batch, and a
    # history is usually a handful of people over many revisions.
    editor_names: dict[int, str | None] = {}

    def build(revision: AnnotatedRevision) -> dict[str, Any]:
        editor_id = revision.editor_id
        if editor_id is None:
            editor = None
        elif editor_id in editor_names:
            editor = editor_names[editor_id]
        else:
            editor = masked_editor_name(revision.editor, viewer)
            editor_names[editor_id] = editor
        return {
            "id": revision.pk,
            "edit_summary": revision.edit_summary,
            "editor": editor,
            "size_delta": revision.content_length - revision.previous_length,
            "restored_from": revision.restored_from_id,
            "created": revision.created.isoformat(),
        }

    return prepared, build
