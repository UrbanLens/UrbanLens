"""Helper for embedding JSON directly inside an already-open `<script>` block."""

from __future__ import annotations

import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder

# Same escapes Django's {% json_script %} applies: neutralizes `</script>` and HTML entity injection
# when a JSON payload (e.g. user-owned label/tag names) is embedded directly inside an already-open
# <script> block via `{{ ... |safe }}`, rather than through json_script's own <script
# type="application/json"> wrapper.
_JSON_SCRIPT_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def safe_json_for_script(value: Any) -> str:
    """Serialize a value to JSON that is safe to embed inline inside a `<script>` block.

    Args:
        value: The JSON-serializable value (e.g. a list of dicts of label data).

    Returns:
        A JSON string with `<`, `>`, and `&` escaped so it cannot break out of the enclosing `<script>` tag or inject HTML, even when rendered with `|safe`."""
    return json.dumps(value, cls=DjangoJSONEncoder).translate(_JSON_SCRIPT_ESCAPES)
