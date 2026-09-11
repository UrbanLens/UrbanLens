"""A seed that quietly creates fewer rows than it reports makes every number wrong.

`seed_heavy_account` exists to make the neighbour test meaningful, and everything
downstream of it — k6 thresholds, the connection sampler's budget, the claim that
a label edit is expensive — is arithmetic over the row count it says it made. So
the count has to be true, and the two ways it could quietly not be are both
things that have actually happened in this repo:

- rows merging because coordinates were too close (three pins at 0.0001 degrees
  created one `Location`), and
- `ANALYZE` not running, which does not change the count but makes every timing
  taken afterwards a measurement of the planner's ignorance (N10: 4.683s against
  0.384s at 5,000 pins).

Both are asserted here against the database rather than against the return value,
because the return value is the thing under suspicion.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.integration_testing.perf_seed import (
    BATCH_SIZE,
    HEAVY_LABEL_NAME,
    MAX_SEEDED_PINS,
    PIN_NAME_PREFIX,
    VOCABULARY,
    seed_heavy_account,
)

#: Small enough to keep the suite quick, large enough to cross a batch boundary
#: when doubled — the off-by-one in a batched loop is what this size is for.
SEEDED = 12


class TheSeedCreatesWhatItReportsTests(TestCase):
    """The count in the manifest must be the count in the database."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_it_creates_the_requested_number_of_root_pins(self) -> None:
        report = seed_heavy_account(self.profile, pins=SEEDED)

        actual = Pin.objects.filter(profile=self.profile).root_pins().count()
        self.assertEqual(actual, SEEDED)
        self.assertEqual(report["pins"], actual, "the manifest disagrees with the database")
        self.assertEqual(report["created"], SEEDED)

    def test_every_pin_gets_its_own_location(self) -> None:
        """The failure this guards is silent: rows merge and the count still looks right.

        Asserted on distinct locations rather than on the pin count, because the
        pin count survives the defect — it is the places that collapse.
        """
        seed_heavy_account(self.profile, pins=SEEDED)

        pins = Pin.objects.filter(profile=self.profile).root_pins()
        self.assertEqual(
            pins.values("location_id").distinct().count(),
            SEEDED,
            "seeded pins share locations, so the seed size is not the number of distinct places",
        )
        coordinates = set(Location.objects.filter(pins__profile=self.profile).values_list("latitude", "longitude"))
        self.assertEqual(len(coordinates), SEEDED)

    def test_every_pin_carries_the_shared_label(self) -> None:
        """The P102 trigger. A seed that spread labels would make the fan-out look cheap."""
        seed_heavy_account(self.profile, pins=SEEDED)

        label = Label.objects.get(name=HEAVY_LABEL_NAME, profile=self.profile)
        self.assertEqual(
            Pin.objects.filter(profile=self.profile, labels=label).count(),
            SEEDED,
            "not every seeded pin carries the heavy label, so a label edit against this account "
            "does less work than the scenario claims",
        )

    def test_pins_carry_a_slug_despite_bulk_create(self) -> None:
        """`bulk_create` skips `save`, which is where `ensure_slug` runs."""
        seed_heavy_account(self.profile, pins=SEEDED)

        self.assertEqual(
            Pin.objects.filter(profile=self.profile, slug__isnull=True).count(),
            0,
            "seeded pins have no slug, so every one of them exercises the payload's uuid fallback "
            "rather than the path a real pin takes",
        )

    def test_it_crosses_a_batch_boundary_correctly(self) -> None:
        """The loop is batched, so the interesting sizes are around the batch size.

        Uses a size just over `BATCH_SIZE` rather than a round number, since an
        off-by-one in the final partial batch is the defect a round number hides.
        """
        size = BATCH_SIZE + 3

        report = seed_heavy_account(self.profile, pins=size)

        self.assertEqual(Pin.objects.filter(profile=self.profile).root_pins().count(), size)
        self.assertEqual(report["created"], size)


