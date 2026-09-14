"""External-API views for unblocking, avatars, and private profile annotations.

Every refusal here is 404 with the same body an unknown uuid produces: a blocked party must not be
able to confirm the block exists, and a distinguishing 403 would do exactly that.
**Avatars had no write path.** They are a field on ``Profile``, not an ``Image`` row, so they create
no library entry and consume no photo quota - which is why these routes are gated on
``social:write`` rather than ``photos:write``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer, ProfileDetailSerializer
from urbanlens.dashboard.external_api.serializers_social import (
    AvatarEmojiSerializer,
    ProfileAnnotationsSerializer,
    ProfileNicknameWriteSerializer,
    ProfileTrustWriteSerializer,
    SocialLinksReplaceSerializer,
    SocialLinksResponseSerializer,
)

# Imported rather than reimplemented, for the same reason ``views_trips`` imports ``_trip_detail_payload``:
# ``FriendActionView`` already owns the uuid lookup and the exception-to-status mapping every friendship
# transition shares, and ``ProfileDetailView.get`` already owns the profile payload every profile response must
# match.
from urbanlens.dashboard.external_api.views import ExternalApiView, FriendActionView, ProfileDetailView, _resolve_profile
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.social_link.model import SocialLink
from urbanlens.dashboard.services.profile.avatar import (
    AvatarMalwareDetectedError,
    AvatarScanUnavailableError,
    AvatarTooLargeError,
    AvatarUnsupportedFormatError,
    AvatarUploadError,
    clear_profile_avatar,
    set_profile_avatar,
    set_profile_avatar_from_emoji,
)
from urbanlens.dashboard.services.profile.profile_annotations import (
    MAX_PROFILE_NICKNAME_LENGTH,
    AnnotationError,
    NicknameTooLongError,
    SelfAnnotationError,
    clear_nickname,
    clear_trust,
    get_annotations,
    set_nickname,
    set_trust,
)
from urbanlens.dashboard.services.profile.social_links import get_profile_links
from urbanlens.dashboard.services.social.friendship import unblock_profile

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The single body every "you may not see this profile" refusal carries, on every route in this module. One
#: string, so a caller cannot separate "no such slug" from "not yours" from "not visible to you" by diffing
#: responses.
_NO_SUCH_PROFILE = "No such profile."


class FriendUnblockView(FriendActionView):
    """POST: lift a block this caller placed on the named profile.

    Inherits the shared transition handler, so the uuid lookup, the ``social:write`` gate and the
    error-to-status mapping are the ones every other friendship action uses.

    - not a deletion, because ``FriendshipStatus.can_request`` accepts ``Removed`` and that is what lets
      the two profiles contact each other again.
    """

    not_found_message: ClassVar[str] = _NO_SUCH_PROFILE

    def service_action(self, actor: Profile, target: Profile) -> Friendship:
        """Lift the caller's own block on the target.

        Args:
            actor: The calling profile, which must be the one that blocked.
            target: The blocked profile.

        Returns:
            The relationship, now ``Removed``.

        Raises:
            FriendshipNotFoundError: No block, or not this caller's block.
        """
        return unblock_profile(actor, target)


class _OwnProfileApiView(ExternalApiView):
    """Base for the routes that may only ever touch the caller's own profile.

    Factored out because the resolution rule is the security property, not a convenience: anything other
    than the caller's own slug answers 404, never 403, and never distinguishes "no such profile" from
    "not yours".
    A 403 would confirm that some other user owns that slug, which is precisely what the slug-addressed
    profile surface is careful not to reveal.
    """

    def own_profile(self, request: Request, profile_slug: str) -> Profile | None:
        """The caller's profile, if *profile_slug* names it.

        Args:
            request: The authenticated request.
            profile_slug: A profile slug or uuid from the path.

        Returns:
            The caller's own profile, or None when the slug names anything else - including a profile that
            does not exist.
        """
        viewer = request.user.profile
        target = _resolve_profile(profile_slug)
        if target is None or target.pk != viewer.pk:
            return None
        return target

    def profile_detail(self, request: Request, profile_slug: str) -> Response:
        """Answer with the shared profile-detail payload.

        Delegates to ``ProfileDetailView.get`` rather than assembling a second payload, so an avatar write
        and a subsequent profile read can never disagree about the same profile.

        Args:
            request: The authenticated request.
            profile_slug: The caller's own slug.

        Returns:
            The 200 profile-detail response.
        """
        return ProfileDetailView().get(request, profile_slug)


class ProfileAvatarView(_OwnProfileApiView):
    """PUT a new avatar image; DELETE the current one.

    ``social:write`` on both, not ``photos:write``: an avatar creates no ``Image`` row and consumes no
    photo quota, and ``photos:write`` would additionally authorize destroying the user's photo library.
    PUT answers with the full profile rather than a bare avatar URL so a client can render the updated
    header from one response.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.SOCIAL_WRITE}),
        "DELETE": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }
    #: ``FormParser`` alongside the multipart parser so a client that sends the file part correctly but the rest
    #: of the body as urlencoded still parses, rather than getting an unhelpful 415.
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={"multipart/form-data": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}},
        responses={200: ProfileDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 413: ErrorSerializer, 422: ErrorSerializer, 503: ErrorSerializer},
    )
    def put(self, request: Request, profile_slug: str) -> Response:
        """Replace the caller's avatar with the uploaded image.

        Args:
            request: The authenticated request, carrying a ``file`` part.
            profile_slug: The caller's own slug or uuid.

        Returns:
            200 with the refreshed profile; 400 when no file was sent or the file's bytes contradict its
            declared type; 413 when it exceeds the...
        """
        profile = self.own_profile(request, profile_slug)
        if profile is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response({"error": "No file provided."}, status=400)

        try:
            set_profile_avatar(profile, uploaded)
        except AvatarTooLargeError as exc:
            logger.info("external API avatar upload rejected for %s: %s", profile.pk, exc)
            return Response({"error": "That file is too large. Please upload a smaller image."}, status=413)
        except AvatarUnsupportedFormatError as exc:
            logger.info("external API avatar upload rejected for %s: %s", profile.pk, exc)
            return Response({"error": "That file isn't a supported image format. Please upload a JPEG, PNG, GIF, WebP, HEIC, BMP, TIFF, or AVIF file."}, status=400)
        except AvatarMalwareDetectedError as exc:
            logger.info("external API avatar upload rejected for %s: %s", profile.pk, exc)
            return Response({"error": "That file failed a security scan and wasn't uploaded."}, status=422)
        except AvatarScanUnavailableError as exc:
            logger.warning("external API avatar upload scan unavailable for %s: %s", profile.pk, exc)
            return Response({"error": "Our antivirus scanner is temporarily unavailable. Please try again shortly."}, status=503)
        except AvatarUploadError as exc:
            logger.info("external API avatar upload rejected for %s: %s", profile.pk, exc)
            return Response({"error": "That avatar couldn't be uploaded."}, status=400)

        return self.profile_detail(request, profile_slug)

    @extend_schema(responses={204: None, 404: ErrorSerializer})
    def delete(self, request: Request, profile_slug: str) -> Response:
        """Remove the caller's avatar, deleting the stored file.

        Args:
            request: The authenticated request.
            profile_slug: The caller's own slug or uuid.

        Returns:
            204 on success (including when there was no avatar); 404 for any slug that is not the caller's
            own.
        """
        profile = self.own_profile(request, profile_slug)
        if profile is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        clear_profile_avatar(profile)
        return Response(status=204)


