"""The published OpenAPI schema must include the bearer-authenticated E2EE surface.

Why this file exists at all: the eight ``/dashboard/e2ee/`` key-exchange views
have accepted API-key and OAuth2 bearer credentials since they were converted to
:class:`~urbanlens.dashboard.external_api.mixins.DualAuthJsonView`, but
``schema.preprocess_external_api_only`` filtered the published contract down to
paths under ``/dashboard/api/external/``. The endpoints worked and were simply
invisible, so the mobile team read the schema, concluded end-to-end encryption
had never shipped, and filed re-implementing it as their only P0. A capability
that exists but is undocumented is, from a client author's seat, identical to a
capability that does not exist.

The fix is deliberately a *second exact prefix* rather than a looser match. The
assertions below pin both halves of that: the e2ee paths must be present, and
the internal ``/dashboard/rest/`` surface - which has no public contract and
shares the ``/dashboard/`` root with everything else - must still be absent.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase
from django.urls import reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.schema import PUBLISHED_SCHEMA_PREFIXES, SCHEMA_PATH_PREFIX_PATTERN, preprocess_external_api_only

#: Every bearer-authenticated ``/dashboard/e2ee/`` route - the eight
#: ``DualAuthJsonView`` subclasses, which is the whole key-exchange contract a
#: native client needs.
#:
#: Two sibling routes are deliberately absent. ``change-password/`` is a plain
#: Django ``View`` (session-only, no bearer support), so drf-spectacular never
#: sees it. ``login-params/`` is a DRF view but carries
#: ``@extend_schema(exclude=True)``; it is anonymous by design and is not part
#: of the authenticated surface. See this suite's report for why that second
#: one is arguably worth revisiting.
E2EE_SCHEMA_PATHS = (
    "/dashboard/e2ee/enroll/",
    "/dashboard/e2ee/keys/",
    "/dashboard/e2ee/keys/{profile_slug}/",
    "/dashboard/e2ee/conversation-key/{profile_slug}/",
    "/dashboard/e2ee/group-key/{group_uuid}/",
    "/dashboard/e2ee/rewrap/",
    "/dashboard/e2ee/rewrap-all/",
    "/dashboard/e2ee/reset/",
)


class SchemaPrefixFilterTests(SimpleTestCase):
    """The preprocessing hook itself, exercised without generating a schema.

    Driving the hook directly is what lets the "anchored, not a substring"
    property be stated precisely - the near-miss paths below never appear in
    the real urlconf, so a schema-wide substring assertion could not catch a
    filter that had been loosened to ``"e2ee" in path``.
    """

    def _filter(self, paths: list[str]) -> list[str]:
        """Run *paths* through the hook and return the ones it kept.

        Args:
            paths: Candidate URL paths, in the ``path`` position of the
                ``(path, path_regex, method, callback)`` tuples drf-spectacular
                hands the hook.

        Returns:
            The subset of *paths* the hook admitted, in the original order.
        """
        endpoints = [(path, path, "GET", None) for path in paths]
        return [path for path, _regex, _method, _callback in preprocess_external_api_only(endpoints)]

    def test_external_api_prefix_is_still_admitted(self) -> None:
        """The original behavior is unchanged - this widened the filter, it did not move it."""
        self.assertEqual(self._filter(["/dashboard/api/external/v1/pins/"]), ["/dashboard/api/external/v1/pins/"])

    def test_e2ee_prefix_is_admitted(self) -> None:
        self.assertEqual(self._filter(["/dashboard/e2ee/keys/"]), ["/dashboard/e2ee/keys/"])

    def test_internal_rest_surface_is_still_excluded(self) -> None:
        """``/dashboard/rest/`` has no public contract and must never be published."""
        self.assertEqual(self._filter(["/dashboard/rest/pins/"]), [])

    def test_the_match_is_anchored_at_the_start_of_the_path(self) -> None:
        """A path that merely *contains* an admitted prefix must not be published.

        Guards against someone "simplifying" the prefix test into a substring
        test, which would quietly publish any internal route whose URL happened
        to embed one of these words.
        """
        near_misses = [
            "/dashboard/rest/e2ee/keys/",
            "/dashboard/internal/dashboard/e2ee/keys/",
            "/admin/dashboard/api/external/v1/pins/",
        ]
        self.assertEqual(self._filter(near_misses), [])

    def test_a_longer_segment_sharing_the_prefix_is_not_admitted(self) -> None:
        """``/dashboard/e2ee-debug/`` is a different route, not a sub-path of the E2EE mount."""
        self.assertEqual(self._filter(["/dashboard/e2ee-debug/keys/"]), [])

    def test_the_path_prefix_pattern_anchors_every_branch(self) -> None:
        """``^a|b`` anchors only ``a`` - every branch has to be inside the group.

        drf-spectacular prepends ``^`` to ``SCHEMA_PATH_PREFIX`` only when the
        value does not already start with one, so an unanchored alternation
        would leave every branch after the first free to match mid-path and
        mangle the operation ids of unrelated routes.
        """
        self.assertTrue(SCHEMA_PATH_PREFIX_PATTERN.startswith("^(?:"), f"{SCHEMA_PATH_PREFIX_PATTERN!r} must be an anchored non-capturing group.")
        compiled = re.compile(SCHEMA_PATH_PREFIX_PATTERN)
        self.assertIsNone(compiled.search("/dashboard/rest/dashboard/e2ee/keys/"), "A later branch is matching mid-path - the alternation is not anchored.")
        # Every published mount must be stripped, version segment included -
        # otherwise its routes are tagged 'v1' instead of by resource.
        self.assertEqual(compiled.sub("", "/dashboard/api/external/v1/pins/"), "/pins/")
        self.assertEqual(compiled.sub("", "/dashboard/e2ee/keys/"), "/keys/")

    def test_every_published_prefix_is_absolute_and_trailing_slashed(self) -> None:
        """A prefix missing its trailing slash would admit sibling mounts by accident.

        ``"/dashboard/e2ee"`` (no slash) matches ``/dashboard/e2ee-debug/`` too;
        the trailing slash is what makes each entry name exactly one mount.
        """
        for prefix in PUBLISHED_SCHEMA_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.startswith("/"), f"{prefix!r} is not an absolute path prefix.")
                self.assertTrue(prefix.endswith("/"), f"{prefix!r} must end in '/' so it cannot match a sibling mount.")


class SchemaDocumentContentTests(TestCase):
    """The generated document, end to end - what a client author actually reads."""

    def _schema_body(self) -> str:
        """Fetch the published schema as text."""
        response = self.client.get(reverse("external_api:schema"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_e2ee_paths_appear_in_the_published_schema(self) -> None:
        """Every bearer-capable E2EE route is documented.

        This is the assertion whose absence cost the mobile team a P0: the
        endpoints already worked, so nothing but the schema could have told
        them so.
        """
        body = self._schema_body()
        for path in E2EE_SCHEMA_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, body, f"{path} is bearer-authenticated but missing from the published contract.")

    def test_the_internal_rest_surface_is_absent_from_the_published_schema(self) -> None:
        """Widening the filter must not have let the internal API in with it."""
        self.assertNotIn("/dashboard/rest/", self._schema_body())

    def test_the_external_api_surface_is_still_present(self) -> None:
        """A regression guard for widening the filter by *replacing* the original prefix."""
        body = self._schema_body()
        self.assertIn("/dashboard/api/external/v1/pins/", body)
        self.assertIn("/dashboard/api/external/v1/wikis/", body)

    def test_operation_ids_and_tags_did_not_collapse(self) -> None:
        """Admitting a second mount must not silently rename every operation.

        When ``SCHEMA_PATH_PREFIX`` is unset drf-spectacular estimates it as the
        longest path every endpoint shares, and strips that to derive operation
        ids and tags. A second mount drags that estimate back to ``/dashboard``,
        at which point ``pins_retrieve`` becomes ``api_external_v1_pins_retrieve``
        and every tag in the document collapses into one bucket called ``api`` -
        a breaking change to generated client code, shipped as a side effect of
        a documentation fix. ``schema._pin_schema_path_prefix`` exists to stop
        that; this is the assertion that notices if it stops working.
        """
        body = self._schema_body()
        self.assertIn("operationId: pins_retrieve", body)
        self.assertNotIn("operationId: api_external_v1_", body)
        self.assertNotIn("operationId: v1_", body)
        # Tag extraction strips the same prefix, so a collapsed prefix shows up
        # as every operation sharing one meaningless tag.
        self.assertIn("- pins\n", body)
        self.assertIn("- wikis\n", body)

    def test_the_e2ee_surface_is_not_mirrored_under_the_external_mount(self) -> None:
        """There must be exactly one published key-exchange contract, not two.

        ``controllers/e2ee.py``'s module docstring forbids duplicating these
        views under ``/api/external/v1/``, and the reason is specific: two
        copies of a key-exchange contract drift, and a drifted key-exchange
        contract means somebody's messages stop decrypting. Documenting the
        existing mount is the whole fix.
        """
        self.assertNotIn("/dashboard/api/external/v1/e2ee/", self._schema_body())
