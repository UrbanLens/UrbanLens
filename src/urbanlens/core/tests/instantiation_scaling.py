"""Assert an endpoint does not build a model instance per row it renders.

`QueryScalingMixin` counts statements and `RenderTimeScalingMixin` times the
render. Neither can see the defect this exists for: the map payload ran a flat
21 queries and spent 88% of a 5.79-second build inside `Model.__init__`,
constructing 63,240 objects to emit 10,000 flat dicts. Query count was constant
the whole time, and a timing budget reads the same 6x-too-expensive row as
"slow machine" on a contended host.

**What separates the classes is how many model instances one row costs.**
Django sends `post_init` from `Model.__init__` unconditionally, so a receiver
sees every instantiation - the fetched row, each `select_related` companion, and
one fresh object per `prefetch_related` through-row. Taking
``(objects(large) - objects(small)) / (large - small)`` cancels whatever the
page builds regardless of its rows, and what is left is an integer that does not
move with machine speed, load, or the number of samples: *this endpoint builds N
model objects for every row it shows.*

Two properties worth not re-deriving:

- **It is exact, so it needs no tolerance and no repeats.** A timing mixin needs
  best-of-five and a fraction-of-baseline denominator to survive a shared host.
  This measures a count, so one request per size is the whole measurement, and a
  failure reports the same number on any machine.
- **It sees through the prefetch that a query counter is blind to.** A
  `prefetch_related` over a small shared vocabulary is one extra query however
  many parents it fans out over, but Django rebuilds the related object per
  through-row - 128 distinct labels became ~36,000 instances on the map. The
  query counter reads that as flat; this reads it as +3.6 objects per row.

The counter is deliberately not a substitute for its siblings. Objects per row
says nothing about a page that queries per row without instantiating (`.exists()`
in a loop) or one that is slow for reasons unrelated to the ORM, so the three are
interpretable together and a failure here reports the query count alongside.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from django.db import connection
from django.db.models.signals import post_init
from django.test.utils import CaptureQueriesContext

from urbanlens.core.tests.scaling import SeedScalingMixin

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Model instances one rendered row may cost. A list endpoint that builds the
#: row's own model and nothing else sits at 1.0; anything above means a related
#: object is being rebuilt per row, which is the shape a projection removes.
#: Raise it only with a comment saying why the objects are necessary.
MAX_OBJECTS_PER_ROW = 1.0


@dataclass(frozen=True, slots=True)
class InstantiationSample:
    """One size's object count, kept whole so a failure can name the models."""

    total: int
    by_model: Mapping[str, int]
    body_bytes: int


class count_instantiations:  # noqa: N801 # a context manager, named as one reads at the call site
    """Count every Django model instantiated inside the block, by model.

    Usable on its own around any callable, not just a request, so a service can
    be measured without going through a URL::

        with count_instantiations() as counted:
            MapPinPayloadService(profile).all(query)
        self.assertLess(counted.total, 100)

    Counts instantiations, which is not the same as rows fetched: a
    `select_related` companion, a `prefetch_related` through-row and a deferred
    reload each construct an object and each is counted, which is the point.
    """

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def _record(self, sender: type[Any], **_kwargs: Any) -> None:
        self._counts[f"{sender._meta.app_label}.{sender.__name__}"] += 1

    def __enter__(self) -> Self:
        # weak=False: the receiver is a bound method of this object, and a weak
        # reference to it dies immediately.
        post_init.connect(self._record, weak=False, dispatch_uid=id(self))
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        post_init.disconnect(self._record, dispatch_uid=id(self))

    @property
    def total(self) -> int:
        """How many model instances were constructed in the block."""
        return sum(self._counts.values())

    @property
    def by_model(self) -> Mapping[str, int]:
        """Instantiations per ``app_label.ModelName``, largest first."""
        return dict(self._counts.most_common())


class InstantiationScalingMixin(SeedScalingMixin):
    """Mixin asserting a row costs few model instances.

    Subclasses implement :meth:`seed_rows`, exactly as for the sibling mixins.
    """

    def measure_instantiations(self, url: str, **extra: Any) -> InstantiationSample:
        """Fetch *url*, returning the model instances its render constructed.

        Args:
            url: The URL to fetch.
            **extra: Passed to the test client (headers, auth).

        Returns:
            The instantiation count, its per-model breakdown, and the body size.
        """
        with count_instantiations() as counted:
            response = self.client.get(url, **extra)
        self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")
        return InstantiationSample(total=counted.total, by_model=counted.by_model, body_bytes=len(response.content))

    def assert_objects_per_row_bounded(
        self,
        url: str,
        *,
        max_objects_per_row: float = MAX_OBJECTS_PER_ROW,
        expect_growth: bool = True,
        growth_waiver: str = "",
        **extra: Any,
    ) -> None:
        """Assert one more row costs at most *max_objects_per_row* model instances.

        Args:
            url: The URL to measure.
            max_objects_per_row: Model instances one row may cost. Raise it only
                with a comment saying why the objects are necessary.
            expect_growth: Require the response body to grow between the two
                sizes. Turn this off only for endpoints that cap what they
                render, and say why in *growth_waiver*.
            growth_waiver: Why this endpoint's response cannot grow.
            **extra: Passed to the test client.

        Raises:
            AssertionError: A row costs more than *max_objects_per_row*
                instances, or the seed did not exercise the endpoint.
        """
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        self.seed_rows(self.first_batch)
        small = self.measure_instantiations(url, **extra)
        self.seed_rows(self.second_batch)
        large = self.measure_instantiations(url, **extra)

        self.assert_seed_exercised_endpoint(
            url, small.body_bytes, large.body_bytes, expect_growth=expect_growth, growth_waiver=growth_waiver
        )

        per_row = (large.total - small.total) / self.second_batch
        if per_row > max_objects_per_row:
            with CaptureQueriesContext(connection) as captured:
                self.client.get(url, **extra)
            report = "\n".join(
                f"      {name}: {large.by_model[name] - small.by_model.get(name, 0):+d}"
                for name in large.by_model
                if large.by_model[name] - small.by_model.get(name, 0) > 0
            )
            raise AssertionError(
                f"{url} built {small.total} model objects for {self.first_batch} rows and "
                f"{large.total} for {self.first_batch + self.second_batch} - "
                f"{per_row:.1f} per row, over the {max_objects_per_row} budget.\n"
                f"    (it ran {len(captured.captured_queries)} queries, so a query counter reads this as flat)\n"
                f"    objects added by the {self.second_batch} extra rows:\n{report}",
            )
