"""What differs between the Vault's per-kind media galleries (Photos, Documents).

A new gallery needs a spec here, URL routes passing its ``kind``, a page template extending
``pages/vault/media_page.html``, a tile partial, and a TypeScript tile renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

from urbanlens.dashboard.models.images.model import MediaKind


@dataclass(frozen=True, slots=True)
class MediaKindSpec:
    """One Vault gallery's kind-specific behaviour.

    Attributes:
        kind: The :class:`MediaKind` this gallery lists.
        plural: The plural noun, which is also the URL segment, the URL-name stem and the page's element-id
            stem (``photos`` → ``/vault/photos/``, ``vault.photos.items``, ``#photos-page``).
        template: The page template.
        tile_template: The partial rendering the grid's server-side first page.
        upload_field: The multipart field the upload view reads the file from.
        missing_file_error: The 400 message when that field is absent.
        caption_from_filename: Whether an upload is captioned with its filename.
        select_related: Relations the gallery serializer reads on every row.
        offers_from_others: Whether ``?show=from_others`` narrows the gallery to copies of other people's photos.
        organizes_visits: Whether the page carries the organize queue and upload-issue panels.
    """

    kind: str
    plural: str
    template: str
    tile_template: str
    upload_field: str
    missing_file_error: str
    caption_from_filename: bool
    select_related: tuple[str, ...]
    offers_from_others: bool
    organizes_visits: bool

    @property
    def page_url_name(self) -> str:
        """URL name of the gallery page."""
        return f"vault.{self.plural}"

    @property
    def items_url_name(self) -> str:
        """URL name of the windowed grid's JSON page endpoint."""
        return f"vault.{self.plural}.items"

    @property
    def upload_url_name(self) -> str:
        """URL name of the one-file upload endpoint."""
        return f"vault.{self.plural}.upload"


MEDIA_KIND_SPECS: dict[str, MediaKindSpec] = {
    MediaKind.PHOTO: MediaKindSpec(
        kind=MediaKind.PHOTO,
        plural="photos",
        template="dashboard/pages/vault/photos.html",
        tile_template="dashboard/partials/vault/_photo_grid.html",
        upload_field="image",
        missing_file_error="No image provided.",
        caption_from_filename=False,
        select_related=("pin", "wiki", "profile__user"),
        offers_from_others=True,
        organizes_visits=True,
    ),
    MediaKind.DOCUMENT: MediaKindSpec(
        kind=MediaKind.DOCUMENT,
        plural="documents",
        template="dashboard/pages/vault/documents.html",
        tile_template="dashboard/partials/vault/_document_grid.html",
        upload_field="document",
        missing_file_error="No document provided.",
        caption_from_filename=True,
        select_related=("profile__user",),
        offers_from_others=False,
        organizes_visits=False,
    ),
}


def media_kind_spec(kind: str) -> MediaKindSpec:
    """Return the spec for *kind*.

    Args:
        kind: A :class:`MediaKind` value with a Vault gallery.

    Returns:
        The matching :class:`MediaKindSpec`.

    Raises:
        KeyError: *kind* has no Vault gallery - a routing mistake, not a user input.
    """
    return MEDIA_KIND_SPECS[kind]
