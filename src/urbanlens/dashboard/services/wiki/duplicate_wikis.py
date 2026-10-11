"""Merging one community wiki into another when both turn out to describe the same building.
Two building places that were one real building each grew a wiki (REData P98's drifting Overture refs, see ``services.places.building_merge``). One page has to go, but nothing written on it may:

- Article: the kept page's article stays live; every revision of the other moves into its history, and the other's text, when it differs, is appended as a "Merged from <name>" section in one new revision attributed to that article's last human editor.
- Columns (name, description, security, dates, ...): the kept page keeps its own; one it lacks is taken from the other.
- Per-key rows - a Fact per key, a stat vote per (profile, field), a community boundary per type: the kept page's wins. The other's is snapshotted into the caller's ``PlaceMerge`` record, and a losing boundary outline also becomes a ``BoundaryRevision`` on the kept page, named by the merge's ``WikiEdit`` so it can be restored.
- Everything else (edits, comments, photos, aliases, links, child wikis, ...) moves; a row the kept page already has an identical twin of is snapshotted and dropped.

The absorbed page is deleted only once nothing references it, and the kept page records the merge as a ``WikiEdit`` with no editor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models.fields.files import FieldFile

from urbanlens.dashboard.services.core.row_merge import remaining_references, repoint_references, snapshot_row

if TYPE_CHECKING:
    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: The ``WikiEdit.changes`` key recording a merge, alongside ``child_wiki_merged`` (``services.wiki.wiki_merge``).
WIKI_MERGED_CHANGE = "wiki_merged"

#: Wiki columns that are the kept page's own identity or position; the absorbed page's go with it.
IDENTITY_COLUMNS = frozenset({"id", "created", "updated", "uuid", "slug", "location", "place", "parent_wiki"})

#: Columns that only mean something next to another one, so they move when it moves.
_COMPANIONS: dict[str, tuple[str, ...]] = {
    "pin_type": ("pin_type_is_user_provided",),
    "detail_bg_color": ("detail_bg_opacity",),
    "detail_border_color": ("detail_border_opacity",),
}
COMPANION_COLUMNS = frozenset(column for columns in _COMPANIONS.values() for column in columns)


class WikiMergeError(RuntimeError):
    """The absorbed wiki is still referenced after every move, so deleting it would destroy data."""


@dataclass(slots=True)
class WikiMergeResult:
    """What one wiki merge moved and dropped, for the ``PlaceMerge`` record.

    Attributes:
        moved: Counts and ids of what moved onto the kept page.
        dropped: Snapshots of what the kept page already had its own value for.
    """

    moved: dict[str, Any] = field(default_factory=dict)
    dropped: dict[str, Any] = field(default_factory=dict)

    def drop(self, key: str, snapshot: dict[str, Any]) -> None:
        """Record one dropped row under *key*."""
        self.dropped.setdefault(key, []).append(snapshot)


def gap_filled_columns() -> list[str]:
    """Every Wiki column a merge gap-fills: all of them but identity and companion columns.

    Returns:
        Column names.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    return [column.name for column in Wiki._meta.concrete_fields if column.name not in IDENTITY_COLUMNS | COMPANION_COLUMNS]  # noqa: SLF001


def _has_value(wiki: Wiki, name: str) -> bool:
    """Whether *wiki* holds a real value for column *name*, as opposed to an empty or default one."""
    column = wiki._meta.get_field(name)  # noqa: SLF001
    value = getattr(wiki, column.attname)
    if name == "pin_type" and wiki.pin_type_is_user_provided:
        return True
    if value is None or value == "":
        return False
    return not (column.has_default() and not callable(column.default) and value == column.default)


def _raw(wiki: Wiki, name: str) -> Any:
    """Column *name*'s stored value, JSON-safe enough for a snapshot."""
    value = getattr(wiki, wiki._meta.get_field(name).attname)  # noqa: SLF001
    return value.name if isinstance(value, FieldFile) else value


