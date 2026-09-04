"""Regression tests for external-API URL routing.

The external API's routes are now contributed by fourteen separate modules
(``external_api/urls.py`` plus thirteen ``urls_*.py`` domain modules) that are
concatenated into one flat list. Django resolves a request by walking that list
and taking the first pattern that matches, which makes the assembled urlconf
unsafe by construction: a generic route contributed by one module can swallow a
literal route contributed by another, and neither author is in a position to
notice.

The failure is silent and actively misleading. ``pins/deleted/`` sitting behind
``pins/<str:pin_slug>/`` does not answer "no such route" - it dispatches to the
pin-detail view, which looks for a pin slugged "deleted", finds none, and
returns 404. From outside, that is indistinguishable from a genuinely missing
pin, so the endpoint simply appears not to work and no amount of staring at the
view explains why.

``urls.order_by_specificity`` is the structural fix: it recomputes the ordering
from the routes themselves rather than trusting declaration order. These tests
are what keeps that fix honest. The central one reverses every registered route
and resolves the resulting URL back, asserting it lands on the route it came
from - which is precisely the assertion a shadowed pattern fails. It reads the
route table out of the live urlconf, so endpoints added later are covered
without anybody remembering to extend this file.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from django.http import HttpRequest, HttpResponse
from django.urls import NoReverseMatch, Resolver404, path, resolve, reverse
from django.urls.resolvers import RegexPattern, URLResolver
from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.external_api import urls as external_api_urls
from urbanlens.dashboard.external_api.urls import order_by_specificity

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Explains the invariant in the words of whoever is about to read a failure.
#: Route-shadowing failures are otherwise extremely hard to interpret - the test
#: reports "wrong view name" and the reader has no reason to suspect ordering.
_ORDERING_RULE: Final = (
    "The external API's urlpatterns are concatenated from urls.py plus the urls_*.py domain modules and re-sorted by urls.order_by_specificity, which puts narrow segments (literals, then int/uuid, then slug, then str, then path) "
    "ahead of broad ones. When a route reverses to a URL that resolves to a *different* route, that ordering has broken down: some other pattern of the same shape is claiming this route's URLs. In production it does not look like a "
    "routing bug - the wrong view runs, fails to find the object it was asked for, and answers 404. Fix the collision rather than reordering by hand: two routes of identical shape (e.g. 'pins/<str:a>/' and 'pins/<str:b>/') are genuinely "
    "ambiguous and no ordering can save them."
)

#: One concrete value per path converter, used both to reverse every registered
#: route and to build the concrete URL that is then resolved back.
#:
#: These are chosen as *witnesses* that separate the converters, which is what
#: makes the round-trip meaningful. The str sample contains dots so it is not
#: also a valid slug; the slug sample contains letters and hyphens so it is
#: neither an int nor a uuid; the int sample is digits only. If they overlapped,
#: a mis-ordered pair of converters would still round-trip and the test would
#: pass through a real bug.
_SAMPLE_ARGUMENTS: Final[dict[str, str | int]] = {
    "int": 424242,
    "uuid": "00000000-0000-4000-8000-0000000c0ffe",
    "slug": "zz-sample-zz",
    "str": "zz.sample.zz",
    "path": "zz.sample.zz/zz.sample.zz",
}

#: Parses ``<converter:name>`` (and bare ``<name>``) captures out of a route.
#:
#: Deliberately a second copy of the grammar ``urls._ROUTE_PARAMETER_RE`` uses,
#: rather than an import of it. This test exists to catch the sorter getting the
#: ordering wrong; if it borrowed the sorter's own parser, a parsing bug in the
#: sorter would produce matching wrong answers on both sides and hide itself.
_PARAMETER_RE: Final = re.compile(r"<(?:([^>:]+):)?([^>]+)>")


def _view(request: HttpRequest) -> HttpResponse:
    """Stand-in view for synthetic urlconfs. Routing never calls it.

    Args:
        request: Unused - present only to satisfy the view signature.

    Returns:
        An empty response, never actually produced by these tests.
    """
    return HttpResponse()


def _route_parameters(route: str) -> list[tuple[str, str]]:
    """Extract a route's captures as ``(converter_name, parameter_name)`` pairs.

    Args:
        route: A route string as passed to ``path()``, e.g.
            ``"pins/<str:pin_slug>/notes/<int:note_id>/"``.

    Returns:
        One pair per capture, in path order. A capture written without an
        explicit converter (``<pk>``) reports ``"str"``, matching Django's own
        default.
    """
    return [(converter or "str", name) for converter, name in _PARAMETER_RE.findall(route)]


def _registered_routes() -> list[tuple[str, str, dict[str, str | int]]]:
    """Read every external-API route straight out of the live urlconf.

    Reading the urlconf instead of hard-coding a list is what makes these tests
    cover routes that do not exist yet: the fifteen agents adding endpoints to
    the ``urls_*.py`` domain modules get the shadowing check for free, without
    having to know this file exists.

    Returns:
        ``(url_name, route, reverse_kwargs)`` for each registered pattern, with
        the kwargs populated from :data:`_SAMPLE_ARGUMENTS`.
    """
    routes: list[tuple[str, str, dict[str, str | int]]] = []
    for entry in external_api_urls.urlpatterns:
        route = str(entry.pattern)
        kwargs = {
            name: _SAMPLE_ARGUMENTS.get(converter, _SAMPLE_ARGUMENTS["str"])
            for converter, name in _route_parameters(route)
        }
        routes.append((entry.name, route, kwargs))
    return routes


class ExternalApiRouteTableTests(SimpleTestCase):
    """Structural invariants of the assembled route table itself."""

    def test_the_route_table_is_not_empty(self) -> None:
        """Guards the rest of this file: a broken import would make every other test vacuous."""
        self.assertGreater(
            len(_registered_routes()),
            100,
            "The external API urlconf came back nearly empty - the domain modules are probably not being concatenated at all, which would make every other assertion here pass trivially.",
        )

    def test_every_route_is_named(self) -> None:
        """An unnamed route cannot be reversed, so no client and no test can reach it."""
        unnamed = [route for name, route, _kwargs in _registered_routes() if not name]
        self.assertEqual(
            unnamed,
            [],
            "Every external-API route needs name=; reverse('external_api:...') is the only supported way to build these URLs, and an unnamed route is also invisible to the shadowing check below.",
        )

    def test_route_names_are_unique(self) -> None:
        """Two routes sharing a name make reverse() silently pick one of them.

        Names are global across the flat ``external_api:`` namespace, so two
        domain modules can collide without either author seeing the other's
        file. Django does not complain - ``reverse()`` just starts returning the
        other module's URL, and the endpoint that "stopped working" is the one
        that never changed.
        """
        names = [name for name, _route, _kwargs in _registered_routes()]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(
            duplicates,
            [],
            "Duplicate external-API route names. Names are shared across every urls_*.py module because they all land in the flat 'external_api:' namespace - prefix yours with your domain.",
        )

    def test_every_converter_in_use_has_a_sample_value(self) -> None:
        """A converter with no witness value would silently weaken the shadowing check.

        ``_registered_routes`` falls back to the ``str`` sample for unknown
        converters so the suite still runs, but that fallback can produce a
        value the converter rejects - which turns a real ordering failure into
        an unexplained Resolver404, or worse, hides one.
        """
        used = {
            converter
            for _name, route, _kwargs in _registered_routes()
            for converter, _parameter in _route_parameters(route)
        }
        missing = sorted(used - set(_SAMPLE_ARGUMENTS))
        self.assertEqual(
            missing,
            [],
            f"These path converters are used by external-API routes but have no entry in _SAMPLE_ARGUMENTS: {missing}. Add one whose value is accepted by that converter and rejected by every broader one.",
        )

    def test_urlpatterns_are_ordered_by_specificity(self) -> None:
        """The published list must be the sorted one, not a hand-ordered list that happens to work.

        If someone assigns ``urlpatterns`` without running it through
        ``order_by_specificity`` the table may still resolve correctly today,
        purely by luck of declaration order, and then break the first time a
        domain module adds a literal. This asserts the guarantee is mechanical.
        """
        patterns = list(external_api_urls.urlpatterns)
        self.assertEqual(
            [str(entry.pattern) for entry in order_by_specificity(patterns)],
            [str(entry.pattern) for entry in patterns],
            "external_api.urls.urlpatterns is not in specificity order - it must be assigned as order_by_specificity(...) over the concatenated modules.",
        )

    def test_every_entry_is_a_flat_path_route(self) -> None:
        """``include()`` here would break the flat namespace the whole API depends on.

        ``reverse("external_api:pins.detail")`` is used throughout the codebase
        and ``schema.preprocess_external_api_only`` selects endpoints by URL
        prefix; an included sub-namespace breaks the first outright and quietly
        reshapes the second.
        """
        resolvers = [str(entry.pattern) for entry in external_api_urls.urlpatterns if isinstance(entry, URLResolver)]
        self.assertEqual(
            resolvers,
            [],
            "External-API domain modules must be concatenated into urls.py's list, never include()d - see the ordering rule in external_api/urls.py.",
        )


class ExternalApiUrlResolutionTests(SimpleTestCase):
    """The round-trip: every route's own URL must resolve back to that route."""

    def test_every_route_reverses(self) -> None:
        """A registered route that cannot be reversed is unreachable from client code.

        In practice this fails when a route is declared with capture names its
        callers cannot supply, or when the flat namespace has been broken by an
        ``include()`` - both of which turn ``reverse()`` into a NoReverseMatch
        naming a route that visibly exists in the urlconf.
        """
        for name, route, kwargs in _registered_routes():
            with self.subTest(name=name, route=route):
                try:
                    url = reverse(f"external_api:{name}", kwargs=kwargs)
                except (
                    NoReverseMatch
                ) as error:  # pragma: no cover - only reachable when a route is genuinely unreversible
                    self.fail(
                        f"external_api:{name} ({route}) is registered but will not reverse with {kwargs!r}: {error}. Every external route must live in the flat 'external_api:' namespace - see external_api/urls.py."
                    )
                self.assertTrue(
                    url.startswith("/dashboard/api/external/v1/"),
                    f"external_api:{name} reversed to {url!r}, which is outside the external API mount. The namespace must stay flat - see the ordering rule in external_api/urls.py.",
                )

    def test_every_route_resolves_back_to_itself(self) -> None:
        """The shadowing check. Build each route's own URL, then ask who answers it.

        Any pattern that claims another route's URLs shows up here as a name
        mismatch, no matter which module declared it.
        """
        for name, route, kwargs in _registered_routes():
            with self.subTest(name=name, route=route):
                url = reverse(f"external_api:{name}", kwargs=kwargs)
                try:
                    match = resolve(url)
                except Resolver404:  # pragma: no cover - only reachable when a route is genuinely unroutable
                    self.fail(
                        f"external_api:{name} ({route}) reverses to {url!r}, which resolves to nothing at all. {_ORDERING_RULE}"
                    )
                self.assertEqual(
                    match.view_name,
                    f"external_api:{name}",
                    f"external_api:{name} ({route}) reverses to {url!r}, but that URL is answered by {match.view_name!r} instead. {_ORDERING_RULE}",
                )

    def test_the_canonical_shadowing_case_still_resolves(self) -> None:
        """``pins/deleted/`` is the route this whole mechanism was built around.

        It is covered by the data-driven test above, but named explicitly here
        so the next person to break it gets a failure that says what broke.
        """
        match = resolve(reverse("external_api:pins.deleted"))
        self.assertEqual(
            match.view_name,
            "external_api:pins.deleted",
            f"'pins/deleted/' is being swallowed by the generic pin-detail route again. {_ORDERING_RULE}",
        )


