"""A view wired to several routes must accept every parameter any of them supplies.

Django resolves a handler's arguments at *request* time, so a view class wired
to two routes with different parameters fails only when someone actually
requests the mismatched one - as a ``TypeError`` that surfaces as a 500 with
nothing in the signature to suggest it. Nothing at import time, in review, or in
`makemigrations` notices.

This audit met the shape three times in three chunks:

- ``saved_filters.new`` - ``SavedFilterEditView.post`` required ``filter_uuid``,
  which only the *edit* route supplies. Every POST to ``new/`` was a 500 (chunk 552).
- ``pin.link.to`` - ``PinRelinkView.get`` omitted ``location_slug`` entirely,
  which its own ``post`` declares. Every GET was a 500 (chunk 556).
- ``pin.link`` - the same view's POST half, whose remaining failure is the filed
  detach-location product decision rather than a signature problem.

All three were found by requesting routes nobody requests. This checks the
property directly instead: for every view class wired to two or more routes, each
handler must accept the union of the parameters those routes can pass.

**Verified to bind**: restoring ``PinRelinkView.get``'s pre-fix signature makes
this report exactly that method and parameter. A guard nobody has watched fail
is a guard nobody knows works.

A handler taking ``**kwargs`` is skipped - it accepts anything by construction,
which is a legitimate way to serve several routes.
"""

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

    Parameters accumulate down the resolver tree, because a route nested under
    ``path("<str:label_kind>/", include(...))`` receives that parameter too -
    reading only the leaf pattern was what made an earlier sweep in this
    codebase miss most parameterised routes (see ``test_route_query_scaling``).
    """
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


def _signature_mismatches() -> list[str]:
    """Every handler that cannot accept a parameter one of its routes supplies."""
    problems: list[str] = []
    for view_class, entries in _multi_route_views().items():
        supplied = frozenset().union(*(params for _name, params in entries))
        for handler_name in _HANDLERS:
            handler = getattr(view_class, handler_name, None)
            if handler is None or not callable(handler):
                continue
            try:
                signature = inspect.signature(handler)
            except (TypeError, ValueError):
                continue
            if any(param.kind == param.VAR_KEYWORD for param in signature.parameters.values()):
                continue  # **kwargs accepts anything, which is a valid way to serve several routes
            missing = supplied - (set(signature.parameters) - _NON_ROUTE_ARGS)
            if missing:
                routes = sorted(name or "<unnamed>" for name, _ in entries)
                problems.append(f"{view_class.__module__}.{view_class.__qualname__}.{handler_name}() cannot accept {sorted(missing)} (routes: {routes})")
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

    def test_the_scan_reads_inherited_parameters(self) -> None:
        """Parameters from an enclosing include() must be counted, not just the leaf's.

        Reading only leaf patterns is the specific mistake that made an earlier
        sweep here blind to most parameterised routes.
        """
        with_inherited = [params for _name, params, _cb in _routes() if len(params) > 1]

        self.assertGreater(len(with_inherited), 20, "no routes appear to inherit a parameter - the resolver walk is not descending")
