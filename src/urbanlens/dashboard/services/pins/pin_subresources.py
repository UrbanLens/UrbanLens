"""Write operations for a pin's notes, aliases, and links, shared by every caller.

The HTMX controllers (``controllers.aliases``, ``controllers.links``, the
``PinNote`` views in ``controllers.pin_edit``) and the external API's
sub-resource endpoints both go through here, so the two surfaces cannot drift.
That matters more than it looks: two of these operations have a non-obvious
ordering requirement that is easy to omit when reimplementing them, and getting
it wrong produces a bug that only shows up minutes or hours later.

Specifically, deleting an alias or a link must first record a
:class:`~urbanlens.dashboard.models.auto_removals.model.PinAutoRemoval`
tombstone. Aliases and links are not only user-authored - plugin panels
(Nominatim, EPA) recreate links when their cache goes stale, and the
pin<->wiki alias-mirror signal recreates aliases - so a plain ``delete()``
is silently undone the next time either runs. The tombstone is what tells
that automatic-creation code the value was deliberately removed.

Every mutation also bumps the pin's ``updated`` stamp (:func:`touch_pin`).
Sub-resource rows are part of what ``GET /pins/{slug}/`` serves but they live
in their own tables, so without this a client delta-syncing on ``updated``
would never learn that a pin's notes or links changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH, PinLink
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, normalize_name_for_comparison, sanitize_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: Same scheme restriction ``controllers.links._clean_link_input`` applies.
#: Deliberately *not* ``services.security.url_safety.ensure_public_http_url``: that is an
#: SSRF guard for urls the server itself will fetch, and it costs a DNS
#: resolution per call. A stored bookmark is never fetched by us, and running it
#: through that guard would reject a perfectly legitimate intranet bookmark
#: (and slow every link creation down) for no security benefit.
_validate_link_url = URLValidator(schemes=["http", "https"])


class PinSubResourceError(Exception):
    """Base for the recoverable failures these operations raise.

    Every message is written to be safe to surface directly to the caller.
    ``safe_message`` carries it explicitly rather than relying on callers to
    read ``str(exc)`` - a generic exception accessor invites a future
    subclass to smuggle unsafe content in without anyone noticing.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


class AliasExistsError(PinSubResourceError):
    """The pin already has an alias with this name, case-insensitively."""

    def __init__(self, message: str = "That alias already exists.") -> None:
        super().__init__(message)


class AliasIsCurrentNameError(PinSubResourceError):
    """The alias being deleted is the pin's current name.

    Removing it would leave the pin's own name absent from its alias list,
    which the alias list is defined to always contain.
    """

    def __init__(self, message: str = "This alias is the current name - pick another name first.") -> None:
        super().__init__(message)


class InvalidLinkError(PinSubResourceError):
    """The submitted link url is missing, too long, or not a valid http(s) url."""


class LinkExistsError(PinSubResourceError):
    """The pin already has a link with this url."""

    def __init__(self, message: str = "That link is already on this pin.") -> None:
        super().__init__(message)


def touch_pin(pin: Pin) -> None:
    """Bump the pin's ``updated`` stamp so delta-sync clients see the change.

    Sub-resources live in their own tables, so a note/alias/link write leaves
    the pin row itself untouched and invisible to a client syncing on
    ``updated``. Saving only ``updated`` also skips ``Pin.save()``'s
    name-change alias sync, which keys off ``"name"`` being in ``update_fields``.

    Args:
        pin: The pin to re-stamp.
    """
    pin.save(update_fields=["updated"])