class SpecificityOrderingTests(SimpleTestCase):
    """``order_by_specificity`` on hand-built urlconfs, including the hazard it fixes."""

    @staticmethod
    def _resolver(patterns: list[URLPattern]) -> URLResolver:
        """Wrap a bare pattern list in a resolver rooted at ``/``.

        Args:
            patterns: Patterns to resolve against, in the order given.

        Returns:
            A resolver that accepts absolute paths, like the project's root one.
        """
        return URLResolver(RegexPattern(r"^/"), patterns)

    def test_declaration_order_alone_really_does_shadow_a_literal(self) -> None:
        """Prove the hazard is real before asserting the fix works on it.

        Without this, the next test only shows that a sorted list resolves
        correctly - which would also be true if the ordering did nothing.
        """
        shadowed = [
            path("pins/<str:pin_slug>/", _view, name="pins.detail"),
            path("pins/deleted/", _view, name="pins.deleted"),
        ]
        match = self._resolver(shadowed).resolve("/pins/deleted/")
        self.assertEqual(match.url_name, "pins.detail")
        # And note what the caller sees: not a 404 from the router, but the
        # detail view hunting for a pin whose slug is "deleted".
        self.assertEqual(match.kwargs, {"pin_slug": "deleted"})

    def test_order_by_specificity_rescues_the_shadowed_literal(self) -> None:
        """The same two routes, sorted, dispatch the literal to its own view."""
        ordered = order_by_specificity(
            [
                path("pins/<str:pin_slug>/", _view, name="pins.detail"),
                path("pins/deleted/", _view, name="pins.deleted"),
            ]
        )
        resolver = self._resolver(ordered)
        self.assertEqual(resolver.resolve("/pins/deleted/").url_name, "pins.deleted")
        # The generic route must still work for everything that is not a literal.
        self.assertEqual(resolver.resolve("/pins/old-mill/").url_name, "pins.detail")

    def test_narrow_converters_sort_ahead_of_broad_ones(self) -> None:
        """int/uuid before slug before str: each accepts a subset of the next."""
        ordered = order_by_specificity(
            [
                path("things/<str:anything>/", _view, name="any"),
                path("things/<slug:sluggish>/", _view, name="slug"),
                path("things/<int:number>/", _view, name="number"),
            ]
        )
        self.assertEqual([entry.name for entry in ordered], ["number", "slug", "any"])

    def test_ordering_is_stable_for_equally_specific_routes(self) -> None:
        """Identically shaped routes keep their declared order rather than being reshuffled.

        The sort must not invent a winner between two genuinely ambiguous
        routes; that is a conflict for the author to resolve, and silently
        picking one would make it harder to see.
        """
        declared = [
            path("a/<str:first>/", _view, name="first"),
            path("a/<str:second>/", _view, name="second"),
        ]
        self.assertEqual([entry.name for entry in order_by_specificity(declared)], ["first", "second"])

    def test_include_is_rejected(self) -> None:
        """An included sub-namespace cannot be ordered, and breaks reverse() besides."""
        with self.assertRaises(TypeError):
            order_by_specificity([path("nested/", ([path("x/", _view, name="x")], "nested", "nested"))])