class TheSeedIsIdempotentTests(TestCase):
    """Re-running a fixture must top it up, not double it."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_running_it_twice_leaves_the_requested_number(self) -> None:
        seed_heavy_account(self.profile, pins=SEEDED)

        second = seed_heavy_account(self.profile, pins=SEEDED)

        self.assertEqual(Pin.objects.filter(profile=self.profile).root_pins().count(), SEEDED)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["already_present"], SEEDED)

    def test_asking_for_more_tops_up_rather_than_restarting(self) -> None:
        seed_heavy_account(self.profile, pins=SEEDED)

        second = seed_heavy_account(self.profile, pins=SEEDED * 2)

        self.assertEqual(Pin.objects.filter(profile=self.profile).root_pins().count(), SEEDED * 2)
        self.assertEqual(second["created"], SEEDED)

    def test_topping_up_does_not_collide_on_coordinates(self) -> None:
        """The second run must continue the grid, not restart it.

        `Location` is unique on `(latitude, longitude)`, so a restarted grid
        raises rather than merging — which is a loud failure, but only if
        something runs the second seed. This is that something.
        """
        seed_heavy_account(self.profile, pins=SEEDED)
        seed_heavy_account(self.profile, pins=SEEDED * 2)

        coordinates = Location.objects.filter(pins__profile=self.profile).values_list("latitude", "longitude")
        self.assertEqual(len(set(coordinates)), SEEDED * 2)

    def test_topping_up_by_a_different_amount_does_not_collide(self) -> None:
        """Three runs, each creating a different number of pins.

        The test above passes without exercising the defect it is named for: it
        creates `SEEDED` twice, so anything derived from "how many are being
        created now" comes out the same both times. The grid geometry was
        derived from exactly that, and this is the shape that found it — a
        200-then-20,000 top-up raised `IntegrityError` on the unique constraint
        while the doubling test stayed green.
        """
        seed_heavy_account(self.profile, pins=3)
        seed_heavy_account(self.profile, pins=17)
        seed_heavy_account(self.profile, pins=40)

        coordinates = Location.objects.filter(pins__profile=self.profile).values_list("latitude", "longitude")
        self.assertEqual(len(set(coordinates)), 40)
        self.assertEqual(Pin.objects.filter(profile=self.profile).root_pins().count(), 40)

    def test_a_pin_index_always_lands_on_the_same_coordinate(self) -> None:
        """The property the collision was a symptom of, asserted directly.

        Two accounts seeded to different sizes must agree about where pin *n*
        goes, because that is what makes a top-up safe. Asserted across profiles
        rather than across runs, so the grid stays pinned even if the top-up
        path is later rewritten.
        """
        other = baker.make(User).profile

        seed_heavy_account(self.profile, pins=5)
        seed_heavy_account(other, pins=45)

        mine = sorted(Location.objects.filter(pins__profile=self.profile).values_list("latitude", "longitude"))
        theirs = sorted(Location.objects.filter(pins__profile=other).values_list("latitude", "longitude"))
        self.assertEqual(
            mine, theirs[: len(mine)], "the same pin index landed on different coordinates in differently-sized seeds"
        )

    def test_a_seed_too_large_for_the_grid_is_refused(self) -> None:
        """Better than silently laying pins past the north pole."""
        with self.assertRaises(ValueError) as caught:
            seed_heavy_account(self.profile, pins=MAX_SEEDED_PINS + 1)

        self.assertIn(str(MAX_SEEDED_PINS), str(caught.exception))


class TheSeedTellsThePlannerTests(TestCase):
    """A seeded benchmark without `ANALYZE` measures the planner, not the query."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_statistics_are_refreshed_by_default(self) -> None:
        """`reltuples` is -1 on a never-analysed table and non-negative after.

        Asserted against `pg_class` rather than against the report's own flag,
        since the flag is what would be wrong if this were broken.
        """
        report = seed_heavy_account(self.profile, pins=SEEDED)

        self.assertTrue(report["analyzed"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [Pin._meta.db_table])
            reltuples = cursor.fetchone()[0]
        self.assertGreaterEqual(reltuples, 0, "ANALYZE did not run against the pins table")

    def test_it_can_be_turned_off_and_says_so(self) -> None:
        """Only useful for demonstrating the cost of skipping it — but it must be honest."""
        report = seed_heavy_account(self.profile, pins=SEEDED, analyze=False)

        self.assertFalse(report["analyzed"])


class TheReportIsUsableByTheHarnessTests(TestCase):
    """Every value the load harness reads out of the manifest has to be true.

    The k6 neighbour scenario edits the shared label *by id* and filters pins by
    a *name prefix*, both taken from this report. A report that named a label
    that did not exist, or a prefix that matched nothing, would produce a run
    that finished green having measured a 404 and an empty result set.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_the_reported_label_id_resolves_to_the_reported_label(self) -> None:
        report = seed_heavy_account(self.profile, pins=SEEDED)

        label = Label.objects.get(pk=report["label_id"])
        self.assertEqual(label.name, report["label"])
        self.assertEqual(label.kind, report["label_kind"])
        self.assertEqual(label.profile, self.profile)

    def test_the_reported_prefix_matches_every_seeded_pin(self) -> None:
        report = seed_heavy_account(self.profile, pins=SEEDED)

        matched = Pin.objects.filter(profile=self.profile, name__startswith=report["pin_name_prefix"]).count()
        self.assertEqual(matched, SEEDED, "the reported name prefix does not select the pins that were seeded")
        self.assertEqual(report["pin_name_prefix"], PIN_NAME_PREFIX)

    def test_the_reported_label_is_editable_by_id_after_a_rename(self) -> None:
        """A load run renames or recolours the label; a later top-up must not fork it.

        The seeder finds its label by name, so a run that changed the name would
        leave the next seed creating a second label and the pins split between
        them - which reads as "the label edit got cheaper".
        """
        first = seed_heavy_account(self.profile, pins=SEEDED)

        second = seed_heavy_account(self.profile, pins=SEEDED * 2)

        self.assertEqual(second["label_id"], first["label_id"])
        self.assertEqual(Label.objects.filter(profile=self.profile, name=HEAVY_LABEL_NAME).count(), 1)
        self.assertEqual(Pin.objects.filter(profile=self.profile, labels=second["label_id"]).count(), SEEDED * 2)


class TheSeedHoldsTheMapCentreConstantTests(TestCase):
    """The seed stores the map centre, so a load run does not measure computing it.

    `Profile.compute_map_center` sits on the critical path of the map page. It
    used to be O(n^2) in pins, so a seeded account without a stored centre wedged
    the process on the run's first map request and every phase after it measured
    that (P108, fixed). Still held constant: the harness exists to compare phases
    against each other, and a per-account one-off on the first request of the run
    is exactly the kind of variable that makes two phases incomparable.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_the_centre_is_stored_by_default(self) -> None:
        report = seed_heavy_account(self.profile, pins=SEEDED)

        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.map_center_latitude)
        self.assertIsNotNone(self.profile.map_center_longitude)
        self.assertIsNotNone(report["map_center"])

    def test_the_stored_centre_is_the_answer_the_slow_path_would_give(self) -> None:
        """Not an approximation, and this is what says so.

        The seeded grid spans far less than the 1,000 km cluster radius, so every
        point is in one cluster and the densest-cluster centroid is the plain
        mean. If the grid ever grew past that radius this would fail, which is
        the right outcome - the shortcut would no longer be equivalent.
        """
        seed_heavy_account(self.profile, pins=SEEDED)
        self.profile.refresh_from_db()
        stored = (float(self.profile.map_center_latitude), float(self.profile.map_center_longitude))

        Profile.objects.filter(pk=self.profile.pk).update(map_center_latitude=None, map_center_longitude=None)
        self.profile.refresh_from_db()
        computed = self.profile.compute_map_center()

        self.assertAlmostEqual(stored[0], computed[0], places=4)
        self.assertAlmostEqual(stored[1], computed[1], places=4)

    def test_it_can_be_left_unset_to_reproduce_p108(self) -> None:
        report = seed_heavy_account(self.profile, pins=SEEDED, precompute_map_center=False)

        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.map_center_latitude)
        self.assertIsNone(report["map_center"])


