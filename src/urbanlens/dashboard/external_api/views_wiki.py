"""External-facing wiki endpoints: community pages, articles, comments, reviews.

**The invariant this whole module is built around.**

Every wiki-scoped handler's first statement is::

    location, wiki, profile = resolve_visible_wiki(request, location_slug)

That call is the only way a Wiki is ever loaded here - never
``Wiki.objects.get()``, never a permission check that returns something other
than a 404. It raises a bare ``Http404`` for all three of:

- a ``location_slug`` that matches nothing,
- a real Location that has no Wiki, and
- a real Wiki the caller has not pinned.

Combined with :mod:`.errors`, which renders every ``Http404`` as the constant
body ``{"error": "Not found."}``, those three cases are indistinguishable down
to the byte. That is the point: if a caller could tell "no such place" from
"you can't see this place", the slug becomes an oracle for enumerating which
locations other users have pinned, which is precisely the inference this app
exists to prevent. A 403 here would leak; a friendlier message here would leak.

The same reasoning extends to sub-resources. An edit id, alias id, link id,
comment id or revision id is **always** looked up scoped to the
already-resolved wiki (``get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)``).
A bare lookup by id would make those integers globally enumerable across every
wiki in the database - a smaller leak than the slug oracle, but the same kind.

Scopes: everything wiki-shaped uses ``wiki:read``/``wiki:write``. The
pin-scoped comment and review endpoints use ``pins:read``/``pins:write``
instead, because they are private per-pin data on the caller's own pin rather
than shared community content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.gis.geos import GEOSException
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.response import Response

from urbanlens.dashboard.external_api.mixins import _ReactionMixin
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_wiki import (
    ArticleDetailSerializer,
    ArticleRevisionDetailSerializer,
    ArticleRevisionSerializer,
    ArticleSaveSerializer,
    CommentCreateSerializer,
    CommentSerializer,
    GalleryImageSerializer,
    ReviewSerializer,
    WikiAliasCreateSerializer,
    WikiAliasSerializer,
    WikiBoundaryPayloadSerializer,
    WikiBoundaryUpdateSerializer,
    WikiCoverPhotoResponseSerializer,
    WikiCoverPhotoUpdateSerializer,
    WikiDetailSerializer,
    WikiEditSerializer,
    WikiLinkCreateSerializer,
    WikiLinkSerializer,
    WikiOwnerSerializer,
    WikiPropertySaleSerializer,
    WikiStatSerializer,
    WikiStatVoteInputSerializer,
    WikiUpdateSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView, OwnedPinMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.boundary.queryset import DEFAULT_RADIUS_METERS
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.comments.comments import (
    ALLOWED_EMOJIS,
    CommentValidationError,
    aggregate_reactions,
    comment_is_visible,
    comment_mentions,
    create_comment,
    toggle_reaction,
    top_level_comment_queryset,
    visible_comment_tree,
)
from urbanlens.dashboard.services.geo.geo import InvalidPolygonGeoJSONError, geometry_to_geojson, parse_multipolygon_geojson
from urbanlens.dashboard.services.locations.boundaries import boundary_generation_ran, schedule_location_boundary_generation
from urbanlens.dashboard.services.pins.reviews import clear_review, upsert_review
from urbanlens.dashboard.services.wiki.articles import (
    ArticleConflictError,
    diff_revisions,
    get_article,
    latest_revision_id,
    restore_revision,
    save_article_checked,
)
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki
from urbanlens.dashboard.services.wiki.wiki_aliases import promote_wiki_alias_to_name
from urbanlens.dashboard.services.wiki.wiki_detail import build_wiki_detail, masked_editor_name
from urbanlens.dashboard.services.wiki.wiki_edits import WikiEditValidationError, apply_wiki_edit, revert_wiki_edit

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.comments.model import Comment as CommentModel
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


class WikiApiView(ExternalApiView):
    """Base for every wiki endpoint: uniform errors plus the visibility gate.

    The ``{"error": ...}`` envelope is inherited, not opted into: since
    ``ExternalApiView`` itself mixes in ``ErrorEnvelopeMixin`` ahead of
    ``APIView``, naming the mixin again here would only restate the MRO the base
    already guarantees - and a second name for one behaviour is how the two
    drift apart later.
    """

    def resolve(self, request: Request, location_slug: str) -> tuple[Any, Any, Profile]:
        """Resolve the Location/Wiki/Profile for *location_slug*, or 404.

        Every handler in this module calls this as its first statement. See the
        module docstring for why nothing else may load a Wiki.

        Args:
            request: The authenticated request.
            location_slug: The Location slug or uuid from the URL.

        Returns:
            Tuple of (Location, Wiki, requesting Profile).

        Raises:
            Http404: Indistinguishably, for "no such location", "no wiki
                here", and "you haven't pinned this place".
        """
        return resolve_visible_wiki(request, location_slug)


def _own_pin_or_404(view: OwnedPinMixin, request: Request, pin_slug: str) -> Pin:
    """Resolve one of the caller's own pins, or raise the uniform 404.

    Delegates to ``OwnedPinMixin.get_owned_pin`` so pin lookup (slug-or-uuid,
    ownership filter, select_related set) stays defined in exactly one place.

    Args:
        view: The calling view instance.
        request: The authenticated request.
        pin_slug: The pin slug or uuid from the URL.

    Returns:
        The caller's matching pin.

    Raises:
        Http404: No such pin, or it belongs to someone else - deliberately the
            same response for both.
    """
    pin = view.get_owned_pin(request, pin_slug)
    if pin is None:
        raise Http404
    return pin


def _serialize_comment(item: Any, profile: Profile) -> dict[str, Any]:
    """Render one gated comment (and its replies) for the API.

    Args:
        item: A ``services.comments.comments.VisibleComment`` that has already passed
            every visibility gate.
        profile: The requesting profile.

    Returns:
        A JSON-serializable comment dict, one level of replies deep.
    """
    comment = item.comment
    return {
        "id": comment.pk,
        # Raw storage format plus resolved mentions rather than HTML: by the
        # time a comment reaches here it has passed the mention gate, so the
        # viewer has provably pinned every location it names.
        "text": comment.text,
        "mentions": comment_mentions(comment.text),
        "author": masked_editor_name(comment.profile, profile),
        "author_is_self": comment.profile_id == profile.pk,
        "image_url": comment.image.url if comment.image else None,
        "has_map": comment.markup_map_id is not None and not comment.map_removed,
        "reactions": aggregate_reactions(comment, profile),
        "parent_was_deleted": item.parent_was_deleted,
        "created": comment.created.isoformat(),
        "replies": [_serialize_comment(reply, profile) for reply in item.replies],
    }


class WikiDetailApiView(WikiApiView):
    """GET the community wiki for a place; PATCH its editable fields.

    PATCH applies a community edit through the same
    ``services.wiki.wiki_edits.apply_wiki_edit`` the dashboard's "Suggest edits"
    form uses, and records the identical ``WikiEdit`` audit row - but with
    ``strict=True``, so an unrecognized security level or an unparseable date
    is a 400 rather than the internal view's silent skip.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "PATCH": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: WikiDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return the full detail payload for one wiki."""
        location, wiki, profile = self.resolve(request, location_slug)
        return Response(build_wiki_detail(wiki, location, profile))

    @extend_schema(request=WikiUpdateSerializer, responses={200: WikiDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, location_slug: str) -> Response:
        """Apply a community edit to one wiki's fields."""
        location, wiki, profile = self.resolve(request, location_slug)

        serializer = WikiUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Flatten the nested security object into the {field: value} mapping
        # apply_wiki_edit consumes. The nesting is an external-API shape
        # decision (matching the pin detail read shape); the service and the
        # audit trail stay flat, as the internal view already recorded them.
        changes: dict[str, Any] = {key: value for key, value in data.items() if key != "security"}
        changes.update(data.get("security") or {})
        # Dates arrive as date objects from DateField; the service writes them
        # through unchanged and stringifies for the audit record.

        try:
            with transaction.atomic():
                apply_wiki_edit(wiki, profile, changes, strict=True)
        except WikiEditValidationError as exc:
            return Response({"error": exc.message, "fields": {exc.field: exc.message} if exc.field else {}}, status=400)

        return Response(build_wiki_detail(wiki, location, profile))


class WikiHistoryView(PaginatedListMixin, WikiApiView):
    """GET the wiki's edit history, newest first."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(responses={200: WikiEditSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of the wiki's edit history."""
        _location, wiki, profile = self.resolve(request, location_slug)
        edits = wiki.edits.select_related("editor__user").order_by("-created", "-pk")
        rows = [
            {
                "id": edit.pk,
                "changes": edit.changes,
                "reverted": edit.reverted,
                "editor": masked_editor_name(edit.editor, profile),
                "created": edit.created.isoformat(),
            }
            for edit in edits
        ]
        return self.paginated_response(rows, WikiEditSerializer, request)


class WikiRevertView(WikiApiView):
    """POST to undo one edit from the wiki's history."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: WikiDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str, edit_id: int) -> Response:
        """Revert one edit, recording the reversal as a new edit."""
        location, wiki, profile = self.resolve(request, location_slug)
        # Scoped to this wiki: a bare id lookup would make WikiEdit ids
        # enumerable across every wiki in the database.
        target_edit = get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)

        if target_edit.reverted:
            return Response({"error": "This edit has already been reverted."}, status=400)

        with transaction.atomic():
            revert_edit, skipped_fields = revert_wiki_edit(location, wiki, profile, target_edit)

        if revert_edit is None:
            return Response(
                {"error": "Could not revert - every field this edit touched has been changed again since.", "skipped_fields": skipped_fields},
                status=409,
            )

        payload = build_wiki_detail(wiki, location, profile)
        payload["skipped_fields"] = skipped_fields
        return Response(payload)


class WikiStatVoteApiView(WikiApiView):
    """GET the caller's own community stat vote; PUT to cast it, DELETE to withdraw."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "PUT": frozenset({ApiKeyScope.WIKI_WRITE}),
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    def _check_field(self, field: str) -> None:
        """Reject an unknown stat field with the uniform 404.

        Args:
            field: The stat field name from the URL.

        Raises:
            Http404: *field* is not one of the four stat fields.
        """
        if field not in WikiStatField.values:
            raise Http404

    def _payload(self, wiki: Any, field: str, profile: Profile) -> dict[str, Any]:
        """Build the composite/own-vote response body for one stat field."""
        composite = WikiStatVote.objects.composite(wiki, field)
        return {
            "rounded": composite.rounded,
            "exact": composite.exact,
            "count": composite.count,
            "my_vote": WikiStatVote.objects.my_vote(wiki, field, profile),
        }

    @extend_schema(responses={200: WikiStatSerializer, 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str, field: str) -> Response:
        """Return the composite and the caller's own vote for one stat field."""
        _location, wiki, profile = self.resolve(request, location_slug)
        self._check_field(field)
        return Response(self._payload(wiki, field, profile))

    @extend_schema(request=WikiStatVoteInputSerializer, responses={200: WikiStatSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, location_slug: str, field: str) -> Response:
        """Cast or replace the caller's vote on one stat field."""
        _location, wiki, profile = self.resolve(request, location_slug)
        self._check_field(field)

        serializer = WikiStatVoteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        WikiStatVote.objects.cast(wiki, profile, field, serializer.validated_data["value"])
        return Response(self._payload(wiki, field, profile))

    @extend_schema(responses={200: WikiStatSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str, field: str) -> Response:
        """Withdraw the caller's vote on one stat field."""
        _location, wiki, profile = self.resolve(request, location_slug)
        self._check_field(field)

        WikiStatVote.objects.clear(wiki, profile, field)
        return Response(self._payload(wiki, field, profile))


class WikiAliasesView(WikiApiView):
    """GET the wiki's alternate names; POST to add one.

    The list includes the wiki's current name - the alias list is defined as
    every name the place is known by - so each row carries ``is_current`` to say
    which one that is. The wiki is handed to the serializer as context so
    computing that flag costs no per-row query.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: WikiAliasSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """List the wiki's aliases."""
        _location, wiki, _profile = self.resolve(request, location_slug)
        return Response(WikiAliasSerializer(wiki.aliases.order_by("pk"), many=True, context={"wiki": wiki}).data)

    @extend_schema(request=WikiAliasCreateSerializer, responses={201: WikiAliasSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str) -> Response:
        """Add an alias to the wiki."""
        _location, wiki, profile = self.resolve(request, location_slug)

        serializer = WikiAliasCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        alias = WikiAlias(wiki=wiki, name=data["name"], created_by=profile)
        if data.get("kind"):
            alias.kind = data["kind"]
        alias.save()
        # Serialized after save(), like the promote endpoint below and for the
        # same reason: _AliasBase.save() sanitizes ``name``, so the row the
        # client is told about must be read back rather than echoed.
        return Response(WikiAliasSerializer(alias, context={"wiki": wiki}).data, status=201)


class WikiAliasUseView(WikiApiView):
    """POST: make one of the wiki's aliases its current community name.

    The wiki counterpart of ``PinAliasUseView``, and shaped identically:
    no request body, and the full wiki detail in the response rather than the
    alias, so a client applies the rename (and everything derived from it -
    the alias list's ``is_current`` flags, the edit history's new entry) from
    the same payload ``GET wikis/{location_slug}/`` already hands it.

    Unlike the pin version this is a *community* edit, so it goes through
    ``services.wiki.wiki_aliases.promote_wiki_alias_to_name`` and lands in the wiki's
    edit history alongside every other wiki edit - a rename nobody can see or
    revert is the failure mode that matters on shared content.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(request=None, responses={200: WikiDetailSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str, alias_id: int) -> Response:
        """Promote one alias to the wiki's name and return the updated wiki.

        Promoting the alias that is already the name answers 200 with the
        unchanged wiki, not 400: "use this name" is an idempotent statement of
        intent, and a client retrying a request whose response it never saw
        would otherwise get an error for successfully doing nothing.

        Args:
            request: The authenticated request. No body is read.
            location_slug: The Location slug or uuid from the URL.
            alias_id: The alias to promote, scoped to this wiki.

        Returns:
            The full wiki detail payload, built after the save.

        Raises:
            Http404: The wiki is not visible to this caller, or *alias_id* is
                not an alias of it.
        """
        location, wiki, profile = self.resolve(request, location_slug)
        # Scoped to this wiki: a bare id lookup would turn alias ids into an
        # oracle for the names on wikis this caller cannot see - the same leak
        # the module docstring describes for edit and comment ids, and worse
        # here because an alias *is* content rather than just a handle.
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)

        with transaction.atomic():
            promote_wiki_alias_to_name(wiki, profile, alias)

        # Built strictly after the save. Wiki.save() sanitizes ``name`` to a
        # restricted character set, so a payload assembled from ``alias.name``
        # beforehand can report a name the database does not actually hold -
        # and the client would then cache and display a value that disagrees
        # with every subsequent read.
        return Response(build_wiki_detail(wiki, location, profile))


class WikiAliasDetailView(WikiApiView):
    """DELETE one of the wiki's aliases."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str, alias_id: int) -> Response:
        """Remove one alias, scoped to this wiki."""
        _location, wiki, _profile = self.resolve(request, location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        alias.delete()
        return Response(status=204)


class WikiAliasToggleNicknameView(WikiApiView):
    """POST: flip one of the wiki's aliases between nickname-only and a plain alternate name.

    Nickname-only aliases are excluded from external name-provider queries but
    still shown in the wiki's own alias list - toggling this is a display
    preference on shared data, not a rename, so unlike :class:`WikiAliasUseView`
    it is not recorded in the wiki's edit history, matching the internal
    ``LocationAliasToggleNicknameView`` it wraps.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(request=None, responses={200: WikiAliasSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str, alias_id: int) -> Response:
        """Flip one alias's nickname flag and return it.

        Args:
            request: The authenticated request. No body is read.
            location_slug: The Location slug or uuid from the URL.
            alias_id: The alias to toggle, scoped to this wiki.

        Returns:
            The updated alias.

        Raises:
            Http404: The wiki is not visible to this caller, or *alias_id* is
                not an alias of it.
        """
        _location, wiki, _profile = self.resolve(request, location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        alias.toggle_nickname()
        return Response(WikiAliasSerializer(alias, context={"wiki": wiki}).data)


class WikiBoundaryApiView(WikiApiView):
    """GET the wiki page map's typed boundaries; POST to save or clear the community drawing of one.

    Thin wrapper over ``controllers.boundary.WikiBoundaryView`` - same
    resolution chain, same polling contract (``pending``/``refreshing``), and
    the same rule that a community drawing lives on a wiki-keyed ``Boundary``
    row, never on the shared location-default row that API-generated geometry
    occupies, so a community edit can never influence point-to-location
    matching.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    def _payload(self, wiki: Any, *, pending: bool, refreshing: bool) -> dict[str, Any]:
        """Build the boundary payload, mirroring ``controllers.boundary._wiki_boundary_payload``."""
        boundaries = {}
        for boundary_type in (BoundaryType.PROPERTY, BoundaryType.BUILDING):
            polygon, source = Boundary.objects.resolve_for_wiki(wiki, boundary_type)
            boundaries[str(boundary_type.value)] = {"polygon": geometry_to_geojson(polygon), "source": source}
        location = wiki.location
        return {
            "latitude": float(location.latitude) if location and location.latitude is not None else None,
            "longitude": float(location.longitude) if location and location.longitude is not None else None,
            "default_radius_meters": DEFAULT_RADIUS_METERS,
            "pending": pending,
            "refreshing": refreshing,
            "boundaries": boundaries,
        }

    @extend_schema(responses={200: WikiBoundaryPayloadSerializer, 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return the wiki's effective boundaries, scheduling generation/refresh when needed."""
        location, wiki, profile = self.resolve(request, location_slug)
        already_ran = boundary_generation_ran(location)
        in_flight = schedule_location_boundary_generation(location, profile)
        return Response(self._payload(wiki, pending=in_flight and not already_ran, refreshing=in_flight and already_ran))

    @extend_schema(request=WikiBoundaryUpdateSerializer, responses={200: WikiBoundaryPayloadSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str) -> Response:
        """Save or clear the community-drawn boundary of one type, with audit."""
        location, wiki, profile = self.resolve(request, location_slug)

        serializer = WikiBoundaryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        boundary_type = data["boundary_type"]

        row = Boundary.objects.row_for_wiki(wiki, boundary_type)
        old_wkt = row.polygon.wkt if row and row.polygon else None

        polygon_geojson = data.get("polygon")
        if polygon_geojson:
            try:
                geom = parse_multipolygon_geojson(polygon_geojson)
            except InvalidPolygonGeoJSONError as exc:
                return Response({"error": exc.safe_message}, status=400)

            from urbanlens.dashboard.models.site_settings import SiteSettings

            max_km2 = SiteSettings.get_current().max_bbox_area_km2
            try:
                area_km2 = geom.transform(6933, clone=True).area / 1_000_000
            except GEOSException:
                area_km2 = 0.0
            if area_km2 > max_km2:
                return Response({"error": f"Boundary is too large ({area_km2:,.0f} km²). Maximum allowed area is {max_km2:,.0f} km²."}, status=400)

            if row is None:
                row = Boundary(wiki=wiki, location=location, boundary_type=boundary_type)
            row.polygon = geom
            if row.location_id != wiki.location_id:
                row.location = wiki.location
            row.save()
            new_wkt = geom.wkt
        else:
            if row is not None:
                row.delete()
            new_wkt = None

        WikiEdit.objects.create(wiki=wiki, editor=profile, changes={f"boundary_{boundary_type}": {"from": old_wkt, "to": new_wkt}})

        already_ran = boundary_generation_ran(location)
        in_flight = schedule_location_boundary_generation(location, profile)
        return Response(self._payload(wiki, pending=in_flight and not already_ran, refreshing=in_flight and already_ran))


class WikiCoverPhotoApiView(WikiApiView):
    """PUT to set the wiki's hero-banner cover photo; DELETE to clear it.

    Wraps ``controllers.image_gallery.WikiCoverPhotoView``. Any profile with a
    pin at the wiki's location may set it - the wiki is community content
    editable by everyone who can see it, the same rule comments, markup and
    aliases already share. The photo must already be in the wiki's own
    gallery (resolved by ``image_uuid``, matching the addressing every other
    photo route in this module uses) rather than accepted as an ad-hoc upload.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.WIKI_WRITE}),
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(request=WikiCoverPhotoUpdateSerializer, responses={200: WikiCoverPhotoResponseSerializer, 404: ErrorSerializer})
    def put(self, request: Request, location_slug: str) -> Response:
        """Set the wiki's cover photo to one already in its gallery."""
        location, wiki, profile = self.resolve(request, location_slug)

        serializer = WikiCoverPhotoUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # uuid filter first - `visible_to` eagerly resolves the uploader set of
        # the queryset it is given, so the unfiltered manager would cost a
        # site-wide uploader walk to answer about one image.
        image = get_object_or_404(Image.objects.filter(uuid=serializer.validated_data["image_uuid"]).visible_to(profile))
        if image.wiki_id != wiki.pk and image.location_id != location.pk:
            raise Http404
        wiki.cover_photo = image
        wiki.save(update_fields=["cover_photo", "updated"])
        return Response({"cover_photo_url": image.image.url if image.image else image.source_url})

    @extend_schema(responses={200: WikiCoverPhotoResponseSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str) -> Response:
        """Clear the wiki's cover photo."""
        _location, wiki, _profile = self.resolve(request, location_slug)
        wiki.cover_photo = None
        wiki.save(update_fields=["cover_photo", "updated"])
        return Response({"cover_photo_url": None})


class WikiOwnershipView(PaginatedListMixin, WikiApiView):
    """GET the wiki's shared Ownership card: current and past owners of this place.

    Read-only this pass - see ``docs/notes/mobile_app_notes.md`` Part 7 for why
    the write side (adding/editing/unlinking an owner) is deferred rather than
    built here.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(responses={200: WikiOwnerSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of the location's shared owners."""
        location, _wiki, _profile = self.resolve(request, location_slug)
        return self.paginated_response(WikiOwner.objects.for_location(location), WikiOwnerSerializer, request)


class WikiPropertySalesView(PaginatedListMixin, WikiApiView):
    """GET the wiki's shared Sale History tab, newest first.

    Read-only this pass, for the same reason as :class:`WikiOwnershipView`.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(responses={200: WikiPropertySaleSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of the location's shared sale records."""
        location, _wiki, _profile = self.resolve(request, location_slug)
        sales = WikiPropertySale.objects.for_location(location).prefetch_related("previous_owners", "new_owners")
        return self.paginated_response(sales, WikiPropertySaleSerializer, request)


class WikiLinksView(WikiApiView):
    """GET the wiki's external links; POST to add one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: WikiLinkSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """List the wiki's links in display order."""
        _location, wiki, _profile = self.resolve(request, location_slug)
        return Response(WikiLinkSerializer(wiki.links.order_by("order", "pk"), many=True).data)

    @extend_schema(request=WikiLinkCreateSerializer, responses={201: WikiLinkSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str) -> Response:
        """Add an external link to the wiki."""
        _location, wiki, profile = self.resolve(request, location_slug)

        serializer = WikiLinkCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            # Wikis are edited concurrently by many people, so a duplicate url is
            # an ordinary client outcome, not a server fault - the unique
            # constraint decides and this reports 400 rather than 500.
            with transaction.atomic():
                link = WikiLink.objects.create(wiki=wiki, name=data.get("name", ""), url=data["url"], created_by=profile)
        except IntegrityError:
            return Response({"detail": "That link is already on this page."}, status=400)
        return Response(WikiLinkSerializer(link).data, status=201)


class WikiLinkDetailView(WikiApiView):
    """DELETE one of the wiki's links."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str, link_id: int) -> Response:
        """Remove one link, scoped to this wiki."""
        _location, wiki, _profile = self.resolve(request, location_slug)
        link = get_object_or_404(WikiLink, id=link_id, wiki=wiki)
        link.delete()
        return Response(status=204)


def _gallery_row(image: Image) -> dict[str, Any]:
    """Shape one gallery image into its API payload.

    A module-level function rather than an inline comprehension so it can be
    handed to ``paginated_response`` as a ``row_builder`` and therefore run on
    the requested page only - see that method's docstring.

    Args:
        image: A wiki gallery image with a non-empty file.

    Returns:
        The serializable row for :class:`GalleryImageSerializer`.
    """
    return {
        "id": image.pk,
        "uuid": image.uuid,
        "url": image.image.url,
        "caption": image.caption,
        "author": image.author,
        "source_url": image.source_url,
        "copyright": image.copyright,
        "created": image.created.isoformat(),
    }


class WikiGalleryView(PaginatedListMixin, WikiApiView):
    """GET the wiki's shared photo gallery.

    Read-only this pass: accepting uploads needs the async malware-scan
    handshake the comment image path uses, which is out of scope here.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(responses={200: GalleryImageSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of gallery images the caller may see."""
        _location, wiki, profile = self.resolve(request, location_slug)
        # visible_to applies the uploader's photo-visibility setting and the
        # viewer's own photo filter - a wiki being visible does not make every
        # photo on it visible.
        #
        # `exclude(image="")` is pushed into the queryset rather than filtered
        # out in Python so the page the paginator slices is the page that gets
        # returned; a Python-side `if image.image` would make pages short and
        # the count wrong.
        # Most-likely-relevant first (REData's cached confidence - see
        # services.photos.redata_relevance), falling back to upload order for
        # a photo REData hasn't scored yet.
        images = Image.objects.filter(wiki=wiki).visible_to(profile).exclude(image="").order_by(F("redata_confidence").desc(nulls_last=True), "-created", "-pk")
        # The queryset is paginated, then rows are built for the page only.
        # Building rows first materialized the *entire* visible gallery and
        # resolved a storage URL per image on every request before the
        # paginator sliced the resulting list - so the page-size limit bounded
        # only the response body, while the work behind it grew with the whole
        # gallery.
        return self.paginated_response(images, GalleryImageSerializer, request, row_builder=_gallery_row)


class WikiArticleView(WikiApiView):
    """GET the wiki's article; PUT to save a new version of it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "PUT": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: ArticleDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return the wiki's article, body included."""
        _location, wiki, profile = self.resolve(request, location_slug)
        article = get_article(wiki=wiki)
        if article is None:
            raise Http404
        return Response(
            {
                "id": article.pk,
                "content": article.content,
                "content_html": article.content_html,
                "toc": article.toc,
                "word_count": article.word_count(),
                "last_edited_by": masked_editor_name(article.last_edited_by, profile),
                "updated": article.updated.isoformat(),
                "base_revision_id": latest_revision_id(article),
            },
        )

    @extend_schema(request=ArticleSaveSerializer, responses={200: ArticleDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def put(self, request: Request, location_slug: str) -> Response:
        """Save a new version of the wiki's article, refusing to clobber a concurrent edit."""
        _location, wiki, profile = self.resolve(request, location_slug)

        serializer = ArticleSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                article, _revision = save_article_checked(
                    editor=profile,
                    content=data["content"],
                    edit_summary=data.get("edit_summary", ""),
                    base_revision_id=data["base_revision_id"],
                    wiki=wiki,
                )
        except ArticleConflictError as exc:
            # Mirrors the internal editor's conflict handling exactly: nothing
            # is written, and the client is told which revision is current so
            # it can show the other edit and re-base.
            return Response(
                {"error": "This article changed while you were editing.", "conflict": True, "current_revision_id": exc.current_revision_id},
                status=409,
            )

        return Response(
            {
                "id": article.pk,
                "content": article.content,
                "content_html": article.content_html,
                "toc": article.toc,
                "word_count": article.word_count(),
                "last_edited_by": masked_editor_name(article.last_edited_by, profile),
                "updated": article.updated.isoformat(),
                "base_revision_id": latest_revision_id(article),
            },
        )


class WikiArticleRevisionsView(PaginatedListMixin, WikiApiView):
    """GET the article's revision history, newest first."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
    }

    @extend_schema(responses={200: ArticleRevisionSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of the article's revisions."""
        _location, wiki, profile = self.resolve(request, location_slug)
        article = get_article(wiki=wiki)
        if article is None:
            raise Http404

        revisions = list(article.revisions.select_related("editor__user", "restored_from").order_by("-created", "-pk"))
        rows = [
            {
                "id": revision.pk,
                "edit_summary": revision.edit_summary,
                "editor": masked_editor_name(revision.editor, profile),
                # Delta against the revision immediately before this one;
                # `revisions` is newest-first, so that is the next item.
                "size_delta": revision.size_delta(revisions[index + 1] if index + 1 < len(revisions) else None),
                "restored_from": revision.restored_from_id,
                "created": revision.created.isoformat(),
            }
            for index, revision in enumerate(revisions)
        ]
        return self.paginated_response(rows, ArticleRevisionSerializer, request)


class WikiArticleRevisionDetailView(WikiApiView):
    """GET one article revision, with its diff against the preceding one. DELETE to scrub it from history.

    Deletion is self-service, restricted to the revision's own
    ``editor`` - this is a contributor removing something they personally
    wrote (an accidentally-shared private detail, say), not moderation of
    someone else's edit. It never touches the article's *current* text:
    ``Article.content`` is its own denormalized field, not derived live from
    the newest revision, so deleting even the newest revision only removes it
    from the history list. A revision that some other, later restore points
    back at (``ArticleRevision.restored_from``) is left with that link set to
    null by the FK's own ``on_delete=SET_NULL`` - no extra handling needed
    here for the dangling reference.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: ArticleRevisionDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str, revision_id: int) -> Response:
        """Return one revision's content plus a diff against its predecessor."""
        _location, wiki, profile = self.resolve(request, location_slug)
        article = get_article(wiki=wiki)
        if article is None:
            raise Http404

        # Scoped to this wiki's article - a bare id would expose revisions of
        # every article in the database.
        revision = get_object_or_404(ArticleRevision, id=revision_id, article=article)
        previous = article.revisions.filter(created__lt=revision.created).order_by("-created", "-pk").first()

        return Response(
            {
                "id": revision.pk,
                "edit_summary": revision.edit_summary,
                "editor": masked_editor_name(revision.editor, profile),
                "size_delta": revision.size_delta(previous),
                "restored_from": revision.restored_from_id,
                "created": revision.created.isoformat(),
                "content": revision.content,
                # DiffRow carries only kind ("context"/"add"/"del"/"skip") and
                # the line text - there are no line numbers to expose.
                "diff": [{"kind": row.kind, "text": row.text} for row in diff_revisions(previous.content if previous is not None else "", revision.content)],
            },
        )

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str, revision_id: int) -> Response:
        """Permanently remove one revision the caller themselves wrote.

        A 404, not a 403, when the revision exists but belongs to someone
        else - matching every other lookup miss in this module. A caller who
        cannot see the wiki learns nothing beyond "no such revision" either
        way, so telling apart "wrong author" from "wrong wiki" would be a
        needless extra disclosure.

        Args:
            request: The authenticated request.
            location_slug: The Location slug or uuid from the URL.
            revision_id: The revision to delete, scoped to this wiki's article.

        Returns:
            204 on success, or the uniform 404 when the revision does not
            exist, belongs to a different article, or was not authored by
            the caller.
        """
        _location, wiki, profile = self.resolve(request, location_slug)
        article = get_article(wiki=wiki)
        if article is None:
            raise Http404

        revision = get_object_or_404(ArticleRevision, id=revision_id, article=article, editor=profile)
        revision.delete()
        return Response(status=204)


class WikiArticleRevisionRestoreView(WikiApiView):
    """POST to restore an older article revision as the newest one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: ArticleDetailSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str, revision_id: int) -> Response:
        """Restore one revision's content, appending it as a new revision."""
        _location, wiki, profile = self.resolve(request, location_slug)
        article = get_article(wiki=wiki)
        if article is None:
            raise Http404

        revision = get_object_or_404(ArticleRevision, id=revision_id, article=article)
        with transaction.atomic():
            article, _new_revision = restore_revision(scope_article=article, revision=revision, editor=profile)

        return Response(
            {
                "id": article.pk,
                "content": article.content,
                "content_html": article.content_html,
                "toc": article.toc,
                "word_count": article.word_count(),
                "last_edited_by": masked_editor_name(article.last_edited_by, profile),
                "updated": article.updated.isoformat(),
                "base_revision_id": latest_revision_id(article),
            },
        )


class _CommentListMixin(PaginatedListMixin):
    """Shared list/create behavior for pin- and wiki-scoped comment endpoints."""

    def _list_comments(self, request: Request, comments_qs: Any, profile: Profile) -> Response:
        """Return one page of the comments *profile* may see.

        Args:
            request: The request driving pagination.
            comments_qs: All comments on the host pin or wiki.
            profile: The requesting profile.

        Returns:
            A paginated envelope of serialized comments.
        """
        top_level = list(top_level_comment_queryset(comments_qs))
        # Every visibility decision - including the @location mention gate that
        # drops a comment entirely rather than redacting it - happens here, in
        # the same call the internal comment panel makes.
        visible = visible_comment_tree(top_level, profile)
        rows = [_serialize_comment(item, profile) for item in visible]
        return self.paginated_response(rows, CommentSerializer, request)

    def _create_comment(self, request: Request, profile: Profile, *, pin: Pin | None = None, wiki: Any = None) -> Response:
        """Validate and create a comment on a pin or wiki.

        Args:
            request: The request carrying the submitted comment.
            profile: The authoring profile.
            pin: Host pin, or None for a wiki comment.
            wiki: Host wiki, or None for a pin comment.

        Returns:
            The created comment, serialized, with status 201.
        """
        serializer = CommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        parent: CommentModel | None = None
        if data.get("parent_id") is not None:
            # Scoped to the same host, so a comment id from another pin/wiki
            # can't be used to graft a reply across threads - and gated by the
            # same visibility rules the list applies, so a parent the caller
            # was never shown reads as "no such comment" rather than accepting
            # the reply. Without the second check a guessed sequential id
            # confirmed its own existence *and* created a reply the author
            # could never retrieve, since visible_comment_tree omits a hidden
            # parent together with everything under it.
            scope = {"pin": pin} if pin is not None else {"wiki": wiki}
            parent = get_object_or_404(Comment.objects.select_related("profile"), id=data["parent_id"], **scope)
            if not comment_is_visible(parent, profile):
                raise Http404

        try:
            comment = create_comment(profile=profile, pin=pin, wiki=wiki, text=data["text"], parent=parent)
        except CommentValidationError as exc:
            return Response({"error": exc.safe_message}, status=400)

        # The freshly created comment never passes through visible_comment_tree,
        # so its mentions are resolved here without the gate that call would
        # have applied. comment_mentions performs a *global* location lookup:
        # fed a crafted `@[name](loc:<uuid>)` token for a location the author
        # has not pinned, it answers with the real slug when that uuid exists
        # and echoes the submitted uuid when it doesn't - turning comment
        # creation into exactly the location-existence oracle the mention gate
        # is built to prevent. Serializing through the same gate the list uses
        # closes it; a comment whose own author cannot see it is refused
        # outright rather than persisted invisibly.
        if not comment_is_visible(comment, profile):
            comment.delete()
            return Response({"error": "That comment mentions a location you haven't pinned."}, status=400)

        return Response(
            {
                "id": comment.pk,
                "text": comment.text,
                "mentions": comment_mentions(comment.text),
                "author": masked_editor_name(comment.profile, profile),
                "author_is_self": True,
                "image_url": None,
                "has_map": False,
                "reactions": {},
                "parent_was_deleted": False,
                "created": comment.created.isoformat(),
                "replies": [],
            },
            status=201,
        )


class WikiCommentsView(_CommentListMixin, WikiApiView):
    """GET the wiki's comment thread; POST to add a comment."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.WIKI_READ}),
        "POST": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={200: CommentSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, location_slug: str) -> Response:
        """Return one page of the wiki's visible comments."""
        _location, wiki, profile = self.resolve(request, location_slug)
        return self._list_comments(request, wiki.comments.all(), profile)

    @extend_schema(request=CommentCreateSerializer, responses={201: CommentSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, location_slug: str) -> Response:
        """Post a comment on the wiki."""
        _location, wiki, profile = self.resolve(request, location_slug)
        return self._create_comment(request, profile, wiki=wiki)


class WikiCommentDetailView(WikiApiView):
    """DELETE one of the caller's own comments on a wiki."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, location_slug: str, comment_id: int) -> Response:
        """Delete one comment. Someone else's reads as not found, never 403."""
        _location, wiki, profile = self.resolve(request, location_slug)
        # profile= in the lookup, not a permission branch: another user's
        # comment id must be indistinguishable from a nonexistent one.
        comment = get_object_or_404(Comment, id=comment_id, wiki=wiki, profile=profile)
        comment.delete()
        return Response(status=204)


@extend_schema_view(
    put=extend_schema(responses={200: None, 400: ErrorSerializer, 404: ErrorSerializer}),
    delete=extend_schema(responses={200: None, 400: ErrorSerializer, 404: ErrorSerializer}),
)
class WikiCommentReactionView(_ReactionMixin, WikiApiView):
    """PUT to add an emoji reaction to a wiki comment; DELETE to remove it.

    Nothing but configuration: the idempotent toggle, the emoji check, the
    ``{"reactions": ...}`` envelope and the 400/404 discipline all live in
    :class:`~urbanlens.dashboard.external_api.mixins._ReactionMixin`, shared
    with the pin-comment and group-message reaction endpoints. The schema
    annotations move to ``extend_schema_view`` because the handlers themselves
    are inherited and there is no local ``put``/``delete`` to decorate.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.WIKI_WRITE}),
        "DELETE": frozenset({ApiKeyScope.WIKI_WRITE}),
    }

    reaction_target_field = "comment"
    reaction_allowed_emojis = ALLOWED_EMOJIS
    reaction_toggle = staticmethod(toggle_reaction)
    reaction_summarizer = staticmethod(aggregate_reactions)

    def resolve_reaction_target(self, request: Request, **kwargs: Any) -> tuple[CommentModel, Profile]:
        """Resolve the wiki comment being reacted to, or 404.

        Every gate is a lookup rather than a permission branch: ``self.resolve``
        404s unless the caller has pinned the location (see the module
        docstring), ``wiki=wiki`` keeps a comment id from another wiki - or
        from a wiki this caller cannot see - from being reachable by guessing
        integers, and :func:`comment_is_visible` applies the same per-comment
        gates the thread listing does.

        That third check is not redundant with the second. Wiki scope alone
        left every comment on a wiki the caller can see reachable by its
        sequential id, including the ones ``visible_comment_tree`` drops for
        this viewer: hidden by the author's comment-visibility, awaiting a
        malware scan, or naming a location the caller has not pinned. Reacting
        to one returned its reaction summary (confirming the id exists) and
        notified an author whose comments this caller is not allowed to read.

        Args:
            request: The authenticated request.
            **kwargs: URL keyword arguments; ``location_slug`` and
                ``comment_id`` are read here.

        Returns:
            Tuple of (the comment, the requesting profile).

        Raises:
            Http404: Unknown location, invisible wiki, a comment id that is not
                on that wiki, or a comment this caller was never shown - all
                indistinguishable.
        """
        _location, wiki, profile = self.resolve(request, kwargs["location_slug"])
        comment = get_object_or_404(Comment.objects.select_related("profile"), id=kwargs["comment_id"], wiki=wiki)
        if not comment_is_visible(comment, profile):
            raise Http404
        return comment, profile


class PinCommentsView(OwnedPinMixin, _CommentListMixin, ExternalApiView):
    """GET the caller's own comments on their pin; POST to add one.

    Uses ``pins:*`` rather than ``wiki:*``: a pin's comment thread is the
    owner's own private annotation of their own pin, not shared community
    content.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(responses={200: CommentSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the pin's visible comments."""
        pin = _own_pin_or_404(self, request, pin_slug)
        return self._list_comments(request, pin.comments.all(), request.user.profile)

    @extend_schema(request=CommentCreateSerializer, responses={201: CommentSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str) -> Response:
        """Post a comment on the caller's own pin."""
        pin = _own_pin_or_404(self, request, pin_slug)
        return self._create_comment(request, request.user.profile, pin=pin)


class PinCommentDetailView(OwnedPinMixin, ExternalApiView):
    """DELETE one of the caller's own comments on their pin."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "DELETE": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, pin_slug: str, comment_id: int) -> Response:
        """Delete one comment on the caller's own pin."""
        pin = _own_pin_or_404(self, request, pin_slug)
        comment = get_object_or_404(Comment, id=comment_id, pin=pin, profile=request.user.profile)
        comment.delete()
        return Response(status=204)


class PinReviewView(OwnedPinMixin, ExternalApiView):
    """GET the caller's own star rating for a pin; PUT to set it, DELETE to clear it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "PUT": frozenset({ApiKeyScope.PINS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(responses={200: ReviewSerializer, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return the caller's rating for this pin, or 404 when unrated."""
        pin = _own_pin_or_404(self, request, pin_slug)
        review = Review.objects.for_pair(request.user.profile, pin).first()
        if review is None:
            raise Http404
        return Response({"rating": review.rating})

    @extend_schema(request=ReviewSerializer, responses={200: ReviewSerializer, 201: ReviewSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, pin_slug: str) -> Response:
        """Set the caller's rating for this pin."""
        pin = _own_pin_or_404(self, request, pin_slug)

        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        review, created = upsert_review(request.user.profile, pin, serializer.validated_data["rating"])
        return Response({"rating": review.rating}, status=201 if created else 200)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, pin_slug: str) -> Response:
        """Clear the caller's rating for this pin."""
        pin = _own_pin_or_404(self, request, pin_slug)
        if not clear_review(request.user.profile, pin):
            raise Http404
        return Response(status=204)
