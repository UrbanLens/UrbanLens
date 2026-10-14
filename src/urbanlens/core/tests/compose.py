"""The deployment's compose files, read as ``docker compose -f docker-compose.yml -f docker-compose.<name>.yml`` merges them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: The repo root - parents[4] from src/urbanlens/core/tests/compose.py.
_REPO_ROOT = Path(__file__).resolve().parents[4]
#: Compose files that adjust a developer's checkout rather than describe a deployment.
_DEVELOPMENT_OVERLAYS = ("docker-compose.hot-reload.yml",)


def _normalise_networks(service: dict[str, Any]) -> dict[str, Any]:
    """Compose accepts a service's networks as a list or a mapping; compare them as a mapping."""
    if "networks" in service and isinstance(service["networks"], list):
        service["networks"] = dict.fromkeys(service["networks"])
    return service


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge one service entry the way ``docker compose -f a -f b`` does: mappings merge key by key."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def compose_document() -> dict[str, Any]:
    """
    The base compose file with every deployment overlay ``docker-compose.<name>.yml`` beside it merged in, as ``docker compose -f ... -f ...`` reads them.

    A tree that carries no such file yields the base file alone.
    """
    base = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = {name: _normalise_networks(dict(service)) for name, service in base["services"].items()}
    anchors: dict[str, Any] = {}
    networks = dict(base.get("networks", {}))
    for path in sorted(_REPO_ROOT.glob("docker-compose.*.yml")):
        if path.name in _DEVELOPMENT_OVERLAYS:
            continue
        extra = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, override in extra.get("services", {}).items():
            services[name] = _merge(services.get(name, {}), _normalise_networks(dict(override)))
        anchors.update({key: value for key, value in extra.items() if key.startswith("x-")})
        networks.update(extra.get("networks", {}))
    return {**anchors, **base, "services": services, "networks": networks}
