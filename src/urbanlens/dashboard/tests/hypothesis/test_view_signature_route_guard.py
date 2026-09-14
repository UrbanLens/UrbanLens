"""A view wired to several routes must accept every parameter any of them supplies."""

from __future__ import annotations

import collections
import inspect

from django.urls import get_resolver
from django.urls.resolvers import URLResolver

from urbanlens.core.tests.testcase import SimpleTestCase

#: Handler names Django dispatches to by HTTP method.
_HANDLERS = ("get", "post", "put", "patch", "delete")

#: Never route parameters - they are the framework's own.
_NON_ROUTE_ARGS = {"self", "request", "args", "kwargs"}


def _routes() -> list[tuple[str | None, frozenset[str], object]]:
    """Every URL pattern as ``(name, parameters, callback)``.

    Parameters accumulate down the resolver tree, because a route nested under ``path("<str:label_kind>/",
    include(...))`` receives that parameter too - reading only the leaf pattern was what made an earlier sweep
    in this codebase miss most parameterised routes (see ``test_route_query_scaling``)."""
    found: list[tuple[str | None, frozenset[str], object]] = []

    def walk(resolver, inherited: frozenset[str]) -> None:
        for pattern in resolver.url_patterns:
            params = inherited | frozenset(pattern.pattern.regex.groupindex)
            if isinstance(pattern, URLResolver):
                walk(pattern, params)
            else:
                found.append((pattern.name, params, pattern.callback))

    walk(get_resolver(), frozenset())
    return found


def _multi_route_views() -> dict[type, list[tuple[str | None, frozenset[str]]]]:
    """View classes wired to more than one route, with each route's parameters."""
    by_view: dict[type, list[tuple[str | None, frozenset[str]]]] = collections.defaultdict(list)
    for name, params, callback in _routes():
        view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if view_class is not None:
            by_view[view_class].append((name, params))
    return {cls: entries for cls, entries in by_view.items() if len(entries) > 1}


def _unacceptable(func: object, supplied: frozenset[str]) -> set[str]:
    """Which of `supplied` this callable cannot accept as keyword arguments."""
    try:
        signature = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return set()
    if any(param.kind == param.VAR_KEYWORD for param in signature.parameters.values()):
        return set()  # **kwargs accepts anything, which is a valid way to serve several routes
    return set(supplied) - (set(signature.parameters) - _NON_ROUTE_ARGS)


def _signature_mismatches() -> list[str]:
    """Every handler that cannot accept a parameter one of its routes supplies.

    Checks *every* parameterised route, not only those on views wired to two or more."""
    problems: list[str] = []
    by_view: dict[type, list[tuple[str | None, frozenset[str]]]] = collections.defaultdict(list)

    for name, params, callback in _routes():
        if not params:
            continue
        view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if view_class is None:
            # A function view or DRF action: the callback itself takes the parameters.
            missing = _unacceptable(callback, params)
            if missing:
                problems.append(
                    f"{getattr(callback, '__module__', '?')}.{getattr(callback, '__qualname__', callback)} cannot accept {sorted(missing)} (route {name or '<unnamed>'})"
                )
            continue
        by_view[view_class].append((name, params))

    for view_class, entries in by_view.items():
        # The union, so a view on several routes must satisfy all of them - the
        # original property - while a view on one still has to satisfy that one.
        supplied = frozenset().union(*(params for _name, params in entries))
        for handler_name in _HANDLERS:
            handler = getattr(view_class, handler_name, None)
            if handler is None or not callable(handler):
                continue
            missing = _unacceptable(handler, supplied)
            if missing:
                routes = sorted(name or "<unnamed>" for name, _ in entries)
                problems.append(
                    f"{view_class.__module__}.{view_class.__qualname__}.{handler_name}() cannot accept {sorted(missing)} (routes: {routes})"
                )
    return sorted(problems)


class ViewSignatureRouteGuardTests(SimpleTestCase):
    def test_every_handler_accepts_every_parameter_its_routes_supply(self) -> None:
        mismatches = _signature_mismatches()

        self.assertEqual(
            mismatches,
            [],
            "these handlers raise TypeError on one of their own routes:\n" + "\n".join(mismatches),
        )

    # -- guard the guard ----------------------------------------------------

    def test_the_scan_still_finds_multi_route_views(self) -> None:
        """A resolver-walk refactor that matched nothing would pass vacuously."""
        self.assertGreater(len(_multi_route_views()), 20, "multi-route view discovery found suspiciously few classes")

    def test_the_scan_examines_single_route_views_too(self) -> None:
        """The widened scope must actually be exercising handlers, not just present.

        Counted the same way the check itself counts, so a refactor that stopped resolving view classes - and
        would then report zero problems forever - fails here instead of passing silently."""
        checked = sum(
            1
            for _name, params, callback in _routes()
            if params
            for view_class in [getattr(callback, "view_class", None) or getattr(callback, "cls", None)]
            if view_class is not None
            for handler_name in _HANDLERS
            if callable(getattr(view_class, handler_name, None))
        )

        self.assertGreater(checked, 200, "the signature check is barely examining anything")

    def test_the_scan_reads_inherited_parameters(self) -> None:
        """Parameters from an enclosing include() must be counted, not just the leaf's.

        Reading only leaf patterns is the specific mistake that made an earlier
        sweep here blind to most parameterised routes.
        """
        with_inherited = [params for _name, params, _cb in _routes() if len(params) > 1]

        self.assertGreater(
            len(with_inherited), 20, "no routes appear to inherit a parameter - the resolver walk is not descending"
        )
