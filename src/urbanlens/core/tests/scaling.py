"""What a scaling test needs whatever it is measuring.

Two mixins measure how an endpoint behaves as its data grows -
:class:`~urbanlens.core.tests.query_scaling.QueryScalingMixin` counts queries,
:class:`~urbanlens.core.tests.render_scaling.RenderTimeScalingMixin` times the
render - and both need the same two things: a way to create more of the rows
the endpoint lists, and a guard that the seed actually changed what it renders.

The guard is the part worth sharing. A scaling test whose seed grows something
the endpoint does not list renders the same page twice and passes without
measuring anything, and that is not hypothetical: during the 2026-08-17 audit a
survey reported the conversation list flat while seeding pins, and seeding
conversations properly found about eleven queries per row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.core.tests.testcase import TestCase

    # Spelled this way so mypy sees the real assertion API inside the mixin
    # bodies while the runtime MRO stays a plain mixin over whatever TestCase
    # the subclass names.
    _Base = TestCase
else:
    _Base = object

#: Rows seeded before the first and second measurement. The second is large
#: enough that one query per row is unmistakable against normal variation.
FIRST_BATCH = 2
SECOND_BATCH = 10

#: How many more bytes the response must return before the seed counts as having
#: exercised the endpoint. A response is not perfectly stable between identical
#: requests - recorded activity, streak counters and timestamps move it a little -
#: so "grew at all" is too weak a test. Measured on ``trips.overview``: four
#: repeats with nothing seeded spanned **11 bytes**, while ten real trips added
#: **12,033**. Three orders of magnitude apart, so the floor only has to sit
#: clear of the noise.
MIN_GROWTH_BYTES = 200


class SeedScalingMixin(_Base):
    """The seed contract, and the guard that the seed did something."""

    #: Overridable per test class - a slow seed may want smaller batches.
    first_batch: int = FIRST_BATCH
    second_batch: int = SECOND_BATCH

    def seed_rows(self, count: int) -> None:
        """Create *count* more of whatever the endpoint under test lists.

        The rows created here must be the rows the endpoint renders. Seeding
        something else produces a constant-size response and a meaningless pass,
        which is what ``expect_growth`` guards against.

        Args:
            count: How many rows to add.
        """
        raise NotImplementedError("scaling tests must seed the rows their endpoint lists")

    def assert_seed_exercised_endpoint(
        self, url: str, small_body: int, large_body: int, *, expect_growth: bool, growth_waiver: str
    ) -> None:
        """Fail unless the seed changed what *url* renders.

        Args:
            url: The URL that was measured, for the message.
            small_body: Response length at ``first_batch`` rows.
            large_body: Response length at ``first_batch + second_batch`` rows.
            expect_growth: Require the body to have grown. Turn this off only
                for endpoints that cap what they render (pagination).
            growth_waiver: Why this endpoint's response cannot grow. Required
                when *expect_growth* is False, so the exemption is legible.

        Raises:
            AssertionError: The seed did not exercise the endpoint, or growth
                was waived without a reason.
        """
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")
        if not expect_growth:
            return

        total = self.first_batch + self.second_batch
        self.assertGreaterEqual(
            large_body - small_body,
            MIN_GROWTH_BYTES,
            f"{url} returned {small_body} bytes for {self.first_batch} rows and {large_body} for {total} - "
            f"a change of {large_body - small_body}, under the {MIN_GROWTH_BYTES}-byte noise floor. "
            "The seed does not exercise this endpoint, so a flat measurement would prove nothing. "
            "Seed the rows this endpoint actually lists, or pass expect_growth=False with a reason.",
        )
