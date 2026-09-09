"""Hold a fast reimplementation to the answers of the function it replaced.

A batch or cached fast path is a second implementation of a decision that
already had one, and the two drift silently: nothing fails when they disagree,
because each is self-consistent. When the decision is a privacy check, the
disagreement is not a slow page - it is someone's real name and avatar shown to
a viewer with no standing right to see them.

The 2026-08-17 audit added exactly such a path (``Profile.visible_profile_pks``,
a batch form of ``can_view_profile``) and tested it this way rather than against
hand-written expectations. It caught a real bug in that fix within minutes: an
early return skipped the temporary-access fallback, so a profile holding a valid
grant came back masked. Hand-written expectations would not have covered the
case, because the author who wrote the bug also writes the expectations.

Use :func:`assert_agrees` whenever adding a fast path over an existing
predicate, and prefer generating the inputs (every enum value, every
relationship) over choosing them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


def assert_agrees(
    reference: Callable[[Any], Any],
    candidate: Callable[[Any], Any],
    subjects: Iterable[Any],
    *,
    describe: Callable[[Any], str] = repr,
    label: str = "candidate",
) -> None:
    """Assert *candidate* returns what *reference* returns, for every subject.

    Any return value compares, not only a predicate's: a batch serializer held
    to the per-row one it replaced is the same question as a batch permission
    check held to the single one, and drifts the same silent way.

    Args:
        reference: The established implementation, treated as correct.
        candidate: The reimplementation under test.
        subjects: Inputs to compare on. Generate these rather than pick them -
            the interesting disagreements are in combinations nobody thought of.
        describe: Renders a subject for the failure message. Default ``repr``.
        label: Name for *candidate* in the failure message.

    Raises:
        AssertionError: Any subject where the two disagree, listing each with
            the direction of the disagreement, since for a predicate "wrongly
            visible" and "wrongly hidden" are very different bugs.
    """
    disagreements = []
    for subject in subjects:
        expected = reference(subject)
        actual = candidate(subject)
        if expected != actual:
            disagreements.append(f"  {describe(subject)}: {label} {_direction(expected, actual)}")

    if disagreements:
        listing = "\n".join(disagreements)
        raise AssertionError(
            f"{label} disagrees with the reference implementation on {len(disagreements)} subject(s):\n{listing}"
        )


def _direction(expected: Any, actual: Any) -> str:
    """How *actual* differs from *expected*, phrased for whatever they are."""
    if isinstance(expected, bool) and isinstance(actual, bool):
        return "said yes where the reference said no" if actual else "said no where the reference said yes"
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        differing = [
            f"{key}={actual.get(key)!r} (reference {expected.get(key)!r})"
            for key in keys
            if expected.get(key) != actual.get(key)
        ]
        return "differs on " + ", ".join(differing)
    return f"returned {actual!r}, reference returned {expected!r}"
