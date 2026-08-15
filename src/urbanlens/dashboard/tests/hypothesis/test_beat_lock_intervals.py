"""Each beat task's overlap lock must expire before its next scheduled tick.

These tasks guard themselves with ``cache.add(key, ..., timeout=TTL)`` so two
runs can't overlap, and the TTLs are hand-tuned to sit "just under" the beat
interval - the comments in ``tasks.py`` say so explicitly. But the TTLs are
integers in ``tasks.py`` and the intervals are integers in
``settings/base.py``, with nothing tying them together.

Both directions of drift fail silently, and the quiet one is the dangerous one:

- TTL >= interval: every tick that lands while the previous lock is still held
  is skipped outright. Drop the safety check-in interval to 4 minutes and the
  270s lock silently halves the rate at which overdue check-ins escalate. No
  error, no log - just a feature running at half speed.
- TTL much shorter than the task's real runtime: two runs overlap. That is the
  failure the lock exists to prevent.

The completeness test is the one that keeps this honest: a new lock-guarded
beat task must be added to the map below or it fails here, rather than being
silently skipped by a test that only knows about the tasks someone remembered.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import tasks as tasks_module

_TASKS_PATH = Path(tasks_module.__file__)

def _takes_a_lock(node: ast.FunctionDef) -> bool:
    """Whether a task guards itself with an overlap lock.

    Matches both idioms: the raw ``cache.add(...)`` these tasks used to inline,
    and ``services.core.locks``' ``acquire_lock``/``beat_lock``, which replaced it
    so an overrunning run stops releasing its successor's lock. Matching only one
    would make this scan quietly find nothing - which is what
    ``test_the_scan_still_finds_the_known_locks`` exists to catch.

    Args:
        node: A function definition from ``tasks.py``.

    Returns:
        Whether the function body takes a lock.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if getattr(sub.func, "attr", None) == "add" and getattr(getattr(sub.func, "value", None), "id", None) == "cache":
            return True
        if getattr(sub.func, "id", None) in {"acquire_lock", "beat_lock"}:
            return True
    return False


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
}


def _effective_period_seconds(schedule) -> int:
    """The seconds between firings for an interval or crontab schedule.

    The staggered schedule (audit chunk 483) moved hourly/daily entries to
    ``crontab``: a single-minute crontab over all hours fires hourly; one
    pinned to specific hour(s) fires daily per listed hour. Interval entries
    stay plain seconds.
    """
    from celery.schedules import crontab

    if isinstance(schedule, crontab):
        if len(schedule.hour) == 24:
            return 60 * 60
        return (24 * 60 * 60) // max(len(schedule.hour), 1)
    return int(schedule)


class BeatLockIntervalTests(SimpleTestCase):
    def test_every_lock_expires_before_the_next_tick(self) -> None:
        too_long = {}
        for entry, ttl in _LOCKED_BEAT_TASKS.items():
            interval = _effective_period_seconds(settings.CELERY_BEAT_SCHEDULE[entry]["schedule"])
            if ttl >= interval:
                too_long[entry] = f"lock {ttl}s >= interval {interval}s - ticks will be silently skipped"

        self.assertEqual(too_long, {})

    def test_every_beat_entry_named_here_still_exists(self) -> None:
        """A renamed or deleted schedule entry must not leave a check pointing at nothing."""
        unknown = sorted(set(_LOCKED_BEAT_TASKS) - set(settings.CELERY_BEAT_SCHEDULE))

        self.assertEqual(unknown, [])

    def test_every_beat_scheduled_task_that_takes_a_lock_is_covered(self) -> None:
        """The completeness arm: a new lock-guarded beat task must be added above.

        Finds every function in ``tasks.py`` that calls ``cache.add(...)`` - the
        overlap-guard idiom - and checks that the ones reachable from the beat
        schedule appear in the map. Without this, adding an eleventh scheduled
        sweep would silently get no interval check at all.
        """
        tree = ast.parse(_TASKS_PATH.read_text())
        guarded = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and _takes_a_lock(node)
        }
        scheduled_names = {entry["task"].rsplit(".", 1)[-1]: name for name, entry in settings.CELERY_BEAT_SCHEDULE.items()}
        uncovered = sorted(scheduled_names[fn] for fn in guarded & set(scheduled_names) if scheduled_names[fn] not in _LOCKED_BEAT_TASKS)

        self.assertEqual(uncovered, [], "lock-guarded beat tasks with no interval check - add them to _LOCKED_BEAT_TASKS")

    def test_the_scan_still_finds_the_known_locks(self) -> None:
        """Guard against the AST scan quietly matching nothing after a refactor."""
        tree = ast.parse(_TASKS_PATH.read_text())
        guarded = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and _takes_a_lock(node)
        )

        self.assertGreaterEqual(guarded, len(_LOCKED_BEAT_TASKS))
