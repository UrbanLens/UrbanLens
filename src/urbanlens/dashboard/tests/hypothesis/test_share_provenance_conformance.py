"""Every module that creates a ``PinShare`` must also record the exposure.

``CLAUDE.md``: "Any new pin/location share path must call ``resolve_origin_share``
+ ``record_share_exposure`` to keep the ``LocationExposure`` provenance chain
intact." Today six modules create shares and all six comply - but nothing
*enforces* it, and the cost of the seventh forgetting is silent: the share still
sends, so nothing looks broken, while the recipient's onward shares chain under
the wrong ancestor (or none), which is exactly what the chain exists to prevent.

A static check rather than a behavioural one because the failure is "a new code
path exists that nobody wrote a test for" - the case a behavioural test by
definition misses.

The related rule (do *not* record an exposure when merely accepting or rejecting
an already-sent share) is covered behaviourally elsewhere; see
``services.sharing.pin_sharing``'s module docstring for why that asymmetry is
deliberate.
"""

from __future__ import annotations

import ast
import pathlib

from urbanlens.core.tests.testcase import SimpleTestCase

_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[3] / "dashboard"

#: Modules that construct a PinShare but deliberately leave exposure recording to
#: their caller. Each entry needs a comment saying who records it instead - the
#: point of this test is that "the caller probably does it" is asserted, not assumed.
_EXPOSURE_RECORDED_BY_CALLER: dict[str, str] = {}


def _creates_pin_share(tree: ast.AST) -> bool:
    """Whether the module constructs a ``PinShare`` row."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # PinShare(...)
        if isinstance(func, ast.Name) and func.id == "PinShare":
            return True
        # PinShare.objects.create(...) / .get_or_create(...) / .bulk_create(...)
        if isinstance(func, ast.Attribute) and func.attr in {"create", "get_or_create", "bulk_create"}:
            owner = func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "objects":
                base = owner.value
                if isinstance(base, ast.Name) and base.id == "PinShare":
                    return True
    return False


class ShareProvenanceConformanceTests(SimpleTestCase):
    """A new share path cannot silently skip the provenance chain."""

    def test_every_pin_share_creator_records_an_exposure(self) -> None:
        creators: list[str] = []
        offenders: list[str] = []

        for path in sorted(_SOURCE_ROOT.rglob("*.py")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            source = path.read_text()
            if "PinShare" not in source:
                continue
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:  # pragma: no cover - a broken file is another test's problem
                continue
            if not _creates_pin_share(tree):
                continue

            relative = str(path.relative_to(_SOURCE_ROOT.parent))
            creators.append(relative)
            if relative in _EXPOSURE_RECORDED_BY_CALLER:
                continue
            if "record_share_exposure" not in source:
                offenders.append(f"{relative} creates a PinShare but never calls record_share_exposure")

        # Guards the scan itself - a refactor that moves or renames PinShare would
        # otherwise leave this passing while checking nothing.
        self.assertGreaterEqual(len(creators), 5, f"share-creation scan found suspiciously few modules: {creators}")
        self.assertEqual(offenders, [], "\n".join(offenders))
