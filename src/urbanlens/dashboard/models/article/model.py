"""Article models - long-form, Wikipedia-style write-ups for pins and wikis.

An :class:`Article` is the full free-form article body for exactly one host:

- ``wiki`` set: the community article for a shared place. Everyone who can see
  the wiki can read it, and anyone with access may edit it (every edit is
  recorded as an :class:`ArticleRevision`, so vandalism can be reverted).
- ``pin`` set: a user's private article about their own pin. Only the pin's
  owner can ever read or edit it.

Content is authored in Markdown (with footnote references) and rendered to
sanitized HTML by :mod:`urbanlens.dashboard.services.articles`. The rendered
HTML is cached on the row (``content_html``) so page views never re-render.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.validators import MaxLengthValidator
from django.db.models import CASCADE, SET_NULL, CharField, CheckConstraint, ForeignKey, Index, JSONField, OneToOneField, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.article.queryset import ArticleManager, ArticleRevisionManager
from urbanlens.dashboard.services.text_limits import MAX_ARTICLE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

#: edit_summary used by services.wiki_seed's system-initiated saves (editor
#: left None deliberately, not because an account was deleted) - the single
#: source of truth for both the writer (wiki_seed.py imports this rather than
#: redefining its own copy) and the reader (ArticleRevision.editor_display_name
#: below, which needs to tell the two null-editor cases apart).
EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA = "Seeded from Wikipedia"
SYSTEM_EDIT_SUMMARIES = frozenset({EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA})

logger = logging.getLogger(__name__)


class Article(abstract.DashboardModel):
    """The current state of one pin's or one wiki's article.

    Exactly one of ``pin`` or ``wiki`` is set (DB-enforced). The row holds the
    latest Markdown source plus its cached sanitized-HTML rendering; the full
    edit trail lives in :class:`ArticleRevision`.
    """

    # Markdown source of the current article text.
    content = TextField(blank=True, default="", max_length=MAX_ARTICLE_LENGTH, validators=[MaxLengthValidator(MAX_ARTICLE_LENGTH)])
    # Cached sanitized HTML rendering of ``content`` - regenerated on every
    # save through services.articles; never trusted from user input.
    content_html = TextField(blank=True, default="")
    # Cached table of contents: [{"level": 2, "title": ..., "anchor": ...}].
    toc = JSONField(default=list, blank=True)

    pin = OneToOneField(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="article",
    )
    wiki = OneToOneField(
        "dashboard.Wiki",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="article",
    )
    # Attribution only; deleting the editor's profile keeps the article.
    last_edited_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="articles_last_edited",
    )

    if TYPE_CHECKING:
        pin_id: int | None
        wiki_id: int | None
        last_edited_by_id: int | None

    objects = ArticleManager()

    @property
    def is_private(self) -> bool:
        """Whether this is a pin article (private to the pin's owner)."""
        return self.pin_id is not None

    @property
    def host_name(self) -> str:
        """Display name of the pin or wiki this article belongs to."""
        if self.pin is not None:
            return self.pin.effective_name or "Unnamed pin"
        if self.wiki is not None:
            return self.wiki.name or "Unnamed wiki"
        return "Article"

    def editable_by(self, profile: Profile) -> bool:
        """Whether ``profile`` may edit this article.

        Pin articles: only the pin's owner. Wiki articles: anyone who can see
        the wiki (a pin at the location, or being its creator) - callers are
        expected to have already resolved visibility via the standard wiki
        access gate, so this only re-checks the pin-privacy side.

        Args:
            profile: The profile attempting the edit.

        Returns:
            True when the edit is allowed.
        """
        if self.pin is not None:
            return self.pin.profile_id == profile.id
        return self.wiki is not None

    def word_count(self) -> int:
        """Approximate word count of the Markdown source."""
        return len(self.content.split())

    def __str__(self):
        host = "pin" if self.pin_id else "wiki"
        return f"Article({host}={self.pin_id or self.wiki_id})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_articles"
        get_latest_by = "updated"
        constraints = [
            CheckConstraint(
                condition=(Q(pin__isnull=False) & Q(wiki__isnull=True)) | (Q(pin__isnull=True) & Q(wiki__isnull=False)),
                name="article_exactly_one_host",
            ),
        ]


class ArticleRevision(abstract.DashboardModel):
    """One saved version of an article's Markdown source.

    A new revision is written on every successful save (including restores),
    each carrying the *complete* text at that moment - so any revision can be
    viewed, diffed against its predecessor, or restored wholesale without
    replaying a chain of diffs.
    """

    # Complete Markdown source as of this revision.
    content = TextField(blank=True, default="", max_length=MAX_ARTICLE_LENGTH, validators=[MaxLengthValidator(MAX_ARTICLE_LENGTH)])
    # Author-provided one-line description of what changed.
    edit_summary = CharField(max_length=255, blank=True, default="")

    article = ForeignKey(
        Article,
        on_delete=CASCADE,
        related_name="revisions",
    )
    editor = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="article_revisions",
    )
    # When this revision was produced by restoring an older one, the revision
    # that was restored - lets history label it "Restored version from ...".
    restored_from = ForeignKey(
        "self",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="restorations",
    )

    if TYPE_CHECKING:
        article_id: int
        editor_id: int | None
        restored_from_id: int | None

    objects = ArticleRevisionManager()

    @property
    def editor_display_name(self) -> str:
        """The name to show for who made this revision.

        ``editor`` is null both when the account that made it has since been
        deleted (``Profile``'s own ``on_delete=SET_NULL``) and when the
        revision was a system-initiated save that never had one to begin with
        (``services.wiki_seed`` passes ``editor=None`` deliberately when
        seeding a starting article from Wikipedia) - those need different
        labels, so this checks the edit summary against the known
        system-generated ones instead of assuming every null editor means a
        deleted account.

        Returns:
            The editor's username, "Wikipedia" for a system-seeded revision,
            or "Deleted user" for a genuinely deleted account.
        """
        if self.editor is not None:
            return self.editor.username
        if self.edit_summary in SYSTEM_EDIT_SUMMARIES:
            return "Wikipedia"
        return "Deleted user"

    def size_delta(self, previous: ArticleRevision | None) -> int:
        """Character-count change relative to ``previous`` (Wikipedia-style +N/-N).

        Args:
            previous: The revision immediately before this one, or None for
                the first revision.

        Returns:
            Signed character delta.
        """
        previous_len = len(previous.content) if previous is not None else 0
        return len(self.content) - previous_len

    def __str__(self):
        return f"ArticleRevision(article={self.article_id}, created={self.created:%Y-%m-%d %H:%M})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_article_revisions"
        ordering = ["-created"]
        get_latest_by = "created"
        indexes = [
            Index(fields=["article", "created"], name="idxdb_artrev_article_created"),
        ]
