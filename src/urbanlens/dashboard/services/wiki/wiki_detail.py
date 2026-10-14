"""Full wiki-detail payload for the external API's ``GET /wikis/{location_slug}/``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities
from urbanlens.dashboard.services.wiki.articles import latest_revision_id
from urbanlens.dashboard.services.wiki.wiki_aliases import alias_is_current_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.viewer_projection import ViewerProjection


def _isoformat_or_none(value: Any) -> str | None:
    """Return ``value.isoformat()``, or None when *value* is None."""
    return value.isoformat() if value is not None else None


def _boundary_geojson(wiki: Wiki) -> dict[str, Any] | None:
    """
    The wiki's effective property boundary as a GeoJSON geometry dict, if any.

    Args:
        wiki: The wiki whose boundary to resolve.

    Returns:
        A GeoJSON geometry dict, or None when the place has no boundary.
    """
    polygon = Boundary.objects.effective_polygon_for_wiki(wiki, BoundaryType.PROPERTY)
    return json.loads(polygon.geojson) if polygon is not None else None


def masked_editor_name(profile: Profile | None, viewer: Profile) -> str | None:
    """
    Resolve an editor's display name as *viewer* is permitted to see it.
    Wiki content is authored by many people a viewer may have no standing to see, so every attributed name on this surface goes through the same identity-visibility masking the comment thread uses.

    Args:
        profile: The attributed profile, or None for a system/deleted author.
        viewer: The requesting profile.

    Returns:
        The masked display name, or None when there is no attributed author."""
    if profile is None:
        return None
    identity = resolve_visible_identities(viewer, [profile]).get(profile.pk) or {}
    display_name = identity.get("display_name")
    return str(display_name) if display_name else str(profile)


def _article_summary(wiki: Wiki, profile: Profile) -> dict[str, Any] | None:
    """
    Summarize the wiki's article without shipping its full body.

    Args:
        wiki: The wiki whose article to summarize.
        profile: The requesting profile, for author masking.

    Returns:
        A summary dict, or None when this viewer has no article to see."""
    from urbanlens.dashboard.services.wiki.articles import get_article
    from urbanlens.dashboard.services.wiki.viewer_projection import is_projected, project_article, viewer_projection

    article = project_article(get_article(wiki=wiki), wiki, profile)
    if article is None:
        return None

    if is_projected(article):
        visible = viewer_projection(wiki, profile).newest_revision(article)
        return {
            "id": article.pk,
            "word_count": article.word_count(),
            "updated": _isoformat_or_none(visible.created) if visible else None,
            "last_edited_by": masked_editor_name(visible.editor if visible else None, profile),
            "base_revision_id": visible.pk if visible else None,
        }

    return {
        "id": article.pk,
        "word_count": article.word_count(),
        "updated": _isoformat_or_none(article.updated),
        "last_edited_by": masked_editor_name(article.last_edited_by, profile),
        "base_revision_id": latest_revision_id(article),
    }


def _stats(projection: ViewerProjection, profile: Profile) -> dict[str, dict[str, Any]]:
    """
    The four community stat composites plus the viewer's own votes.

    Args:
        projection: How the requesting profile reads the wiki whose stats to read.
        profile: The requesting profile, for ``my_vote``.

    Returns:
        ``{field: {rounded, exact, count, my_vote}}`` for every stat field."""
    stats: dict[str, dict[str, Any]] = {}
    for field in WikiStatField.values:
        composite = projection.stat_composite(field)
        stats[field] = {
            "rounded": composite.rounded,
            "exact": composite.exact,
            "count": composite.count,
            "my_vote": WikiStatVote.objects.my_vote(projection.wiki, field, profile),
        }
    return stats


def build_wiki_detail(wiki: Wiki, location: Location, profile: Profile) -> dict[str, Any]:
    """
    Assemble the full detail payload for one wiki.

    Args:
        wiki: The wiki to serialize.
        location: The wiki's Location, already resolved by that same call.
        profile: The requesting profile, needed for own-vote lookup, author masking, and how they read the wiki.

    Returns:
        A JSON-serializable dict covering identity, description, dates, security, coordinates, boundary, aliases, links, community stats and counts, the article summary, and the comment count."""
    from urbanlens.dashboard.services.wiki.viewer_projection import project_wiki, viewer_projection
    from urbanlens.dashboard.services.wiki.wiki_edits import wiki_revision_marker

    projection = viewer_projection(wiki, profile)
    # Field values come from `shown`; row sets are filtered separately. Reading
    # the raw row here is what let this payload drift from the HTML page, which
    # the comment in controllers/location_wiki.py claimed could not happen.
    shown = project_wiki(wiki, profile)

    latitude = wiki.latitude
    longitude = wiki.longitude
    cover_photo = wiki.cover_photo if projection.shows_cover_photo() else None

    aliases = projection.rows(wiki.aliases.all())
    links = projection.rows(wiki.links.order_by("order", "pk"))
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.services.comments.comments import visible_comment_count

    comments = projection.rows(Comment.objects.filter(wiki=wiki))

    payload: dict[str, Any] = {
        # The navigable identifier: wiki routes resolve a Location, so this is
        # what a client round-trips. wiki_slug is informational only.
        "location_slug": location.ensure_slug(),
        "wiki_slug": wiki.slug or None,
        "uuid": str(wiki.uuid),
        # Sent back as a PATCH's base_revision_id, so an edit over a newer write is refused rather than applied.
        "revision": wiki_revision_marker(wiki),
        "name": shown.name,
        "description": shown.description or None,
        "pin_type": shown.pin_type,
        "indoor_outdoor": shown.indoor_outdoor or None,
        "date_abandoned": _isoformat_or_none(shown.date_abandoned),
        "date_last_active": _isoformat_or_none(shown.date_last_active),
        "security": {field: getattr(shown, field) for field, _label in SECURITY_FIELDS},
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
        "address": wiki.address or None,
        "cover_photo_url": cover_photo.file_url if cover_photo is not None else None,
        "boundary": _boundary_geojson(wiki),
        # is_current comes from the shared helper rather than a local comparison
        # so this payload cannot drift from WikiAliasSerializer, which documents
        # the field, or from the delete guard, which decides what the rule means.
        "aliases": [{"id": alias.pk, "name": alias.name, "kind": alias.kind, "source": alias.source, "is_current": alias_is_current_name(alias, wiki)} for alias in aliases],
        "links": [{"id": link.pk, "name": link.name, "url": link.url, "wayback_url": link.wayback_url or None, "auto_source": link.auto_source or None, "order": link.order} for link in links],
        "stats": _stats(projection, profile),
        "article": _article_summary(wiki, profile),
        # Gated, so the number cannot disagree with the thread the viewer is
        # shown - see services.comments.comments.visible_comment_count.
        "comment_count": visible_comment_count(comments, profile),
        "created": _isoformat_or_none(wiki.created),
        "updated": _isoformat_or_none(wiki.updated),
    }

    # pin_count_low / pin_count_approx / first_pinned / first_pinned_precision.
    community = projection.community_summary(location)
    payload.update(community)
    payload["first_pinned"] = _isoformat_or_none(community["first_pinned"])

    return payload
