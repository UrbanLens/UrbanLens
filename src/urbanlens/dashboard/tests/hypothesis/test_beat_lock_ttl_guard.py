"""Fail the build when a beat task's overlap-lock TTL stops matching its schedule.

``services.core.locks.acquire_lock``'s docstring states the constraint on the TTL
its callers pass: it "should sit just under the task's beat interval, so a tick
is never skipped by a lock the previous run has already finished with". Eleven
beat tasks take that lock and all eleven satisfy it today (audit chunk 542).

Nothing enforced it, and the two halves live in different files - the TTL
constants in ``dashboard/tasks.py``, the intervals in
``UrbanLens/settings/base.py``. Retuning a schedule without touching its lock
constant breaks the invariant silently, and it fails invisibly in *both*
directions:

* **TTL >= interval** - a run killed mid-flight leaves a lock that outlives the
  gap, so the next tick (or several) is skipped. On the safety sweeps that is a
  missed check-in escalation.
* **TTL much below the real runtime** - the lock expires while the run is still
  going and the overlap it exists to prevent happens anyway, so the lock is
  decoration.

Either symptom - an occasional skipped tick, an occasional double run - reads as
flakiness rather than as a configuration error, which is exactly why it wants a
test rather than a note.

Both sides are read *live* (the imported module's constants, Django's own
``CELERY_BEAT_SCHEDULE``) rather than re-parsed from source, so the test checks
the values the workers actually run with. Only the task-to-lock mapping is taken
from the AST, because that association exists nowhere else.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks as tasks_module

#: Resolved from this file rather than the cwd - the checkout path differs
#: between the host and the app container.
TASKS_PATH = Path(tasks_module.__file__).resolve()

#: Lowest fraction of its interval a TTL may be before it is more likely to
#: expire mid-run than to prevent an overlap. Every current lock sits at 90-92%.
_MIN_FRACTION_OF_INTERVAL = 0.5


def _locked_tasks() -> dict[str, str]:
    """Map each module-level task function to the TTL name it locks with.

    Returns:
        ``{function name: ttl constant name}`` for every function whose body
        calls ``acquire_lock``.
    """
    tree = ast.parse(TASKS_PATH.read_text())
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "acquire_lock"):
                continue
            if len(call.args) == 2:
                found[node.name] = ast.unparse(call.args[1])
    return found


def _interval_seconds(schedule: object) -> float | None:
    """The gap between two consecutive firings of *schedule*, in seconds.

    Args:
        schedule: A ``CELERY_BEAT_SCHEDULE`` entry's ``schedule`` value - either
            a number of seconds, a ``timedelta``, or a celery ``crontab``.

    Returns:
        The interval, or None when it cannot be determined.
    """
    if isinstance(schedule, (int, float)):
        return float(schedule)
    if isinstance(schedule, datetime.timedelta):
        return schedule.total_seconds()
    # A crontab. `remaining_estimate` is deliberately not used: it answers
    # "how long until the next fire, from *now*", so calling it twice compounds
    # rather than stepping, which produced negative intervals when this test
    # first tried it. The cardinality of the field sets gives the gap directly
    # for every regular pattern, which is all this schedule uses.
    minutes = getattr(schedule, "minute", None)
    hours = getattr(schedule, "hour", None)
    if minutes is None or hours is None:
        return None
    if len(minutes) == 1 and len(hours) == 24:
        return 3600.0  # a fixed minute of every hour
    if len(minutes) == 1 and len(hours) == 1:
        return 86400.0  # a fixed time once a day
    if len(minutes) > 1 and len(hours) == 24 and 60 % len(minutes) == 0:
        return 3600.0 / len(minutes)  # an evenly-spaced */N minute step
    return None


def _scheduled_intervals() -> dict[str, float]:
    """Map each beat-scheduled task function name to its interval in seconds."""
    intervals: dict[str, float] = {}
    for entry in settings.CELERY_BEAT_SCHEDULE.values():
        name = str(entry["task"]).rsplit(".", 1)[-1]
        seconds = _interval_seconds(entry["schedule"])
        if seconds is not None:
            intervals[name] = seconds
    return intervals


class BeatLockTtlGuardTests(SimpleTestCase):
    def test_every_locked_beat_task_ttl_sits_under_its_interval(self) -> None:
        """The constraint acquire_lock's own docstring states."""
        intervals = _scheduled_intervals()
        offenders = []
        for task_name, ttl_name in _locked_tasks().items():
            if task_name not in intervals:
                continue  # locked but not beat-scheduled - nothing to compare against
            ttl = getattr(tasks_module, ttl_name, None) if not ttl_name.isdigit() else int(ttl_name)
            interval = intervals[task_name]
            if ttl is None:
                offenders.append(f"{task_name}: TTL {ttl_name} is not a module constant")
            elif ttl >= interval:
                offenders.append(f"{task_name}: TTL {ttl}s >= interval {interval:.0f}s - a killed run would skip the next tick")

        self.assertEqual(offenders, [], "beat lock TTLs must sit just under their schedule interval")

    def test_no_ttl_is_so_short_it_would_expire_mid_run(self) -> None:
        """The other direction: a lock that lapses mid-run prevents nothing."""
        intervals = _scheduled_intervals()
        offenders = []
        for task_name, ttl_name in _locked_tasks().items():
            if task_name not in intervals:
                continue
            ttl = getattr(tasks_module, ttl_name, None) if not ttl_name.isdigit() else int(ttl_name)
            interval = intervals[task_name]
            if ttl is not None and ttl < interval * _MIN_FRACTION_OF_INTERVAL:
                offenders.append(f"{task_name}: TTL {ttl}s is only {ttl / interval:.0%} of its {interval:.0f}s interval")

        self.assertEqual(offenders, [], "a TTL far below the interval expires mid-run and the overlap happens anyway")

    # -- guard the guard ----------------------------------------------------
    # The bulk-write guard's first version scanned zero files and passed
    # vacuously. These two make that failure mode loud.

    def test_the_scan_still_finds_locked_tasks(self) -> None:
        locked = _locked_tasks()
        self.assertGreaterEqual(len(locked), 10, f"the acquire_lock scan found only {len(locked)} tasks - it has stopped matching")

    def test_the_schedule_still_resolves_intervals(self) -> None:
        intervals = _scheduled_intervals()
        self.assertGreaterEqual(len(intervals), 20, f"only {len(intervals)} beat intervals resolved - crontab handling has drifted")
        self.assertTrue(all(seconds > 0 for seconds in intervals.values()))

    def test_locked_and_scheduled_sets_actually_overlap(self) -> None:
        """Without this, both assertions above could pass over an empty comparison."""
        compared = set(_locked_tasks()) & set(_scheduled_intervals())
        self.assertGreaterEqual(len(compared), 10, f"only {len(compared)} tasks are both locked and scheduled - the mapping broke")
