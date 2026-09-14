"""Assert an endpoint does not build a model instance per row it renders."""

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


class count_instantiations:  # noqa: N801  # a context manager, named as one reads at the call
    """Count every Django model instantiated inside the block, by model.

    Usable on its own around any callable, not just a request, so a service can be measured without going
    through a URL:: with count_instantiations() as counted: MapPinPayloadService(profile).all(query)
    self.assertLess(counted.total, 100) Counts instantiations, which is not the same as rows fetched: a
    `select_related` companion, a `prefetch_related` through-row and a deferred reload each construct an object
    and each is counted, which is the point."""

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
            url: The URL to fetch. **extra: Passed to the test client (headers, auth).

        Returns:
            The instantiation count, its per-model breakdown, and the body size."""
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
            url: The URL to measure. max_objects_per_row: Model instances one row may cost.

        Raises:
            AssertionError: A row costs more than *max_objects_per_row* instances, or the seed did not exercise the endpoint."""
        if not expect_growth and not growth_waiver:
            raise AssertionError("expect_growth=False needs growth_waiver= explaining why the response cannot grow")

        self.seed(self.first_batch)
        small = self.measure_instantiations(url, **extra)
        self.seed(self.second_batch)
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