#: Segments a synthetic route may be built from: a literal drawn from a tiny
#: alphabet (so collisions between generated routes are likely rather than
#: rare - the point is to stress the ordering, not to avoid it) or a converter.
_SEGMENT = st.one_of(
    st.sampled_from(["alpha", "beta", "gamma"]).map(lambda literal: ("literal", literal)),
    st.sampled_from(["int", "uuid", "slug", "str"]).map(lambda converter: ("converter", converter)),
)
_SHAPES = st.lists(st.lists(_SEGMENT, min_size=1, max_size=3), min_size=1, max_size=6)


def _render_route(shape: list[tuple[str, str]]) -> str:
    """Turn a generated shape into a ``path()`` route string.

    Args:
        shape: ``(kind, value)`` segments, where kind is ``"literal"`` or
            ``"converter"``.

    Returns:
        A route with a trailing slash, capture names derived from position so
        they are unique within the route.
    """
    return (
        "/".join(
            value if kind == "literal" else f"<{value}:p{position}>" for position, (kind, value) in enumerate(shape)
        )
        + "/"
    )


def _render_sample_path(shape: list[tuple[str, str]]) -> str:
    """Build the concrete URL a shape's own route should claim.

    Because every converter's sample value is rejected by every other converter
    (and by every literal in the alphabet), two different shapes can never
    produce the same sample path - so a mismatch below is always an ordering
    failure and never an unavoidable collision.

    Args:
        shape: The same ``(kind, value)`` segments passed to :func:`_render_route`.

    Returns:
        An absolute path with a trailing slash.
    """
    return "/" + "/".join(value if kind == "literal" else str(_SAMPLE_ARGUMENTS[value]) for kind, value in shape) + "/"


