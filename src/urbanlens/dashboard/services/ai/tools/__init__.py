"""The assistant's typed tool registry.

Importing this package populates :data:`registry.REGISTRY` as a side effect:
every tool module below calls ``register(...)`` at import time. A module not
imported here is a tool that silently doesn't exist - there is no other
discovery mechanism, so a new tool module belongs in this list.
"""

from __future__ import annotations

from urbanlens.dashboard.services.ai.tools import dismissals as _dismissals, help as _help, pins, places as _places, routing as _routing, trips, undo as _undo, visits as _visits, weather as _weather
from urbanlens.dashboard.services.ai.tools.registry import (
    MAX_TOOL_CALLS,
    MAX_TOOL_RESULT_CHARS,
    REGISTRY,
    DataScope,
    ToolContext,
    ToolResult,
    ToolSpec,
    available_tools,
    execute,
    register,
)

__all__ = [
    "MAX_TOOL_CALLS",
    "MAX_TOOL_RESULT_CHARS",
    "REGISTRY",
    "DataScope",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "available_tools",
    "execute",
    "register",
]
