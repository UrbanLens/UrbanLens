"""Reading a posted body as a mapping of fields, however the client sent it."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import TYPE_CHECKING, Any

from django.core.exceptions import BadRequest
from rest_framework import parsers
from rest_framework.exceptions import ParseError

if TYPE_CHECKING:
    from typing import IO

    from django.http import HttpRequest
    from rest_framework.request import Request

#: Content types whose body Django parses into ``request.POST``.
FORM_CONTENT_TYPES = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})


#: Postgres refuses a NUL in text, so a request holding one is refused before a view can pass it to a query.
NUL_REFUSAL = "Text can't contain a NUL character."


def holds_nul(value: object) -> bool:
    """Whether any string in *value*, a decoded JSON value or a form's fields, holds a NUL character.

    Args:
        value: A string, or lists, tuples and dicts of them, keys included.

    Returns:
        True when one does.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)
    return False


class MalformedBodyError(BadRequest, ValueError):
    """Posted JSON that does not decode, or is not the shape asked for. Django answers it with a 400.

    A ``ValueError`` as well, so a view that already catches one for undecodable JSON answers this the same way.
    """


def decode_json(raw: str | bytes) -> Any:
    """Decode client-supplied JSON of any shape: a body, a form field, a query parameter or a WebSocket frame.

    ``json.loads`` raises ``RecursionError``, not a ``ValueError``, on input nested past the interpreter's limit.

    Args:
        raw: The JSON text.

    Returns:
        The decoded value.

    Raises:
        MalformedBodyError: *raw* is not JSON, nests too deeply to decode, or holds a NUL in a string.
    """
    try:
        text = raw.decode(json.detect_encoding(raw), "surrogatepass") if isinstance(raw, bytes) else raw
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise MalformedBodyError("The request body is not valid JSON.") from exc
    # A NUL can only be in a JSON string escaped.
    if "u0000" in text and holds_nul(value):
        raise MalformedBodyError(NUL_REFUSAL)
    return value


def posted_json_object(request: HttpRequest) -> dict[str, Any]:
    """The request's JSON body, which must be an object.

    Args:
        request: The incoming request.

    Returns:
        The decoded object; ``{}`` for a form post or an empty body.

    Raises:
        MalformedBodyError: The body is not JSON, or is JSON that is not an object.
    """
    if request.content_type in FORM_CONTENT_TYPES or not request.body:
        return {}
    data = decode_json(request.body)
    if not isinstance(data, dict):
        raise MalformedBodyError("The request body must be a JSON object.")
    return data


def json_body(request: HttpRequest) -> bytes:
    """The raw body of a request a view decodes as JSON itself.

    A form's body may already have been read into ``request.POST``, by the CSRF check or the NUL refusal; reading
    ``request.body`` after that raises ``RawPostDataException``, a 500.

    Args:
        request: The incoming request.

    Returns:
        The body.

    Raises:
        MalformedBodyError: The request was posted as a form, not JSON.
    """
    if request.content_type in FORM_CONTENT_TYPES:
        raise MalformedBodyError("The request body must be JSON.")
    return request.body


def posted_fields(request: HttpRequest) -> dict[str, Any]:
    """A form post's fields, one value each, or the JSON object body.

    Args:
        request: The incoming request.

    Returns:
        The fields; ``{}`` when there are none to read.

    Raises:
        MalformedBodyError: A JSON body that is malformed or not an object.
    """
    if request.content_type in FORM_CONTENT_TYPES:
        return request.POST.dict()
    return posted_json_object(request)


def text_field(fields: Mapping[str, Any], name: str) -> str:
    """A posted field's text, stripped: ``""`` when it is absent, or a JSON body posted anything but a string.

    A form posts only strings; a JSON body may post any JSON value under a name a view reads as text.

    Args:
        fields: What :func:`posted_fields` or :func:`posted_json_object` returned.
        name: The field.

    Returns:
        The stripped text.
    """
    value = fields.get(name)
    return value.strip() if isinstance(value, str) else ""


