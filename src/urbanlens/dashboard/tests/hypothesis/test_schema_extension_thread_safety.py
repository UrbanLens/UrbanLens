"""Concurrent schema requests must not race drf-spectacular's target_class resolution.

`OpenApiGeneratorExtension._load_class` (drf_spectacular/plumbing.py) resolves
an extension's `target_class` from a dotted string to the class object by
mutating that class attribute in place, with no lock of its own. Two
gevent-concurrent requests both resolving extensions can interleave, and one
can observe the other's `target_class` mid-mutation - e.g. as the `None` a
failed-import branch just wrote - raising `AttributeError: 'NoneType' object
has no attribute 'startswith'` instead of producing a schema.

`external_api.schema.patch_extension_thread_safety` (applied once from
`DashboardConfig.ready()`) wraps `_load_class` in a lock. This proves the
installed wrapper actually holds that lock for the duration of the real
resolution - the property the whole fix depends on - rather than trying to
reproduce the original timing-dependent 500 directly.

See PROBLEMS.md, "concurrent requests to schema/ can 500".
"""

from __future__ import annotations

import threading
from unittest import mock

from django.test import SimpleTestCase

from urbanlens.dashboard.external_api import schema as schema_module


def _fake_extension(target_class):
    """A fresh, throwaway class shaped like an OpenApiGeneratorExtension subclass.

    `_load_class` only touches `target_class`/`optional` on whatever `cls` it
    is given - real inheritance is not required - so this proves the
    wrapper's locking behavior without registering a spurious entry in the
    real extension registry, which every other schema test in the suite
    shares. A fresh class per call, rather than one shared constant, keeps
    the two tests below from seeing each other's mutations.
    """
    return type("FakeExtension", (), {"target_class": target_class, "optional": True})


class LoadClassIsSerializedTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        schema_module.patch_extension_thread_safety()
        from drf_spectacular.plumbing import OpenApiGeneratorExtension

        self._load_class = OpenApiGeneratorExtension.__dict__["_load_class"].__func__

    def test_a_second_caller_cannot_acquire_the_lock_mid_resolution(self) -> None:
        from drf_spectacular import plumbing as plumbing_module

        entered = threading.Event()
        release = threading.Event()
        real_import_string = plumbing_module.import_string

        def _slow_import_string(dotted_path):
            entered.set()
            release.wait(timeout=5)
            return real_import_string(dotted_path)

        fake = _fake_extension("threading.Lock")

        with mock.patch.object(plumbing_module, "import_string", side_effect=_slow_import_string):
            worker = threading.Thread(target=self._load_class, args=(fake,))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5), "resolution never started")
                # The worker must already be holding the lock at this point.
                got_it = schema_module._extension_load_lock.acquire(blocking=False)
                self.assertFalse(got_it, "a second caller acquired the lock while resolution was in flight")
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "resolution never completed")
        # The lock is free again, and resolution actually completed correctly
        # rather than the wrapper just permanently blocking everyone.
        self.assertTrue(schema_module._extension_load_lock.acquire(blocking=False))
        schema_module._extension_load_lock.release()
        self.assertIs(fake.target_class, threading.Lock)

    def test_an_already_resolved_class_is_not_reprocessed(self) -> None:
        """Anti-vacuity: the wrapper does real work, it doesn't just always block."""
        fake = _fake_extension(threading.Lock)  # already resolved, not a string

        with mock.patch("drf_spectacular.plumbing.import_string") as mocked:
            self._load_class(fake)

        mocked.assert_not_called()


class PatchExtensionThreadSafetyIsIdempotentTests(SimpleTestCase):
    def test_calling_it_twice_keeps_the_same_wrapper_installed(self) -> None:
        from drf_spectacular.plumbing import OpenApiGeneratorExtension

        schema_module.patch_extension_thread_safety()
        first = OpenApiGeneratorExtension.__dict__["_load_class"].__func__

        schema_module.patch_extension_thread_safety()
        second = OpenApiGeneratorExtension.__dict__["_load_class"].__func__

        self.assertIs(first, second)