def create_pin_note(pin: Pin, *, text: str) -> PinNote:
    """Add a personal note to a pin.

    Args:
        pin: The pin to attach the note to.
        text: The note body. Leading/trailing whitespace is stripped.

    Returns:
        The created note.

    Raises:
        ValueError: *text* is empty once stripped.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Note text is required.")
    note = PinNote.objects.create(pin=pin, text=cleaned)
    touch_pin(pin)
    return note


def delete_pin_note(note: PinNote) -> None:
    """Delete one personal note.

    No ``PinAutoRemoval`` tombstone, unlike aliases and links: notes are only
    ever authored by the user, so nothing exists that could recreate one.

    Args:
        note: The note to delete.
    """
    pin = note.pin
    note.delete()
    touch_pin(pin)


def create_pin_alias(pin: Pin, *, name: str, kind: str = AliasType.ALTERNATE) -> PinAlias:
    """Add an alternate name to a pin.

    Args:
        pin: The pin to attach the alias to.
        name: The alternate name. Leading/trailing whitespace is stripped.
        kind: An :class:`AliasType` value; defaults to a plain alternate name.

    Returns:
        The created alias.

    Raises:
        ValueError: *name* is empty once stripped.
        AliasExistsError: The pin already has this name as an alias,
            case-insensitively.
    """
    # Validate the value that will actually be stored: PinAlias.save() runs the
    # name through sanitize_name, so a name made only of dropped characters
    # ("\U0001f389", "<>") passes a raw non-empty check and then persists as an
    # empty alias - a blank row that also consumes the pin's unique alias slot.
    cleaned = sanitize_name((name or "").strip()) or ""
    if not cleaned:
        raise ValueError("Name is required.")
    try:
        # atomic() gives IntegrityError its own savepoint to roll back to -
        # without it, catching the error still leaves the surrounding
        # transaction (if any) unusable for further queries until an explicit
        # rollback, including the caller's own re-render or response build.
        with transaction.atomic():
            alias = PinAlias.objects.create(pin=pin, name=cleaned, kind=kind)
    except IntegrityError as exc:
        raise AliasExistsError from exc
    from urbanlens.dashboard.services.undo.mutations import stash_pin_alias_add

    stash_pin_alias_add(pin, alias)
    touch_pin(pin)
    return alias


def delete_pin_alias(pin: Pin, alias: PinAlias) -> None:
    """Delete one of a pin's alternate names.

    Args:
        pin: The alias's owning pin.
        alias: The alias to delete.

    Raises:
        AliasIsCurrentNameError: *alias* is the pin's current name.
    """
    if normalize_name_for_comparison(alias.name) == normalize_name_for_comparison(pin.effective_name):
        raise AliasIsCurrentNameError
    from urbanlens.dashboard.services.undo.mutations import stash_pin_alias_remove

    stash_pin_alias_remove(pin, alias)
    # Tombstone first: an external-source sync or the pin<->wiki alias-mirror
    # signal could otherwise recreate this exact name the moment either one
    # next runs, silently undoing the deletion.
    PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.ALIAS, value=alias.name)
    alias.delete()
    touch_pin(pin)


def promote_alias_to_name(pin: Pin, alias: PinAlias) -> Pin:
    """Make one of a pin's aliases its current name ("use this name").

    The outgoing name is preserved as an alias, so the alias list stays the
    full set of names the pin has ever had and the rename is reversible.
    ``Pin.save()`` already ensures this on any name change; it is re-asserted
    here first because losing the old name is unrecoverable, and cheap to
    prevent - ``get_or_create`` is a no-op in the normal case where the row is
    already there.

    The pin's ``slug`` deliberately does not change: it is the pin's stable
    public identifier, and renaming is expected to be a frequent, low-stakes
    edit rather than something that should break every saved url to the pin.

    Args:
        pin: The alias's owning pin.
        alias: The alias to promote.

    Returns:
        The same *pin* instance, updated in place.
    """
    old_name = (pin.effective_name or "").strip()
    if is_meaningful_name(old_name):
        try:
            with transaction.atomic():
                # Case-insensitive lookup matches the alias uniqueness rule, so
                # a differently-cased row already covering this name is reused
                # rather than racing the DB constraint.
                PinAlias.objects.get_or_create(pin=pin, name__iexact=old_name, defaults={"name": old_name})
        except IntegrityError:
            # Another writer created it concurrently - which is the state we
            # wanted, so there is nothing left to do.
            pass

    pin.name = alias.name
    pin.name_is_user_provided = True
    # Must be .save(), never queryset.update(): Pin.save() is what keeps the
    # alias rows in sync on a name change, and update() bypasses it entirely.
    pin.save(update_fields=["name", "name_is_user_provided", "updated"])
    from urbanlens.dashboard.services.undo.mutations import stash_pin_alias_promote

    stash_pin_alias_promote(pin, before_name=old_name, after_name=pin.name)
    return pin


def create_pin_link(pin: Pin, *, name: str, url: str) -> PinLink:
    """Add an external website link to a pin.

    Args:
        pin: The pin to attach the link to.
        name: The link's display label. May be blank - ``PinLink.display_name``
            falls back to the url's host. Sanitized by ``PinLink.save()``.
        url: The link target. Must be a valid http(s) url.

    Returns:
        The created link.

    Raises:
        InvalidLinkError: *url* is missing, over ``MAX_LINK_URL_LENGTH``, or
            not a valid http(s) url.
    """
    cleaned_url = (url or "").strip()
    cleaned_name = (name or "").strip()
    if not cleaned_url:
        raise InvalidLinkError("A url is required.")
    if len(cleaned_url) > MAX_LINK_URL_LENGTH:
        raise InvalidLinkError(f"That url is too long (max {MAX_LINK_URL_LENGTH:,} characters).")
    try:
        _validate_link_url(cleaned_url)
    except DjangoValidationError as exc:
        raise InvalidLinkError("That doesn't look like a valid http(s) url.") from exc

    try:
        # Same savepoint reasoning as add_pin_alias above.
        with transaction.atomic():
            link = PinLink.objects.create(pin=pin, name=cleaned_name, url=cleaned_url)
    except IntegrityError as exc:
        raise LinkExistsError from exc
    touch_pin(pin)
    return link


def delete_pin_link(pin: Pin, link: PinLink) -> None:
    """Delete one of a pin's external links.

    Args:
        pin: The link's owning pin.
        link: The link to delete.
    """
    # Tombstone first: a plugin panel (Nominatim, EPA) can otherwise recreate
    # this exact link the next time its cache goes stale.
    PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LINK, value=link.url)
    link.delete()
    touch_pin(pin)