def text_type_error(fields: Mapping[str, Any], *names: str) -> str | None:
    """A refusal for the first of *names* a JSON body posted as neither a string nor null.

    An update that read it with :func:`text_field` would clear the field; refusing the request keeps it.

    Args:
        fields: What :func:`posted_fields` or :func:`posted_json_object` returned.
        names: The fields the view reads as text.

    Returns:
        The refusal, or None.
    """
    for name in names:
        if fields.get(name) is not None and not isinstance(fields[name], str):
            return f"{name} must be text."
    return None


def list_field(fields: Mapping[str, Any], name: str) -> list[Any]:
    """A JSON body's list field: ``[]`` when it is absent or posted as anything but a list.

    Args:
        fields: What :func:`posted_json_object` returned.
        name: The field.

    Returns:
        The list, its items unchecked.
    """
    value = fields.get(name)
    return value if isinstance(value, list) else []


def drf_data_object(request: Request) -> Mapping[str, Any]:
    """A DRF request's parsed body, which must be a mapping.

    Args:
        request: The incoming DRF request.

    Returns:
        ``request.data``: a dict for JSON, a ``QueryDict`` for a form.

    Raises:
        ParseError: The body is not a mapping, or could not be parsed. DRF answers it with a 400.
    """
    data = request.data
    if not isinstance(data, Mapping):
        raise ParseError("The request body must be a JSON object.")
    return data


class FormParser(parsers.FormParser):
    """DRF's form parser, refusing a NUL in any field, as ``NulCharacterRefusalMiddleware`` does for a Django view."""

    def parse(self, stream: IO[bytes], media_type: str | None = None, parser_context: Mapping[str, Any] | None = None) -> Any:
        """Parse *stream* as a urlencoded form.

        Args:
            stream: The request body.
            media_type: The request's media type.
            parser_context: DRF's parser context.

        Returns:
            The fields.

        Raises:
            ParseError: A field name or value holds a NUL.
        """
        data = super().parse(stream, media_type, parser_context)
        if holds_nul(list(data.lists())):
            raise ParseError(NUL_REFUSAL)
        return data


class MultiPartParser(parsers.MultiPartParser):
    """DRF's multipart parser, refusing a NUL in any field. An uploaded file's bytes are not read.

    The middleware leaves a CSRF-exempt view's multipart body unread, and every DRF view is CSRF-exempt.
    """

    def parse(self, stream: IO[bytes], media_type: str | None = None, parser_context: Mapping[str, Any] | None = None) -> Any:
        """Parse *stream* as a multipart form.

        Args:
            stream: The request body.
            media_type: The request's media type.
            parser_context: DRF's parser context.

        Returns:
            The fields and files.

        Raises:
            ParseError: A field name or value holds a NUL.
        """
        parsed = super().parse(stream, media_type, parser_context)
        if holds_nul(list(parsed.data.lists())):
            raise ParseError(NUL_REFUSAL)
        return parsed


class JSONParser(parsers.JSONParser):
    """DRF's JSON parser, answering a body nested past the recursion limit with a 400 rather than a 500."""

    def parse(self, stream: IO[bytes], media_type: str | None = None, parser_context: Mapping[str, Any] | None = None) -> Any:
        """Parse *stream* as JSON.

        Args:
            stream: The request body.
            media_type: The request's media type.
            parser_context: DRF's parser context.

        Returns:
            The decoded JSON value.

        Raises:
            ParseError: The body is not JSON, nests too deeply to decode, or holds a NUL in a string.
        """
        try:
            value = super().parse(stream, media_type, parser_context)
        except RecursionError as exc:
            raise ParseError("JSON parse error - the body is nested too deeply.") from exc
        if holds_nul(value):
            raise ParseError(NUL_REFUSAL)
        return value