def _merge_columns(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> None:
    """Gap-fill the kept page's columns from the absorbed one; record every absorbed value it already had its own of."""
    changed: list[str] = []
    differing: dict[str, Any] = {}
    for name in gap_filled_columns():
        if not _has_value(absorbed, name):
            continue
        ours = _has_value(kept, name)
        # An editor's chosen type beats an automatically classified one, whichever page holds it.
        chosen = name == "pin_type" and absorbed.pin_type_is_user_provided and not kept.pin_type_is_user_provided
        if ours and _raw(kept, name) != _raw(absorbed, name):
            differing[name] = _raw(kept, name) if chosen else _raw(absorbed, name)
        if ours and not chosen:
            continue
        for column in (name, *_COMPANIONS.get(name, ())):
            attname = kept._meta.get_field(column).attname  # noqa: SLF001
            setattr(kept, attname, getattr(absorbed, attname))
            changed.append(column)
    if changed:
        kept.save(update_fields=[*changed, "updated"])
    if differing:
        result.dropped["columns"] = differing


def _concealed_base(article: Article, wiki: Wiki, editor: Profile | None) -> str:
    """The article text *editor* could see, which is all a revision attributed to them may carry.
    With concealment off (or for an editor it does not apply to) that is the live text.
    """
    from urbanlens.dashboard.services.wiki.concealment import concealment_active, visible_rows

    if editor is None or not concealment_active(wiki, editor):
        return article.content
    newest = visible_rows(article.revisions.all(), wiki, editor).order_by("-created", "-pk").first()
    return newest.content if newest is not None else ""


def _join_sections(text: str, section: str) -> str:
    return f"{text.rstrip()}\n\n{section}" if text.strip() else section


def _merge_article(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> None:
    """Keep the kept page's article live, adopt the absorbed article's history, and append its differing text."""
    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.wiki import article_merge

    absorbed_article = Article.objects.filter(wiki=absorbed).first()
    if absorbed_article is None:
        return
    kept_article = Article.objects.filter(wiki=kept).first()
    if kept_article is None:
        Article.objects.filter(pk=absorbed_article.pk).update(wiki=kept)
        result.moved["article"] = absorbed_article.pk
        return

    absorbed_text = absorbed_article.content.strip()
    differs = bool(absorbed_text) and absorbed_text not in kept_article.content
    editor_id = article_merge.latest_human_editor_id(absorbed_article if differs else kept_article)
    editor = Profile.objects.filter(pk=editor_id).first() if editor_id is not None else None
    # Read before the history moves: once it does, the absorbed article's own revisions are visible to their editor too.
    base = _concealed_base(kept_article, kept, editor)
    article_merge.record_live_text(kept_article)
    result.moved["article_revisions"] = article_merge.adopt_history(kept_article, absorbed_article)
    Article.objects.filter(pk=absorbed_article.pk).delete()

    if differs:
        section = f"## Merged from {absorbed.name}\n\n{absorbed_text}"
        article_merge.write_merge_revision(
            kept_article,
            content=_join_sections(kept_article.content, section),
            revision_content=_join_sections(base, section),
            editor=editor,
            edit_summary=f"Merged from {absorbed.name}",
        )
        return
    # Nothing to append, but the adopted revisions interleave by date: make the newest one the live text again.
    newest = kept_article.revisions.order_by("-created", "-pk").first()
    if newest is not None and newest.content != kept_article.content:
        article_merge.write_merge_revision(kept_article, content=kept_article.content, revision_content=base, editor=editor, edit_summary=f"Merged from {absorbed.name}")


def _merge_facts(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> None:
    """Move the absorbed page's Facts; where the kept page has one for the same key, it wins and the other is snapshotted with its evidence."""
    from urbanlens.dashboard.models.facts.model import Fact, FactEvidence

    taken = set(Fact.objects.filter(wiki=kept).values_list("key", flat=True))
    for fact in Fact.objects.filter(wiki=absorbed).order_by("pk"):
        if fact.key not in taken:
            Fact.objects.filter(pk=fact.pk).update(wiki=kept)
            taken.add(fact.key)
            continue
        result.drop("facts", snapshot_row(fact) | {"evidence": [snapshot_row(evidence) for evidence in FactEvidence.objects.filter(fact=fact).order_by("pk")]})
        fact.delete()


def _merge_stat_votes(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> None:
    """Move the absorbed page's stat votes; a profile's vote on the kept page for the same stat wins."""
    from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

    taken = set(WikiStatVote.objects.filter(wiki=kept).values_list("profile_id", "field"))
    for vote in WikiStatVote.objects.filter(wiki=absorbed).order_by("pk"):
        if (vote.profile_id, vote.field) not in taken:
            WikiStatVote.objects.filter(pk=vote.pk).update(wiki=kept)
            taken.add((vote.profile_id, vote.field))
            continue
        result.drop("stat_votes", snapshot_row(vote))
        vote.delete()


def _merge_boundaries(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> dict[str, dict[str, int | None]]:
    """Move the absorbed page's community boundaries; the kept page's wins per type and the other's outline becomes a revision.

    Returns:
        ``WikiEdit.changes`` entries naming each losing outline (``from``) and the kept one (``to``), so the merge edit
        keeps the losing revision from being pruned and reverting it restores that outline.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryRevision
    from urbanlens.dashboard.services.geo.wiki_boundary_edits import boundary_change_key, snapshot_outline

    result.moved["boundary_revisions"] = BoundaryRevision.objects.filter(wiki=absorbed).update(wiki=kept)
    changes: dict[str, dict[str, int | None]] = {}
    for boundary in Boundary.objects.filter(wiki=absorbed, pin__isnull=True).order_by("pk"):
        twin = Boundary.objects.filter(wiki=kept, pin__isnull=True, boundary_type=boundary.boundary_type).first()
        if twin is None:
            Boundary.objects.filter(pk=boundary.pk).update(wiki=kept, location=kept.location_id)
            continue
        lost = snapshot_outline(kept, boundary.boundary_type, boundary.drawn_or_generated_polygon)
        current = snapshot_outline(kept, boundary.boundary_type, twin.drawn_or_generated_polygon)
        if lost is not None:
            changes[boundary_change_key(boundary.boundary_type)] = {"from": lost.pk, "to": current.pk if current is not None else None}
        result.drop("boundaries", snapshot_row(boundary) | {"revision": lost.pk if lost is not None else None})
        boundary.delete()
    return changes


def _merge_one_to_ones(kept: Wiki, absorbed: Wiki, result: WikiMergeResult) -> None:
    """The trivia sweep's per-wiki bookkeeping: moved when the kept page has none, else snapshotted."""
    from urbanlens.dashboard.models.trivia.model import TriviaGenerationAttempt

    attempt = TriviaGenerationAttempt.objects.filter(wiki=absorbed).first()
    if attempt is None:
        return
    if TriviaGenerationAttempt.objects.filter(wiki=kept).exists():
        result.drop("trivia_generation_attempt", snapshot_row(attempt))
        attempt.delete()
    else:
        TriviaGenerationAttempt.objects.filter(pk=attempt.pk).update(wiki=kept)


def _merge_albums(kept: Wiki, absorbed: Wiki) -> None:
    """Move the absorbed page's albums, re-slugging on a ``uq_album_wiki_slug`` collision - both hold real photos."""
    from urbanlens.dashboard.models.album.model import Album

    taken = set(Album.objects.filter(parent_wiki=kept).values_list("slug", flat=True))
    for album in Album.objects.filter(parent_wiki=absorbed):
        album.parent_wiki = kept
        if album.slug in taken:
            album.slug = ""
        album.save()
        taken.add(album.slug)


def _repoint_reputation_targets(kept: Wiki, absorbed: Wiki) -> None:
    """Ledger rows name a wiki by id as well as by FK; point the id at the kept page where that stays unique."""
    from urbanlens.dashboard.models.reputation.meta import TargetKind
    from urbanlens.dashboard.models.reputation.model import ReputationEvent

    for pk in ReputationEvent.objects.filter(target_kind=TargetKind.WIKI, target_id=absorbed.pk).values_list("pk", flat=True):
        try:
            with transaction.atomic():
                ReputationEvent.objects.filter(pk=pk).update(target_id=kept.pk)
        except IntegrityError:
            # The same award already exists for the kept page; this row stays as the record it was.
            continue


def merge_duplicate_wiki(kept: Wiki, absorbed: Wiki) -> WikiMergeResult:
    """Fold *absorbed* into *kept* and delete it, once nothing references it.

    Args:
        kept: The page that stays - the survivor place's wiki.
        absorbed: The duplicate page.

    Returns:
        What moved and what was dropped, for the caller's ``PlaceMerge`` record.

    Raises:
        ValueError: The two are the same wiki.
        WikiMergeError: Something still references *absorbed* after every move; nothing is written.
    """
    from urbanlens.dashboard.models.aliases.model import WikiAlias
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

    if kept.pk == absorbed.pk:
        raise ValueError("Cannot merge a wiki into itself")

    result = WikiMergeResult()
    with transaction.atomic():
        list(WikiModel.objects.select_for_update().filter(pk__in=[kept.pk, absorbed.pk]).order_by("pk"))
        if kept.parent_wiki_id == absorbed.pk:
            # Re-nest above the absorbed page first, or its delete would CASCADE through the kept one.
            kept.parent_wiki_id = absorbed.parent_wiki_id
            kept.save(update_fields=["parent_wiki", "updated"])

        _merge_article(kept, absorbed, result)
        _merge_columns(kept, absorbed, result)
        # Provenance of the absorbed page's own columns: moving it would make its writes resolve as the kept page's
        # current values for a concealed viewer, so it is kept in the record instead.
        revisions = list(absorbed.field_revisions.all().order_by("pk"))
        if revisions:
            result.dropped["field_revisions"] = [snapshot_row(revision) for revision in revisions]
            absorbed.field_revisions.all().delete()
        _merge_facts(kept, absorbed, result)
        _merge_stat_votes(kept, absorbed, result)
        boundary_changes = _merge_boundaries(kept, absorbed, result)
        _merge_one_to_ones(kept, absorbed, result)
        _merge_albums(kept, absorbed)
        _repoint_reputation_targets(kept, absorbed)
        kept.labels.add(*absorbed.labels.all())

        result.moved["relations"] = {name: len(rows) for name, rows in remaining_references(absorbed).items()}
        repoint_references(absorbed, kept)
        # The absorbed page's name stays findable; usually its own alias has just moved and is reused.
        WikiAlias.objects.resolve_or_create(kept, absorbed.name)
        # What is left collided with an identical row the kept page already has (an alias, a link, a photo).
        for rows in remaining_references(absorbed).values():
            for row in rows:
                result.drop("duplicates", snapshot_row(row))
                row.delete()
        if leftover := remaining_references(absorbed):
            raise WikiMergeError(f"Wiki {absorbed.pk} is still referenced by {', '.join(sorted(leftover))}; not deleting it.")

        WikiEdit.objects.create(wiki=kept, editor=None, changes={WIKI_MERGED_CHANGE: {"from": absorbed.name, "to": kept.name}, **boundary_changes})
        result.moved["wiki_deleted"] = {"pk": absorbed.pk, "slug": absorbed.slug, "name": absorbed.name}
        absorbed.delete()
    return result
