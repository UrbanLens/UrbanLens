"""The mechanism behind the ``test_search_does_not_read_another_accounts_*`` family, on its own.

Those files each drive one provider and assert the outcome: growing a stranger's rows must not make
the viewer's search read more. They cannot say *why* it holds, so a rewrite that keeps one path
bounded and silently drops another to a join passes every one of them that happens not to cover the
dropped path. This covers the mechanism directly:

* every relation shape search actually filters through decomposes, and decomposes to the same
  answer a join gives (:func:`probe_relation` against :func:`probe_statement`);
* a condition that names both sides refuses rather than answering wrongly;
* ``__anyof`` selects the same rows as ``__in`` while sending one parameter instead of N, which is
  what makes the bound affordable enough to use everywhere.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import EmptyResultSet
from django.db.models import Q
from model_bakery import baker

from urbanlens.core.semijoin import probe_relation, probe_scope, probe_statement, resolve_crossing, restate
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.wiki.model import Wiki

#: What the viewer's own rows match.
TERM = "quokka"


class ResolveCrossingTests(TestCase):
    """Which step of a path a probe drives from, for each relation shape search uses."""

    def test_a_reverse_foreign_key_is_the_first_step(self) -> None:
        crossing = resolve_crossing(Pin, "aliases__name")
        assert crossing is not None
        self.assertEqual(
            (crossing.prefix, crossing.related_model, crossing.back_field, crossing.strip, crossing.rewrite),
            ("", PinAlias, "pin", "aliases__", ""),
        )

    def test_a_reverse_foreign_key_behind_to_one_steps_keeps_them_as_the_prefix(self) -> None:
        crossing = resolve_crossing(Pin, "location__wiki__aliases__name")
        assert crossing is not None
        self.assertEqual(
            (crossing.prefix, crossing.related_model, crossing.back_field, crossing.strip),
            ("location__wiki", WikiAlias, "wiki", "location__wiki__aliases__"),
        )

    def test_a_many_to_many_resolves_to_its_join_table(self) -> None:
        crossing = resolve_crossing(Pin, "labels__name")
        assert crossing is not None
        self.assertEqual(crossing.related_model, Pin.labels.through)
        self.assertEqual((crossing.prefix, crossing.strip), ("", "labels__"))
        self.assertEqual(crossing.related_model._meta.get_field(crossing.back_field).related_model, Pin)  # noqa: SLF001
        self.assertEqual(
            crossing.related_model._meta.get_field(crossing.rewrite.removesuffix("__")).related_model, Label
        )  # noqa: SLF001

    def test_a_path_that_never_leaves_the_model_has_no_crossing(self) -> None:
        self.assertIsNone(resolve_crossing(Pin, "name"))
        self.assertIsNone(resolve_crossing(Pin, "location__official_name"))

    def test_an_unknown_path_has_no_crossing(self) -> None:
        self.assertIsNone(resolve_crossing(Pin, "not_a_field__name"))


class RestateTests(TestCase):
    """Rewriting a condition onto the table the probe drives from, or refusing to."""

    def test_it_strips_the_crossing_from_every_key(self) -> None:
        rewritten = restate(Q(aliases__name__icontains=TERM) | Q(aliases__name__iexact=TERM), "aliases__")
        assert rewritten is not None
        self.assertEqual({child[0] for child in rewritten.children}, {"name__icontains", "name__iexact"})

    def test_it_re_anchors_a_many_to_many_onto_the_join_tables_own_key(self) -> None:
        rewritten = restate(Q(labels__name__icontains=TERM), "labels__", "label__")
        assert rewritten is not None
        self.assertEqual([child[0] for child in rewritten.children], ["label__name__icontains"])

    def test_it_keeps_the_connector_and_the_negation(self) -> None:
        rewritten = restate(~(Q(aliases__name=TERM) | Q(aliases__slug=TERM)), "aliases__")
        assert rewritten is not None
        self.assertTrue(rewritten.negated)
        self.assertEqual(rewritten.connector, Q.OR)

    def test_it_refuses_a_condition_naming_both_sides(self) -> None:
        self.assertIsNone(restate(Q(aliases__name__icontains=TERM) | Q(name__icontains=TERM), "aliases__"))

    def test_it_refuses_a_key_that_is_only_the_crossing(self) -> None:
        self.assertIsNone(restate(Q(aliases=1), "aliases__"))


class _ProbeCase(TestCase):
    """One viewer with a row reachable through each shape, and a stranger with more of each."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile

        location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.wiki = baker.make(Wiki, location=location)
        self.mine = baker.make(Pin, profile=self.viewer, location=location, name="mine")
        baker.make(PinAlias, pin=self.mine, name=f"{TERM} alias")
        baker.make(PinNote, pin=self.mine, text=f"{TERM} note")
        baker.make(WikiAlias, wiki=self.wiki, name=f"{TERM} wiki alias")
        self.mine.labels.add(baker.make(Label, name=f"{TERM} label", profile=self.viewer))

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        their_wiki = baker.make(Wiki, location=their_location)
        theirs = baker.make(Pin, profile=self.stranger, location=their_location, name="theirs")
        for index in range(30):
            baker.make(PinAlias, pin=theirs, name=f"{TERM} alias {index}")
            baker.make(PinNote, pin=theirs, text=f"{TERM} note {index}")
            baker.make(WikiAlias, wiki=their_wiki, name=f"{TERM} wiki alias {index}")
            theirs.labels.add(baker.make(Label, name=f"{TERM} label {index}", profile=self.stranger))

        self.own_pks = list(Pin.objects.filter(profile=self.viewer).values_list("pk", flat=True))

    def both_ways(self, path: str) -> tuple[list, list]:
        """What the probe and the join each answer for ``path__icontains=TERM``."""
        condition = Q(**{f"{path}__icontains": TERM})
        with probe_scope():
            return probe_relation(Pin, path, condition, self.own_pks), probe_statement(Pin, condition, self.own_pks)


