"""Helper for embedding JSON directly inside an already-open `<script>` block.

Django's `{% json_script %}` template filter is the standard safe way to embed
JSON in a template, but it always wraps the value in its own `<script
type="application/json">` tag. Several views build a single larger `<script>`
block containing multiple JS object/array literals (e.g. a pin's `tags_data`
inline inside a bigger per-pin JS object) and interpolate JSON into it via
`{{ ... |safe }}` - `json_script` doesn't fit there, so this replicates just
its escaping.
"""
from __future__ import annotations

import json
from typing import Any

# Same escapes Django's {% json_script %} applies: neutralizes `</script>` and HTML
# entity injection when a JSON payload (e.g. user-owned badge/tag names) is embedded
# directly inside an already-open <script> block via `{{ ... |safe }}`, rather than
# through json_script's own <script type="application/json"> wrapper.
_JSON_SCRIPT_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def safe_json_for_script(value: Any) -> str:
    """Serialize a value to JSON that is safe to embed inline inside a `<script>` block.

    Args:
        value: The JSON-serializable value (e.g. a list of dicts of badge data).

    Returns:
        A JSON string with `<`, `>`, and `&` escaped so it cannot break out of the
        enclosing `<script>` tag or inject HTML, even when rendered with `|safe`.
    """
    return json.dumps(value).translate(_JSON_SCRIPT_ESCAPES)
