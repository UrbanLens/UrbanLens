"""Serializers for the external API's wiki surface.

Split out of ``serializers.py`` purely for size - that module was already
~600 lines covering pins, sync, settings and push, and the wiki surface adds
roughly a dozen more types. The conventions are identical: hand-rolled, never
subclassing the internal serializers, with field-level bounds acting as the
first line of defense against untrusted input.

Two shape decisions worth stating explicitly, because they differ from the
internal dashboard views:

- **Security is nested** (``{"security": {"fences": "some", ...}}``) for both
  read and write. ``PinDetailSerializer`` already exposes it nested on read, so
  nesting the write side keeps one shape across the whole external surface. The
  internal wiki edit view takes the eight fields flat at the top level; that is
  not mirrored here.
- **``base_revision_id`` is required** on an article save, where the internal
  form lets it be omitted. An omitted value there degrades to ``None``, which
  the conflict rule treats as "no revision expected" - fine for a form that
  always renders the field, but for an API an accidentally-omitted key would
  silently overwrite a concurrent edit. Requiring it (while still allowing an
  explicit ``null`` for "I believe this article is new") makes that impossible.
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.services.text_limits import (
    MAX_ARTICLE_EDIT_SUMMARY_LENGTH,
    MAX_ARTICLE_LENGTH,
    MAX_COMMENT_TEXT_LENGTH,
    MAX_WIKI_DESCRIPTION_LENGTH,
)
from urbanlens.dashboard.services.wiki_aliases import alias_is_current_name

#: Field names only, in the canonical order shared with the model layer.
_SECURITY_FIELD_NAMES = tuple(name for name, _label in SECURITY_FIELDS)


class WikiSecuritySerializer(serializers.Serializer):
    """The 8 security indicators on a wiki (schema-only, read side)."""

    fences = serializers.CharField(read_only=True)
    alarms = serializers.CharField(read_only=True)
    cameras = serializers.CharField(read_only=True)
    security = serializers.CharField(read_only=True)
    signs = serializers.CharField(read_only=True)
    vps = serializers.CharField(read_only=True)
    plywood = serializers.CharField(read_only=True)
    locked = serializers.CharField(read_only=True)


class WikiSecurityUpdateSerializer(serializers.Serializer):
    """The writable half of :class:`WikiSecuritySerializer`.

    Every field is optional, so a caller may submit only the indicators they
    actually observed. Each is a strict ``ChoiceField`` - an unrecognized value
    is a 400, never a silently-skipped field (the internal view's behavior; see
    ``docs/PROBLEMS.md``).
    """

    fences = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    alarms = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    cameras = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    security = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    signs = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    vps = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    plywood = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)
    locked = serializers.ChoiceField(choices=SecurityLevel.choices, required=False)


class WikiStatSerializer(serializers.Serializer):
    """One community stat composite plus the viewer's own vote (schema-only)."""

    #: Nearest whole star, or null when nobody has voted.
    rounded = serializers.IntegerField(read_only=True, allow_null=True)
    #: The precise average, or null when nobody has voted.
    exact = serializers.FloatField(read_only=True, allow_null=True)
    #: Already privacy-fuzzed by ``WikiStatVoteQuerySet.composite`` - this is
    #: never the raw ballot count.
    count = serializers.IntegerField(read_only=True)
    my_vote = serializers.IntegerField(read_only=True, allow_null=True)


class WikiAliasSerializer(serializers.Serializer):
    """An alternate name for a wiki (schema-only).

    The alias list is the full set of names a place is known by, *including* the
    one it currently goes by - which is flagged rather than omitted, exactly as
    ``PinAliasSerializer`` does it. Without ``is_current`` a client has no way to
    tell which entry is the live name, so it can neither mark it in a list nor
    know which entries are worth offering a "use this name" action for; it would
    have to re-derive the answer by string-matching against the wiki detail
    payload, using a normalization rule it has no access to.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    source = serializers.CharField(read_only=True)
    is_current = serializers.SerializerMethodField()

    def get_is_current(self, alias) -> bool:
        """Whether this alias is the wiki's current community name.

        The wiki comes from the serializer context rather than ``alias.wiki`` so
        that serializing a whole list costs no per-row query - the view already
        has the wiki in hand from ``resolve_visible_wiki``.

        Args:
            alias: The alias being serialized.

        Returns:
            True when this alias matches the wiki's current name, compared
            loosely enough to ignore case, spacing, and punctuation. False when
            no wiki was supplied in the context, since nothing can be shown as
            current without one.
        """
        return alias_is_current_name(alias, self.context.get("wiki"))


class WikiAliasCreateSerializer(serializers.Serializer):
    """Validates a submitted wiki alias."""

    name = serializers.CharField(max_length=255)
    kind = serializers.CharField(max_length=50, required=False)


class WikiLinkSerializer(serializers.Serializer):
    """An external link attached to a wiki (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True, allow_blank=True)
    url = serializers.CharField(read_only=True)
    wayback_url = serializers.CharField(read_only=True, allow_null=True)
    order = serializers.IntegerField(read_only=True)