class ProbeRelationAgreesWithTheJoinTests(_ProbeCase):
    """Every shape decomposes, and decomposes to the join's answer."""

    def test_a_reverse_foreign_key(self) -> None:
        self.assertProbeAgrees("aliases__name")

    def test_a_second_reverse_foreign_key_on_the_same_model(self) -> None:
        self.assertProbeAgrees("notes__text")

    def test_a_reverse_foreign_key_behind_two_to_one_steps(self) -> None:
        self.assertProbeAgrees("location__wiki__aliases__name")

    def test_a_many_to_many(self) -> None:
        self.assertProbeAgrees("labels__name")

    def assertProbeAgrees(self, path: str) -> None:
        probed, joined = self.both_ways(path)
        self.assertIsNotNone(probed, f"{path} fell back to a join, so it is unbounded again")
        self.assertEqual(sorted(probed or []), sorted(joined))
        self.assertEqual(
            sorted(joined),
            [self.mine.pk],
            f"{path} matched {joined}, so the comparison above was between two empty answers",
        )

    def test_it_finds_nothing_when_nothing_matches(self) -> None:
        condition = Q(aliases__name__icontains="wombat")
        with probe_scope():
            self.assertEqual(probe_relation(Pin, "aliases__name", condition, self.own_pks), [])

    def test_a_condition_naming_both_sides_falls_back_rather_than_answering(self) -> None:
        condition = Q(aliases__name__icontains=TERM) | Q(name="mine")
        with probe_scope():
            self.assertIsNone(probe_relation(Pin, "aliases__name", condition, self.own_pks))


class AnyOfTests(_ProbeCase):
    """The bound itself: same rows as ``__in``, one parameter instead of one per element."""

    def test_it_selects_the_same_rows_as_in(self) -> None:
        pks = list(Pin.objects.values_list("pk", flat=True))
        self.assertEqual(
            sorted(Pin.objects.filter(pk__anyof=pks).values_list("pk", flat=True)),
            sorted(Pin.objects.filter(pk__in=pks).values_list("pk", flat=True)),
        )

    def test_it_sends_one_array_however_long_the_list(self) -> None:
        pks = list(Pin.objects.values_list("pk", flat=True))
        self.assertGreater(len(pks), 1, "the population is too small for this to distinguish the two forms")
        sql, params = Pin.objects.filter(pk__anyof=pks).query.sql_with_params()
        self.assertIn("= ANY('{", sql)
        self.assertEqual(
            params, (), f"one bound sent {len(params)} parameters; the point of the lookup is that it sends none"
        )
        self.assertIn(",".join(str(pk) for pk in pks), sql)

    def test_the_planner_can_see_the_values(self) -> None:
        """Bound as a parameter the array is opaque when the statement is planned, and a small
        table then tips towards a scan of whatever the filter names. See the module docstring."""
        pks = list(Pin.objects.values_list("pk", flat=True))
        sql, _ = Pin.objects.filter(pk__anyof=pks).query.sql_with_params()
        self.assertNotIn("= ANY(%s)", sql)

    def test_a_non_integer_bound_stays_parameterised(self) -> None:
        uuids = [str(uuid) for uuid in Pin.objects.values_list("uuid", flat=True)]
        sql, params = Pin.objects.filter(uuid__anyof=uuids).query.sql_with_params()
        self.assertNotIn("ANY", sql)
        self.assertEqual(
            len(params), len(uuids), "a non-integer bound was written into the statement rather than bound to it"
        )

    def test_it_follows_a_relation_by_primary_key_without_joining(self) -> None:
        sql, _ = PinAlias.objects.filter(pin__pk__anyof=self.own_pks).query.sql_with_params()
        self.assertNotIn(
            "JOIN",
            sql.upper(),
            "bounding by a foreign key's primary key added a join to reach a column already on the row",
        )

    def test_an_empty_bound_matches_nothing(self) -> None:
        self.assertEqual(list(Pin.objects.filter(pk__anyof=[])), [])

    def test_an_empty_bound_never_reaches_the_database(self) -> None:
        with self.assertRaises(EmptyResultSet):
            Pin.objects.filter(pk__anyof=[]).query.sql_with_params()
