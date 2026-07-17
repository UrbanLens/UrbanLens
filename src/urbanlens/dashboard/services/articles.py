"""Article rendering and persistence.

Turns the Markdown source of a pin/wiki article into sanitized HTML plus a
table of contents, and owns the save path (render, cache, revision record).

Authoring format: Markdown ("gfm-like": tables, strikethrough, autolinked
URLs) plus footnote references (``[^1]`` in the text, ``[^1]: source`` at the
bottom) which render as a Wikipedia-style numbered References section.

Security: rendered HTML is always sanitized with nh3 against a fixed allowlist
before it is stored or returned - article HTML is community/user input and
must never reach a template unsanitized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import logging
import re
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
import nh3

if TYPE_CHECKING:
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Tags allowed to survive sanitization. Everything else is stripped (content
#: kept, tag removed) by nh3.
_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del", "div", "dl", "dt",
    "em", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol",
    "p", "pre", "q", "s", "section", "small", "span", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
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

    Headings are demoted one level (``#`` becomes ``<h2>``) so the article can
    never inject a second ``<h1>`` into the page, and each heading receives a
    stable ``id`` used by the table of contents. External links open in a new
    tab. Footnote definitions render as a numbered References section.

    Args:
        content: The raw Markdown source (may be empty).

    Returns:
        The sanitized HTML, TOC entries, and whether references are present.
    """
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
    return RenderedArticle(html=clean, toc=toc, has_references=has_references)


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


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
    editor: Profile,
    content: str,
    edit_summary: str = "",
    pin: Pin | None = None,
    wiki: Wiki | None = None,
    restored_from: ArticleRevision | None = None,
) -> tuple[Article, ArticleRevision | None]:
    """Persist a new version of a pin/wiki article.

    Renders and caches the sanitized HTML, updates the Article row (creating
    it on first save), and records an :class:`ArticleRevision` carrying the
    complete new source. Saving identical content is a no-op (no revision).

    Args:
        editor: The profile making the edit.
        content: The complete new Markdown source.
        edit_summary: Optional one-line description of the change.
        pin: Host pin (mutually exclusive with ``wiki``).
        wiki: Host wiki.
        restored_from: When this save restores an older revision, that revision.

    Returns:
        Tuple of (article, revision) - revision is None for a no-op save.

    Raises:
        ValueError: Neither or both hosts were provided.
    """
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision

    if (pin is None) == (wiki is None):
        raise ValueError("Exactly one of pin or wiki must be provided.")

    content = (content or "").replace("\r\n", "\n").rstrip()
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
    return article, revision


# ----------------------------------------------------------------------
# Revision diffs
# ----------------------------------------------------------------------


@dataclass(slots=True)
class DiffRow:
    """One row of a rendered revision diff.

    ``kind`` is "context", "add", or "del"; ``text`` is the line content.
    """

    kind: str
    text: str


def diff_revisions(old_content: str, new_content: str, *, context: int = 3) -> list[DiffRow]:
    """Line diff between two revision bodies, with limited context.

    Args:
        old_content: The earlier revision's Markdown source.
        new_content: The later revision's Markdown source.
        context: Unchanged lines kept around each change hunk.

    Returns:
        Ordered diff rows; a ``kind="skip"`` row marks elided unchanged spans.
    """
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