class WikiLinkCreateSerializer(serializers.Serializer):
    """Validates a submitted wiki link."""

    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    url = serializers.URLField(max_length=2000)


class WikiArticleSummarySerializer(serializers.Serializer):
    """The wiki's article without its body (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    word_count = serializers.IntegerField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)
    last_edited_by = serializers.CharField(read_only=True, allow_null=True)
    #: The revision id to send back as ``base_revision_id`` when saving.
    base_revision_id = serializers.IntegerField(read_only=True, allow_null=True)


class WikiDetailSerializer(serializers.Serializer):
    """Documents the full wiki-detail response (schema-only).

    See ``services.wiki_detail.build_wiki_detail``, the function that actually
    builds this payload.
    """

    #: The navigable identifier - wiki routes resolve a Location.
    location_slug = serializers.CharField(read_only=True)
    #: Informational only; not accepted by any route.
    wiki_slug = serializers.CharField(read_only=True, allow_null=True)
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True, allow_null=True)
    pin_type = serializers.CharField(read_only=True)
    indoor_outdoor = serializers.CharField(read_only=True, allow_null=True)
    date_abandoned = serializers.DateField(read_only=True, allow_null=True)
    date_last_active = serializers.DateField(read_only=True, allow_null=True)
    security = WikiSecuritySerializer(read_only=True)
    latitude = serializers.FloatField(read_only=True, allow_null=True)
    longitude = serializers.FloatField(read_only=True, allow_null=True)
    address = serializers.CharField(read_only=True, allow_null=True)
    cover_photo_url = serializers.CharField(read_only=True, allow_null=True)
    boundary = serializers.JSONField(read_only=True, allow_null=True)
    aliases = WikiAliasSerializer(many=True, read_only=True)
    links = WikiLinkSerializer(many=True, read_only=True)
    #: Keyed by stat field: danger / vulnerability / priority / rating.
    stats = serializers.DictField(child=WikiStatSerializer(), read_only=True)
    #: True when too few people have pinned this place to show a number at all.
    pin_count_low = serializers.BooleanField(read_only=True)
    pin_count_approx = serializers.IntegerField(read_only=True, allow_null=True)
    #: Truncated to the 1st of its month, and null whenever pin_count_low.
    first_pinned = serializers.DateField(read_only=True, allow_null=True)
    first_pinned_precision = serializers.CharField(read_only=True)
    article = WikiArticleSummarySerializer(read_only=True, allow_null=True)
    comment_count = serializers.IntegerField(read_only=True)
    created = serializers.DateTimeField(read_only=True)
    updated = serializers.DateTimeField(read_only=True)


class WikiUpdateSerializer(serializers.Serializer):
    """Validates an untrusted wiki PATCH payload.

    Every field is optional (this is a partial update), and unknown keys are
    rejected outright rather than ignored - a client misspelling ``decription``
    should be told so, not handed a 200 for a write that never happened.
    """

    name = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(max_length=MAX_WIKI_DESCRIPTION_LENGTH, required=False, allow_null=True, allow_blank=True)
    date_abandoned = serializers.DateField(required=False, allow_null=True)
    date_last_active = serializers.DateField(required=False, allow_null=True)
    security = WikiSecurityUpdateSerializer(required=False)

    def validate(self, attrs: dict) -> dict:
        """Reject unknown top-level keys and empty payloads.

        Args:
            attrs: The validated field values.

        Returns:
            The same mapping, unchanged.

        Raises:
            serializers.ValidationError: An unrecognized key was submitted, or
                nothing recognizable was.
        """
        submitted = set(self.initial_data or {})
        unknown = submitted - set(self.fields)
        if unknown:
            raise serializers.ValidationError(f"Unrecognized field(s): {', '.join(sorted(unknown))}.")
        if not attrs:
            raise serializers.ValidationError("No changes submitted.")
        return attrs


class WikiEditSerializer(serializers.Serializer):
    """One entry in a wiki's edit history (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    #: ``{field: {"from": str, "to": str}}`` as recorded on the WikiEdit.
    changes = serializers.JSONField(read_only=True)
    reverted = serializers.BooleanField(read_only=True)
    editor = serializers.CharField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)


class WikiStatVoteInputSerializer(serializers.Serializer):
    """Validates a submitted community stat vote."""

    value = serializers.IntegerField(min_value=1, max_value=5)


