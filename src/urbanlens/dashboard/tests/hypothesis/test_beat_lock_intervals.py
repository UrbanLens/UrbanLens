"""Each beat task's overlap lock must expire before its next scheduled tick."""

from __future__ import annotations

import ast
from pathlib import Path

from celery.schedules import crontab
from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks as tasks_module

_TASKS_PATH = Path(tasks_module.__file__)


def _takes_a_lock(node: ast.FunctionDef) -> bool:
    """Whether a task guards itself with an overlap lock.

    Matching only one would make this scan quietly find nothing - which is what
    ``test_the_scan_still_finds_the_known_locks`` exists to catch.

    Args:
        node: A function definition from ``tasks.py``.

    Returns:
        Whether the function body takes a lock."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if (
            getattr(sub.func, "attr", None) == "add"
            and getattr(getattr(sub.func, "value", None), "id", None) == "cache"
        ):
            return True
        if getattr(sub.func, "id", None) in {"acquire_lock", "beat_lock"}:
            return True
    return False


def _parse_function(source: str) -> ast.FunctionDef:
    """The function *source* defines first.

    Args:
        source: Python source starting with a function definition.

    Returns:
        Its definition.
    """
    node = ast.parse(source).body[0]
    if not isinstance(node, ast.FunctionDef):
        raise TypeError(f"{source!r} does not start with a function")
    return node


#: beat-schedule entry name -> the lock TTL (seconds) the task guards itself with.
#: Values are read live off ``tasks`` so this can't drift from the constants.
_LOCKED_BEAT_TASKS: dict[str, int] = {
    "scheduled-location-enrichment": 3300,
    "scheduled-trivia-generation": 3300,
    "scheduled-trivia-wiki-incorporation": 3300,
    "safety-checkin-due-reminders": tasks_module._CHECKIN_LOCK_TIMEOUT_SECONDS,
    "safety-checkin-final-warnings": tasks_module._CHECKIN_LOCK_TIMEOUT_SECONDS,
    "safety-checkin-escalation": tasks_module._CHECKIN_LOCK_TIMEOUT_SECONDS,
    "safety-checkin-archival-sweep": tasks_module._CHECKIN_LOCK_TIMEOUT_SECONDS,
    "spotguessr-stall-sweep": tasks_module._SPOTGUESSR_STALL_SWEEP_LOCK_TIMEOUT_SECONDS,
    "trivia-stall-sweep": tasks_module._TRIVIA_STALL_SWEEP_LOCK_TIMEOUT_SECONDS,
    "consensus-stall-sweep": tasks_module._CONSENSUS_STALL_SWEEP_LOCK_TIMEOUT_SECONDS,
    "account-deletion-reminders": tasks_module._DELETION_REMINDER_LOCK_TIMEOUT_SECONDS,
    "account-deletion-hard-delete": tasks_module._HARD_DELETE_LOCK_TIMEOUT_SECONDS,
    "task-outbox-drain": tasks_module._OUTBOX_DRAIN_LOCK_SECONDS,
    "public-pin-candidate-evaluation": tasks_module.PUBLIC_PIN_EVALUATION_LOCK_TIMEOUT_SECONDS,
    "public-media-cache-sweep": tasks_module._PUBLIC_MEDIA_SWEEP_LOCK_TIMEOUT_SECONDS,
}


def _effective_period_seconds(schedule: crontab | float) -> int:
    """The shortest gap, in seconds, between two firings of an interval or crontab schedule.

    A crontab's firings over one day are compared, wrapping midnight, so a crontab firing every hour gets the
    shortest gap between its minutes (wrapping the hour) and one limited to some hours the shortest gap between
    them. Day-of-week and day-of-month limits only lengthen a gap, so they are ignored.

    Args:
        schedule: A ``crontab``, or an interval in seconds.

    Returns:
        The shortest gap in seconds."""
    if isinstance(schedule, crontab):
        firings = sorted(hour * 60 + minute for hour in schedule.hour for minute in schedule.minute)
        wrapped = [*firings[1:], firings[0] + 24 * 60]
        return min(later - earlier for earlier, later in zip(firings, wrapped, strict=True)) * 60
    return int(schedule)


def _too_long_locks(locked_tasks: dict[str, int], beat_schedule: dict) -> dict[str, str]:
    """Every entry whose TTL does not expire before its next scheduled tick.

    Split out of the test body so the boundary itself (``ttl >= interval``) can be pinned directly with
    synthetic data, rather than only ever being exercised by production values that all sit comfortably clear of
    the line."""
    too_long = {}
    for entry, ttl in locked_tasks.items():
        interval = _effective_period_seconds(beat_schedule[entry]["schedule"])
        if ttl >= interval:
            too_long[entry] = f"lock {ttl}s >= interval {interval}s - ticks will be silently skipped"
    return too_long


class BeatLockIntervalTests(SimpleTestCase):
    def test_every_lock_expires_before_the_next_tick(self) -> None:
        too_long = _too_long_locks(_LOCKED_BEAT_TASKS, settings.CELERY_BEAT_SCHEDULE)

        self.assertEqual(too_long, {})

    def test_ttl_equal_to_the_interval_is_flagged(self) -> None:
        """The exact boundary: a lock held for the whole interval is still unsafe - the next tick lands the instant it expires, not after. A ``>`` in place of ``>=`` would let this slip through and still pass every other test here, since no real task's TTL happens to land exactly on its interval."""
        too_long = _too_long_locks({"fake-task": 300}, {"fake-task": {"schedule": 300}})

        self.assertEqual(set(too_long), {"fake-task"})

    def test_ttl_one_second_under_the_interval_is_not_flagged(self) -> None:
        """One tick below the boundary above must pass cleanly."""
        too_long = _too_long_locks({"fake-task": 299}, {"fake-task": {"schedule": 300}})

        self.assertEqual(too_long, {})

    def test_every_beat_entry_named_here_still_exists(self) -> None:
        """A renamed or deleted schedule entry must not leave a check pointing at nothing."""
        unknown = sorted(set(_LOCKED_BEAT_TASKS) - set(settings.CELERY_BEAT_SCHEDULE))

        self.assertEqual(unknown, [])

    def test_every_beat_scheduled_task_that_takes_a_lock_is_covered(self) -> None:
        """The completeness arm: a new lock-guarded beat task must be added above.

        Finds every function in ``tasks.py`` that calls ``cache.add(...)`` - the overlap-guard idiom - and
        checks that the ones reachable from the beat schedule appear in the map."""
        tree = ast.parse(_TASKS_PATH.read_text(encoding="utf-8"))
        guarded = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and _takes_a_lock(node)}
        scheduled_names = {
            entry["task"].rsplit(".", 1)[-1]: name for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
        }
        uncovered = sorted(
            scheduled_names[fn]
            for fn in guarded & set(scheduled_names)
            if scheduled_names[fn] not in _LOCKED_BEAT_TASKS
        )

        self.assertEqual(
            uncovered, [], "lock-guarded beat tasks with no interval check - add them to _LOCKED_BEAT_TASKS"
        )

    def test_the_scan_still_finds_the_known_locks(self) -> None:
        """Guard against the AST scan quietly matching nothing after a refactor."""
        tree = ast.parse(_TASKS_PATH.read_text(encoding="utf-8"))
        guarded = sum(1 for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and _takes_a_lock(node))

        self.assertGreaterEqual(guarded, len(_LOCKED_BEAT_TASKS))

    def test_takes_a_lock_matches_the_raw_cache_add_idiom(self) -> None:
        """Every current task has migrated to ``acquire_lock``/``beat_lock``, so this
        idiom has zero real matches in ``tasks.py`` today - nothing else in this
        file would notice if this arm silently broke.
        """
        node = _parse_function("def f():\n    cache.add('k', 'v', 30)\n")

        self.assertTrue(_takes_a_lock(node))

    def test_takes_a_lock_matches_acquire_lock(self) -> None:
        node = _parse_function("def f():\n    acquire_lock('k', 30)\n")

        self.assertTrue(_takes_a_lock(node))

    def test_takes_a_lock_matches_beat_lock(self) -> None:
        node = _parse_function("def f():\n    with beat_lock('k', 30) as got:\n        pass\n")

        self.assertTrue(_takes_a_lock(node))

    def test_takes_a_lock_is_false_for_an_unrelated_function(self) -> None:
        """The negative case: nothing here currently proves the matcher can say no.
        A ``_takes_a_lock`` that always returns ``True`` would still pass every
        other test in this file that only checks names already known to lock.
        """
        node = _parse_function("def f():\n    do_something_else()\n")

        self.assertFalse(_takes_a_lock(node))

    def test_effective_period_seconds_plain_interval_passes_through(self) -> None:
        self.assertEqual(_effective_period_seconds(300), 300)

    def test_effective_period_seconds_all_hours_crontab_is_hourly(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute=12)), 60 * 60)

    def test_effective_period_seconds_single_hour_crontab_is_daily(self) -> None:
        """No entry in ``_LOCKED_BEAT_TASKS`` uses a limited-hour crontab, so this
        branch of the helper is otherwise never exercised by production data.
        """
        self.assertEqual(_effective_period_seconds(crontab(hour=3, minute=10)), 24 * 60 * 60)

    def test_effective_period_seconds_multi_hour_crontab_divides_the_day(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute=32, hour="*/6")), 6 * 60 * 60)

    def test_effective_period_seconds_minute_list_is_the_shortest_gap(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute="13,28,43,58")), 15 * 60)

    def test_effective_period_seconds_minute_gap_wraps_the_hour(self) -> None:
        """50 then 5 past: the shortest gap is the 15 minutes across the hour, not the 45 inside it."""
        self.assertEqual(_effective_period_seconds(crontab(minute="5,50")), 15 * 60)

    def test_effective_period_seconds_uneven_hours_take_the_shortest_gap(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute=0, hour="0,6,8")), 2 * 60 * 60)

    def test_effective_period_seconds_hour_gap_wraps_the_day(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute=0, hour="2,23")), 3 * 60 * 60)

    def test_effective_period_seconds_minute_list_within_limited_hours(self) -> None:
        self.assertEqual(_effective_period_seconds(crontab(minute="0,30", hour="3,9")), 30 * 60)

    def test_a_lock_longer_than_a_sub_hourly_crontab_is_flagged(self) -> None:
        too_long = _too_long_locks({"fake-task": 1200}, {"fake-task": {"schedule": crontab(minute="13,28,43,58")}})

        self.assertEqual(set(too_long), {"fake-task"})
