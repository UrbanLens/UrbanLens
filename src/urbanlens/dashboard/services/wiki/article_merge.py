"""Folding one article's edit history into another's, so merging two hosts loses no text.
An :class:`Article` is one host's live text; every version it ever held lives in its :class:`ArticleRevision` rows.
Merging two pins (or two wikis) leaves room for only one live article, so the other's rows move across as history: each keeps its own editor, summary and timestamp, and stays viewable, diffable and restorable from the kept article's history page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
    from urbanlens.dashboard.models.profile.model import Profile


def record_live_text(article: Article) -> ArticleRevision | None:
    """Make sure the article's live text exists as a revision before its history moves.
    A save always writes one, but an article created directly (an import, a fixture) can hold text no revision carries - and that text would otherwise vanish with the row.

    Args:
        article: The article whose current text must survive.

    Returns:
        The revision written, or None when the newest revision already holds the live text (or there is no text).
    """
    from urbanlens.dashboard.models.article.model import ArticleRevision

    newest = article.revisions.order_by("-created", "-pk").first()
    if not article.content or (newest is not None and newest.content == article.content):
        return None
    revision = ArticleRevision.objects.create(article=article, editor_id=article.last_edited_by_id, content=article.content)
    # Dated to when the text was written rather than to the merge, so it sorts where it happened.
    ArticleRevision.objects.filter(pk=revision.pk).update(created=article.updated)
    return revision


def adopt_history(kept: Article, absorbed: Article) -> int:
    """Move every revision of *absorbed* onto *kept*, leaving *absorbed* with none.
    The rows move rather than being copied, so ``restored_from`` links between them stay valid and nothing is duplicated.

    Args:
        kept: The article that stays live.
        absorbed: The article whose history moves across. Its live text is recorded first.

    Returns:
        How many revisions moved.
    """
    from urbanlens.dashboard.models.article.model import ArticleRevision

    with transaction.atomic():
        record_live_text(absorbed)
        return ArticleRevision.objects.filter(article=absorbed).update(article=kept)


def live_text_editor_id(article: Article) -> int | None:
    """Who wrote the article's current text: the newest revision's editor.
    Deliberately not "the newest revision with an editor": when the newest one came from a deleted account (or a
    system seed), crediting an earlier person would attribute text they never wrote to them - and, under concealment,
    show it to their friends.

    Args:
        article: The article to inspect.

    Returns:
        A profile pk, or None when no living person wrote the current text. ``last_edited_by`` when there are no revisions.
    """
    newest = article.revisions.order_by("-created", "-pk").first()
    return newest.editor_id if newest is not None else article.last_edited_by_id


def write_merge_revision(article: Article, *, content: str, editor: Profile | None, edit_summary: str, revision_content: str | None = None) -> ArticleRevision:
    """Write *content* to *article* as a new newest revision, even when the text is unchanged.
    ``save_article`` skips a save that changes nothing, which is right for an editor but wrong after a merge: the adopted revisions interleave by date, so without a fresh newest row the history's top entry could be the other host's text while the page shows this one's.

    Args:
        article: The kept article.
        content: The text the article holds from now on.
        editor: Who the revision is attributed to.
        edit_summary: One line explaining the merge.
        revision_content: The text the revision records, when it must differ from the live text: a concealed editor's
            revision may only carry what that editor could see (see ``services.wiki.concealment``). Defaults to
            *content*.

    Returns:
        The new revision.
    """
    from urbanlens.dashboard.models.article.model import ArticleRevision
    from urbanlens.dashboard.services.wiki.articles import render_article

    with transaction.atomic():
        if article.content != content:
            rendered = render_article(content)
            article.content = content
            article.content_html = rendered.html
            article.toc = [{"level": entry.level, "title": entry.title, "anchor": entry.anchor} for entry in rendered.toc]
            article.last_edited_by = editor
            article.save()
        return ArticleRevision.objects.create(article=article, editor=editor, content=content if revision_content is None else revision_content, edit_summary=edit_summary[:255])
