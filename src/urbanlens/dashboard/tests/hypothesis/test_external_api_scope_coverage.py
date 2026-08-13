"""Every external API endpoint must enforce a scope, or be a named exception.

``HasApiKeyScope`` fails closed per method, so an endpoint that gains a new HTTP
method without declaring its scope becomes a dead 403 rather than an open door.
That covers the common mistake. It does not cover the dangerous one: dropping the
permission entirely, or inheriting ``UnscopedExternalApiView``, which
authenticates the credential and then checks nothing. Its own docstring warns
about this - "Do not use this as a shortcut for an endpoint that touches user
data - such an endpoint needs a scope, and inheriting from here would silently
grant it to every credential" - and nothing was enforcing it.

An unscoped endpoint is reachable by *any* valid credential, including a key
created with a single narrow scope, so the failure is invisible: the endpoint
works, for everyone, forever.

``_EXPECTED_UNSCOPED`` is the whole exemption list. Adding to it is the point:
it makes granting a credential-wide endpoint a deliberate, reviewed edit rather
than a base class someone picked because it was shorter.
"""

from __future__ import annotations

from django.urls import get_resolver

from urbanlens.core.tests.testcase import SimpleTestCase

#: Views that authenticate but require no scope, with the reason each is safe.
#: Every one of these must describe *the credential itself*, never user data.
_EXPECTED_UNSCOPED: dict[str, str] = {
    "AuthSessionView": "reports the calling credential's own grant; gating it behind a scope would be circular",
}


def _external_api_views() -> list[tuple[str, type]]:
    """Every resolved view class served under the external API."""

    def walk(resolver, prefix=""):
        for pattern in resolver.url_patterns:
            if hasattr(pattern, "url_patterns"):
                yield from walk(pattern, prefix + str(pattern.pattern))
            else:
                yield prefix + str(pattern.pattern), pattern.callback

    found: list[tuple[str, type]] = []
    for path, callback in walk(get_resolver()):
        view_class = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if view_class is None:
            continue
        if "external_api" not in getattr(view_class, "__module__", ""):
            continue
        found.append((path, view_class))
    return found


def _enforces_scope(view_class: type) -> bool:
    return any("Scope" in getattr(perm, "__name__", "") for perm in getattr(view_class, "permission_classes", []))


class ExternalApiScopeCoverageTests(SimpleTestCase):
    def test_the_scan_finds_the_api(self) -> None:
        """Guards the checks below against passing on an empty set."""
        self.assertGreater(len(_external_api_views()), 50)

    def test_every_endpoint_enforces_a_scope_or_is_a_named_exception(self) -> None:
        unscoped = sorted({cls.__name__ for _path, cls in _external_api_views() if not _enforces_scope(cls)})

        self.assertEqual(
            unscoped,
            sorted(_EXPECTED_UNSCOPED),
            "an external API endpoint accepts any credential regardless of its scopes",
        )

    def test_the_exception_list_has_not_grown_silently(self) -> None:
        """A second unscoped view is a decision, not an implementation detail."""
        self.assertEqual(len(_EXPECTED_UNSCOPED), 1)

    def test_each_exception_documents_why_it_is_safe(self) -> None:
        undocumented = [name for name, reason in _EXPECTED_UNSCOPED.items() if len((reason or "").strip()) < 20]

        self.assertEqual(undocumented, [])
