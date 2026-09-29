"""Reading a posted body as a mapping of fields, however the client sent it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.core.exceptions import BadRequest

if TYPE_CHECKING:
    from django.http import HttpRequest

_FORM_TYPES = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})


def posted_json_object(request: HttpRequest) -> dict[str, Any]:
    """The request's JSON body, which must be an object.

    Args:
        request: The incoming request.

    Returns:
        The decoded object; ``{}`` for a form post or an empty body.

    Raises:
        BadRequest: The body is not JSON, or is JSON that is not an object. Django answers it with a 400.
    """
    if request.content_type in _FORM_TYPES or not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (ValueError, RecursionError) as exc:
        raise BadRequest("The request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise BadRequest("The request body must be a JSON object.")
    return data


def posted_fields(request: HttpRequest) -> dict[str, Any]:
    """A form post's fields, one value each, or the JSON object body.

    Args:
        request: The incoming request.

    Returns:
        The fields; ``{}`` when there are none to read.

    Raises:
        BadRequest: A JSON body that is malformed or not an object.
    """
    if request.content_type in _FORM_TYPES:
        return request.POST.dict()
    return posted_json_object(request)
