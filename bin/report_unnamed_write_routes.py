#!/usr/bin/env python3
"""List the URL routes that accept a write but that no test names.

A route accepts a write when a DRF viewset's ``actions``, a class-based view's handlers (filtered by
``http_method_names``) or a DRF ``APIView``'s handlers include ``post``/``put``/``patch``/``delete``. A route is
named when its full, namespace-joined name appears as a quoted string literal in any test file. Plain function views
are not classified; their count is reported separately.

Needs the URLconf, so it runs where GeoDjango imports, inside the test runner after a sync::

    docker exec -w /app urbanlens_development_main_test_runner /app/.venv/bin/python bin/report_unnamed_write_routes.py
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import pathlib
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

#: Third-party namespaces, not the project's routes.
_EXCLUDED_NAMESPACES = frozenset({"admin", "oauth2_provider", "social"})

_WRITE_METHODS = frozenset({"post", "put", "patch", "delete"})

#: Where tests live, relative to the repository root.
_TEST_GLOBS = ("src/**/tests/**/*.py", "tests/**/*.py")


@dataclass(frozen=True)
class Route:
    """One named route and the methods its view answers.

    Attributes:
        name: The namespace-joined route name, as ``reverse`` takes it.
        path: The route's pattern, joined from every enclosing resolver.
        methods: Lower-case HTTP methods the view handles, or ``None`` for a plain function view.
    """

    name: str
    path: str
    methods: frozenset[str] | None

    @property
    def accepts_write(self) -> bool:
        """Whether any handled method can change state."""
        return bool(self.methods and self.methods & _WRITE_METHODS)


def _view_methods(callback: Callable[..., Any]) -> frozenset[str] | None:
    """The methods *callback* dispatches, read from what ``as_view`` attached to it.

    Args:
        callback: A resolved URL pattern's view.

    Returns:
        Lower-case method names, or ``None`` when the view is a plain function and cannot be read.
    """
    actions = getattr(callback, "actions", None)
    if isinstance(actions, dict):
        return frozenset(method.lower() for method in actions)
    view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    if view_class is None:
        return None
    initkwargs = getattr(callback, "view_initkwargs", None) or getattr(callback, "initkwargs", None) or {}
    allowed = initkwargs.get("http_method_names", view_class.http_method_names)
    return frozenset(method for method in allowed if hasattr(view_class, method))


def _walk(patterns: Iterable[Any], namespace: str, prefix: str) -> Iterator[Route]:
    """Every named route under *patterns*.

    Args:
        patterns: URL patterns and resolvers, as ``urlpatterns`` holds them.
        namespace: The joined namespace of the enclosing resolvers, with a trailing colon, or empty.
        prefix: The joined pattern of the enclosing resolvers.

    Yields:
        One route per named pattern.
    """
    from django.urls import URLResolver

    for entry in patterns:
        route = f"{prefix}{entry.pattern}"
        if isinstance(entry, URLResolver):
            if entry.namespace in _EXCLUDED_NAMESPACES:
                continue
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _walk(entry.url_patterns, inner, route)
        elif entry.name:
            yield Route(name=f"{namespace}{entry.name}", path=route, methods=_view_methods(entry.callback))


def discover_routes() -> list[Route]:
    """Every named project route, one per name.

    Returns:
        The routes, sorted by name. A name bound twice keeps the first binding, as ``reverse`` does.
    """
    from django.urls import get_resolver

    seen: dict[str, Route] = {}
    for route in _walk(get_resolver().url_patterns, "", ""):
        seen.setdefault(route.name, route)
    return sorted(seen.values(), key=lambda route: route.name)


def read_test_sources(root: pathlib.Path) -> str:
    """The concatenated text of every test file under *root*.

    Args:
        root: The repository root.

    Returns:
        All test source, joined by newlines.
    """
    paths = sorted({path for pattern in _TEST_GLOBS for path in root.glob(pattern)})
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)


def is_named(name: str, sources: str) -> bool:
    """Whether *name* appears as a quoted string literal in *sources*.

    Args:
        name: A namespace-joined route name.
        sources: Test source text.

    Returns:
        True when ``"name"`` or ``'name'`` occurs.
    """
    return f'"{name}"' in sources or f"'{name}'" in sources


def main(argv: list[str] | None = None) -> int:
    """Print the unnamed write routes and the totals.

    Args:
        argv: Command-line arguments; ``sys.argv`` when omitted.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd(), help="repository root to scan for tests")
    parser.add_argument("--json", action="store_true", help="print the unnamed write routes as JSON")
    args = parser.parse_args(argv)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings.test")
    import django

    django.setup()

    routes = discover_routes()
    sources = read_test_sources(args.root)
    unnamed = [route for route in routes if not is_named(route.name, sources)]
    unnamed_writes = [route for route in unnamed if route.accepts_write]
    if args.json:
        payload = [{"name": route.name, "path": route.path, "methods": sorted(route.methods or ())} for route in unnamed_writes]
        print(json.dumps(payload, indent=2))
        return 0
    for route in unnamed_writes:
        print(f"{route.name:60} {','.join(sorted(route.methods or ())):24} /{route.path}")
    writes = [route for route in routes if route.accepts_write]
    print()
    print(f"project routes:                 {len(routes)}")
    print(f"accept a write:                 {len(writes)}")
    print(f"named by no test:               {len(unnamed)}")
    print(f"...of which accept a write:     {len(unnamed_writes)}")
    print(f"function views (unclassified):  {sum(route.methods is None for route in routes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