class ProfileAvatarEmojiView(_OwnProfileApiView):
    """POST: generate an emoji avatar instead of uploading one.

    The same option the site's own picker offers, and the reason a client does not need an image library
    to give a new account a recognizable avatar.
    The generated SVG is built from a fixed template plus two values the serializer has already
    constrained to closed sets, so no upload scanning applies.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    @extend_schema(request=AvatarEmojiSerializer, responses={200: ProfileDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, profile_slug: str) -> Response:
        """Generate and store an emoji avatar for the caller.

        Args:
            request: The authenticated request carrying ``animal`` and ``color``.
            profile_slug: The caller's own slug or uuid.

        Returns:
            200 with the refreshed profile; 400 for an unknown animal or a colour outside the site's
            palette; 404 for anyone else's slug.
        """
        profile = self.own_profile(request, profile_slug)
        if profile is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        serializer = AvatarEmojiSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        set_profile_avatar_from_emoji(profile, data["animal"], data["color"])
        return self.profile_detail(request, profile_slug)


class _AnnotationApiView(ExternalApiView):
    """Base for the private-annotation routes: nickname and trust.

    A subject the caller may not see answers 404 - the same 404 an unknown slug gets - because
    ``can_view_profile`` failing means the caller was never shown that this account exists.
    The subject can never reach another person's annotations about them, however visible that person's
    profile is.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SOCIAL_READ}),
        "PUT": frozenset({ApiKeyScope.SOCIAL_WRITE}),
        "DELETE": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    def subject(self, request: Request, profile_slug: str) -> Profile | None:
        """The annotated profile, if this caller may see it at all.

        Args:
            request: The authenticated request.
            profile_slug: A profile slug or uuid from the path.

        Returns:
            The subject profile, or None when it does not resolve or its visibility settings exclude the
            caller.
        """
        viewer = request.user.profile
        target = _resolve_profile(profile_slug)
        if target is None or not target.can_view_profile(viewer):
            return None
        return target

    def annotations_response(self, viewer: Profile, subject: Profile) -> Response:
        """Build the shared annotations payload.

        Returned by the write endpoints as well as the read one, so a client gets the post-write state
        without a follow-up call it might forget to make.

        Args:
            viewer: The calling profile - always the annotations' author.
            subject: The annotated profile.

        Returns:
            200 with the caller's nickname, trust rating and note count.
        """
        annotations = get_annotations(viewer, subject)
        return Response(
            ProfileAnnotationsSerializer(
                {
                    "nickname": annotations.nickname,
                    "trust": annotations.trust,
                    "note_count": annotations.note_count,
                },
            ).data,
        )


