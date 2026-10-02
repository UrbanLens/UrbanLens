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


class MalformedBodyError(BadRequest, ValueError):
    """A body that is not a JSON object. Django answers it with a 400.

    A ``ValueError`` as well, so a view that already catches one for undecodable JSON answers this the same way.
    """


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
    try:
        data = json.loads(request.body)
    except (ValueError, RecursionError) as exc:
        raise MalformedBodyError("The request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise MalformedBodyError("The request body must be a JSON object.")
    return data


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
            ParseError: The body is not JSON, or nests too deeply to decode.
        """
        try:
            return super().parse(stream, media_type, parser_context)
        except RecursionError as exc:
            raise ParseError("JSON parse error - the body is nested too deeply.") from exc