class SpecificityOrderingPropertyTests(SimpleTestCase):
    """Property: for any set of routes, sorting makes each one claim its own URLs.

    The hand-written cases above cover the collisions we already know about.
    This covers the ones the domain modules have not invented yet - which is the
    whole risk being managed, since thirteen modules are about to add routes
    nobody has reviewed together.
    """

    @hyp_settings(max_examples=100, deadline=None)
    @given(_SHAPES)
    def test_every_route_claims_its_own_url_after_ordering(self, shapes: list[list[tuple[str, str]]]) -> None:
        """Sort an arbitrary route set, then round-trip every route through it.

        Args:
            shapes: Generated route shapes; duplicates are dropped because two
                identical routes are ambiguous by definition and no ordering
                can decide between them.
        """
        unique = list(dict.fromkeys(tuple(shape) for shape in shapes))
        patterns = [path(_render_route(list(shape)), _view, name=f"route{index}") for index, shape in enumerate(unique)]
        resolver = URLResolver(RegexPattern(r"^/"), order_by_specificity(patterns))

        for index, shape in enumerate(unique):
            sample = _render_sample_path(list(shape))
            match = resolver.resolve(sample)
            self.assertEqual(
                match.url_name,
                f"route{index}",
                f"{_render_route(list(shape))!r} was shadowed: its own URL {sample!r} resolved to {match.url_name!r}. {_ORDERING_RULE}",
            )