class ArticleDetailSerializer(serializers.Serializer):
    """A wiki article with its full body (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    #: Raw Markdown source, for a client that wants to edit it.
    content = serializers.CharField(read_only=True, allow_blank=True)
    #: Server-rendered, sanitized HTML, for a client that just wants to show it.
    content_html = serializers.CharField(read_only=True, allow_blank=True)
    toc = serializers.JSONField(read_only=True)
    word_count = serializers.IntegerField(read_only=True)
    last_edited_by = serializers.CharField(read_only=True, allow_null=True)
    updated = serializers.DateTimeField(read_only=True)
    #: Send this back as ``base_revision_id`` to save without conflicting.
    base_revision_id = serializers.IntegerField(read_only=True, allow_null=True)


class ArticleSaveSerializer(serializers.Serializer):
    """Validates an article save.

    ``base_revision_id`` is required (though it may be explicitly ``null``) -
    see this module's docstring for why an omitted value is not allowed to mean
    "no opinion".
    """

    content = serializers.CharField(max_length=MAX_ARTICLE_LENGTH, allow_blank=True)
    edit_summary = serializers.CharField(max_length=MAX_ARTICLE_EDIT_SUMMARY_LENGTH, required=False, allow_blank=True)
    base_revision_id = serializers.IntegerField(required=True, allow_null=True)


class ArticleRevisionSerializer(serializers.Serializer):
    """One entry in an article's revision history (schema-only)."""

    id = serializers.IntegerField(read_only=True)
    edit_summary = serializers.CharField(read_only=True, allow_blank=True)
    editor = serializers.CharField(read_only=True, allow_null=True)
    #: Signed character delta against the preceding revision.
    size_delta = serializers.IntegerField(read_only=True)
    restored_from = serializers.IntegerField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)


class ArticleRevisionDetailSerializer(ArticleRevisionSerializer):
    """One revision plus its content and a diff against its predecessor."""

    content = serializers.CharField(read_only=True, allow_blank=True)
    #: Row-wise diff against the immediately preceding revision.
    diff = serializers.JSONField(read_only=True)


class CommentMentionSerializer(serializers.Serializer):
    """A resolved ``@location`` mention inside a comment (schema-only)."""

    display = serializers.CharField(read_only=True)
    #: Safe to expose: a comment only reaches a viewer who has provably pinned
    #: every location it mentions (see ``services.comments``).
    location_slug = serializers.CharField(read_only=True)


class CommentSerializer(serializers.Serializer):
    """A comment as the external API returns it (schema-only).

    Returns the raw storage-format ``text`` plus resolved ``mentions`` rather
    than server-rendered HTML, so a native client can lay it out itself instead
    of embedding a webview.
    """

    id = serializers.IntegerField(read_only=True)
    text = serializers.CharField(read_only=True)
    mentions = CommentMentionSerializer(many=True, read_only=True)
    #: Masked per the author's profile-visibility setting.
    author = serializers.CharField(read_only=True, allow_null=True)
    author_is_self = serializers.BooleanField(read_only=True)
    image_url = serializers.CharField(read_only=True, allow_null=True)
    has_map = serializers.BooleanField(read_only=True)
    #: ``{emoji: {"count": int, "reacted": bool}}``.
    reactions = serializers.JSONField(read_only=True)
    parent_was_deleted = serializers.BooleanField(read_only=True)
    created = serializers.DateTimeField(read_only=True)

    def get_fields(self) -> dict:
        """Attach the one-level-deep ``replies`` field.

        Declared here rather than inline because a serializer cannot reference
        itself during class-body evaluation.

        Returns:
            The field map, with ``replies`` added.
        """
        fields = super().get_fields()
        fields["replies"] = CommentSerializer(many=True, read_only=True)
        return fields


class CommentCreateSerializer(serializers.Serializer):
    """Validates a submitted comment."""

    text = serializers.CharField(max_length=MAX_COMMENT_TEXT_LENGTH)
    #: The comment being replied to. Scoped to the same pin/wiki by the view.
    parent_id = serializers.IntegerField(required=False, allow_null=True)


class ReviewSerializer(serializers.Serializer):
    """The caller's own star rating for one of their pins."""

    rating = serializers.IntegerField(min_value=0, max_value=5)


class GalleryImageSerializer(serializers.Serializer):
    """One image in a wiki's gallery (schema-only).

    Field names verified against ``models/images/model.py`` - the model stores
    an ``image`` file plus optional attribution metadata, and has no separate
    "url" column, so ``url`` here is derived from ``image.url``.
    """

    id = serializers.IntegerField(read_only=True)
    url = serializers.CharField(read_only=True)
    caption = serializers.CharField(read_only=True, allow_null=True)
    author = serializers.CharField(read_only=True, allow_null=True)
    source_url = serializers.CharField(read_only=True, allow_null=True)
    copyright = serializers.CharField(read_only=True, allow_null=True)
    created = serializers.DateTimeField(read_only=True)
