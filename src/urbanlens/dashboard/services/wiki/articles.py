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
from typing import TYPE_CHECKING, Any

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
    editor: Profile | None,
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
        editor: The profile making the edit, or None for a system-initiated
            save (e.g. seeding a new wiki article from a matched Wikipedia
            article) - both ``Article.last_edited_by`` and
            ``ArticleRevision.editor`` are already nullable (SET_NULL) for
            exactly this case.
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


class ArticleConflictError(Exception):
    """Someone else saved the article while this editor was working on it.

    Attributes:
        current_revision_id: The id of the revision that is actually current,
            so a client can fetch it, show the other edit, and re-base.
    """

    def __init__(self, current_revision_id: int) -> None:
        """Record which revision is current."""
        super().__init__("This article changed while you were editing.")
        self.current_revision_id = current_revision_id


def latest_revision_id(article: Article | None) -> int | None:
    """Return the id of *article*'s newest revision, or None.

    Args:
        article: The article to inspect, or None when none exists yet.

    Returns:
        The newest ``ArticleRevision`` id, or None when the article is absent
        or has no revisions.
    """
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
    pin: Pin | None = None,
    wiki: Wiki | None = None,
) -> tuple[Article, ArticleRevision | None]:
    """Save an article, refusing the write if it would clobber a concurrent edit.

    Wraps :func:`save_article` with the optimistic-concurrency check the
    article editor has always performed, moved here so the internal view and
    the external API cannot drift on it.

    The rule, unchanged: if a revision exists and its id is not the one the
    editor started from, the save is refused. A ``base_revision_id`` of None
    therefore conflicts with *any* existing revision - which is what makes it
    safe for the API to require the field explicitly rather than letting an
    omitted value silently overwrite someone else's work.

    Args:
        editor: The profile making the edit (None for system saves).
        content: The complete new Markdown source.
        edit_summary: Optional one-line description of the change.
        base_revision_id: The revision the editor started from, or None when
            they believe the article has no revisions yet.
        pin: Host pin (mutually exclusive with ``wiki``).
        wiki: Host wiki.

    Returns:
        Tuple of (article, revision) - revision is None for a no-op save.

    Raises:
        ArticleConflictError: The article moved on since *base_revision_id*.
            Nothing is written when this is raised.
        ValueError: Neither or both hosts were provided.
    """
    article = get_article(pin=pin, wiki=wiki)
    latest_id = latest_revision_id(article)
    if latest_id is not None and latest_id != base_revision_id:
        raise ArticleConflictError(latest_id)

    return save_article(editor=editor, content=content, edit_summary=edit_summary, pin=pin, wiki=wiki)


def restore_revision(*, scope_article: Article, revision: ArticleRevision, editor: Profile | None) -> tuple[Article, ArticleRevision | None]:
    """Restore an older revision's content as a new revision.

    History is append-only: restoring does not delete anything, it writes the
    old content forward as the newest revision, tagged with ``restored_from``
    so the lineage stays visible in the history list.

    Args:
        scope_article: The article being restored. *revision* must belong to
            it - callers scope the lookup rather than trusting a bare id.
        revision: The revision whose content to restore.
        editor: The profile performing the restore.

    Returns:
        Tuple of (article, revision) - revision is None when the article
        already held exactly that content.

    Raises:
        ValueError: *revision* does not belong to *scope_article*.
    """
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

    An article is host-agnostic - the same row shape backs a pin's private
    article and a community wiki's - so the payload is built here rather than
    in either host's view module. Two endpoints in different files rendering
    "the same" dict by hand is how one of them ends up omitting
    ``base_revision_id`` (silently breaking that host's conflict detection,
    because a client with no revision to echo back always looks like a fresh
    save) or leaking an unmasked editor name that the other correctly masks.

    Args:
        article: The article to render.
        viewer: The requesting profile. Attribution is masked per the editor's
            identity-visibility settings as seen by this viewer, never emitted
            raw.

    Returns:
        A JSON-serializable dict with the article's source, rendered HTML,
        table of contents, word count, masked last editor, update timestamp,
        and the ``base_revision_id`` a client must echo back on save.
    """
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