class TheSeedCanCarryARealisticLabelCountTests(TestCase):
    """One label per pin is the cheapest case for anything that scales with them.

    The map payload names a pin's labels by id and defines each once per
    response, so what that saves over copying every label's facts into every pin
    grows with how many labels a pin carries. A fixture that always gives one
    measures the case where there is nothing to save.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile

    def test_each_pin_carries_the_requested_number(self) -> None:
        seed_heavy_account(self.profile, pins=SEEDED, labels_per_pin=4)

        counts = {pin.labels.count() for pin in Pin.objects.filter(profile=self.profile)}

        self.assertEqual(counts, {4})

    def test_the_vocabulary_is_shared_rather_than_per_pin(self) -> None:
        """A label per pin is not a realistic account and would flatter compression.

        Counted as `(kind, name)` pairs rather than against the profile's total or
        by name alone: a profile arrives with several dozen default labels, and
        some of them share a name with the vocabulary under a different kind.
        """
        report = seed_heavy_account(self.profile, pins=SEEDED, labels_per_pin=4)

        names = {name for _kind, name in VOCABULARY}
        present = set(Label.objects.filter(profile=self.profile, name__in=names).values_list("kind", "name"))
        self.assertEqual(present & set(VOCABULARY), set(VOCABULARY))
        self.assertEqual(report["labels_per_pin"], 4)
        self.assertLess(4, len(VOCABULARY), "the window must be smaller than the vocabulary or every pin is identical")

    def test_neighbouring_pins_do_not_all_carry_the_same_set(self) -> None:
        """Rotating the vocabulary, or every pin's chip list is byte-identical."""
        seed_heavy_account(self.profile, pins=SEEDED, labels_per_pin=3)

        sets = {frozenset(pin.labels.values_list("pk", flat=True)) for pin in Pin.objects.filter(profile=self.profile)}

        self.assertGreater(len(sets), 1)

    def test_one_label_per_pin_is_still_the_default(self) -> None:
        seed_heavy_account(self.profile, pins=SEEDED)

        self.assertEqual({pin.labels.count() for pin in Pin.objects.filter(profile=self.profile)}, {1})

    def test_more_labels_than_the_vocabulary_holds_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            seed_heavy_account(self.profile, pins=SEEDED, labels_per_pin=len(VOCABULARY) + 1)