class ProfileAnnotationsView(_AnnotationApiView):
    """GET: everything the caller privately records about one profile.

    One call for the three things a profile screen needs to render the viewer's own overlay - nickname,
    trust, and how many notes exist - instead of three.
    A combined write would have to be non-idempotent: notes are a collection and the other two are
    singletons, so a single partial update could not both replace a nickname and merge a note list
    without a replayed request either duplicating or dropping notes.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SOCIAL_READ}),
    }

    @extend_schema(responses={200: ProfileAnnotationsSerializer, 404: ErrorSerializer})
    def get(self, request: Request, profile_slug: str) -> Response:
        """Return the caller's private annotations about the named profile.

        Args:
            request: The authenticated request.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the annotations (all null/zero when none exist); 404 when the profile does not resolve
            or is not visible to the caller.
        """
        subject = self.subject(request, profile_slug)
        if subject is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)
        return self.annotations_response(request.user.profile, subject)


class ProfileNicknameView(_AnnotationApiView):
    """PUT or DELETE the caller's private nickname for another profile.

    A singleton per (author, subject) pair, so ``PUT`` is a full replacement and is idempotent;
    ``DELETE`` is idempotent too, and succeeds whether or not a nickname was set.
    Both answer with the full annotations payload so a client's overlay stays in sync from one round
    trip.
    """

    @extend_schema(request=ProfileNicknameWriteSerializer, responses={200: ProfileAnnotationsSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, profile_slug: str) -> Response:
        """Set or replace the caller's nickname for the named profile.

        Args:
            request: The authenticated request carrying ``nickname``.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the refreshed annotations; 400 when annotating yourself; 404 when the profile does not
            resolve or is not visible.
        """
        subject = self.subject(request, profile_slug)
        if subject is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        serializer = ProfileNicknameWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        viewer = request.user.profile
        try:
            set_nickname(viewer, subject, serializer.validated_data["nickname"])
        except SelfAnnotationError as exc:
            logger.info("external API nickname rejected for %s: %s", viewer.pk, exc)
            return Response({"error": "You cannot set a nickname for your own profile."}, status=400)
        except NicknameTooLongError as exc:
            logger.info("external API nickname rejected for %s: %s", viewer.pk, exc)
            return Response({"error": f"Nickname must be {MAX_PROFILE_NICKNAME_LENGTH} characters or fewer."}, status=400)
        except AnnotationError as exc:
            logger.info("external API nickname rejected for %s: %s", viewer.pk, exc)
            return Response({"error": "That nickname is invalid."}, status=400)
        return self.annotations_response(viewer, subject)

    @extend_schema(responses={200: ProfileAnnotationsSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, profile_slug: str) -> Response:
        """Remove the caller's nickname for the named profile.

        Args:
            request: The authenticated request.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the refreshed annotations; 404 when the profile does not resolve or is not visible.
        """
        subject = self.subject(request, profile_slug)
        if subject is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        viewer = request.user.profile
        clear_nickname(viewer, subject)
        return self.annotations_response(viewer, subject)


class ProfileTrustView(_AnnotationApiView):
    """PUT or DELETE the caller's private 1-5 trust rating for another profile.

    Kept separate from the nickname route even though both are singletons on the same pair: they are
    independently meaningful, and a client that only wants to change one should not have to resend the
    other and risk clobbering a value it read before someone else's edit.
    """

    @extend_schema(request=ProfileTrustWriteSerializer, responses={200: ProfileAnnotationsSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, profile_slug: str) -> Response:
        """Set or replace the caller's trust rating for the named profile.

        Args:
            request: The authenticated request carrying ``rating``.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the refreshed annotations; 400 when rating yourself or when the rating is out of range;
            404 when the profile does not resolve...
        """
        subject = self.subject(request, profile_slug)
        if subject is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        serializer = ProfileTrustWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        viewer = request.user.profile
        try:
            set_trust(viewer, subject, serializer.validated_data["rating"])
        except SelfAnnotationError as exc:
            logger.info("external API trust rating rejected for %s: %s", viewer.pk, exc)
            return Response({"error": "You cannot rate your own profile."}, status=400)
        except AnnotationError as exc:
            logger.info("external API trust rating rejected for %s: %s", viewer.pk, exc)
            return Response({"error": "That rating is invalid."}, status=400)
        return self.annotations_response(viewer, subject)

    @extend_schema(responses={200: ProfileAnnotationsSerializer, 404: ErrorSerializer})
    def delete(self, request: Request, profile_slug: str) -> Response:
        """Remove the caller's trust rating for the named profile.

        Args:
            request: The authenticated request.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the refreshed annotations; 404 when the profile does not resolve or is not visible.
        """
        subject = self.subject(request, profile_slug)
        if subject is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        viewer = request.user.profile
        clear_trust(viewer, subject)
        return self.annotations_response(viewer, subject)


class ProfileSocialLinksView(_OwnProfileApiView):
    """GET a profile's public social links; PUT to fully replace the caller's own.

    Visible to anyone who can see the profile at all - unlike contact methods, a social link carries no
    separate ``contact_visibility`` gate, matching ``controllers.userprofile.ViewProfileView``, which
    renders them for any visitor the profile-visibility check admits.
    Only the owner may write them, via :class:`_OwnProfileApiView`'s own-slug-only resolution.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        # Reading someone else's links is a social read, like the rest of
        # ProfileDetailView.get - matches its scope pairing exactly.
        "GET": frozenset({ApiKeyScope.PROFILE_READ, ApiKeyScope.SOCIAL_READ}),
        "PUT": frozenset({ApiKeyScope.SOCIAL_WRITE}),
    }

    def _links_response(self, profile: Profile) -> Response:
        """Build the shared social-links payload.

        Args:
            profile: The profile whose links are being served.

        Returns:
            200 with every link currently on the profile.
        """
        return Response(SocialLinksResponseSerializer({"links": get_profile_links(profile)}).data)

    @extend_schema(responses={200: SocialLinksResponseSerializer, 404: ErrorSerializer})
    def get(self, request: Request, profile_slug: str) -> Response:
        """Return the named profile's social links, if this caller may see the profile at all.

        Args:
            request: The authenticated request.
            profile_slug: The subject's slug or uuid.

        Returns:
            200 with the link list (empty when none are set); 404 when the profile does not resolve or its
            visibility excludes the caller.
        """
        viewer = request.user.profile
        target = _resolve_profile(profile_slug)
        if target is None or not target.can_view_profile(viewer):
            return Response({"error": _NO_SUCH_PROFILE}, status=404)
        return self._links_response(target)

    @extend_schema(request=SocialLinksReplaceSerializer, responses={200: SocialLinksResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def put(self, request: Request, profile_slug: str) -> Response:
        """Replace the caller's entire social-link set.

        Args:
            request: The authenticated request carrying ``links``.
            profile_slug: The caller's own slug or uuid.

        Returns:
            200 with the refreshed link list; 400 when a handle/URL fails its platform's rule or a platform
            is repeated; 404 for any slug that is...
        """
        profile = self.own_profile(request, profile_slug)
        if profile is None:
            return Response({"error": _NO_SUCH_PROFILE}, status=404)

        serializer = SocialLinksReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submitted = serializer.validated_data["links"]

        submitted_platforms = {link["platform"] for link in submitted}
        profile.social_links.exclude(platform__in=submitted_platforms).delete()
        for link in submitted:
            SocialLink.objects.update_or_create(profile=profile, platform=link["platform"], defaults={"handle": link["handle"]})

        return self._links_response(profile)
