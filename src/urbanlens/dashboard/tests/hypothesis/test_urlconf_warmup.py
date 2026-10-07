"""Warming the URLconf means both halves of it, not just the import.

`_warm_urlconf` read `resolver.url_patterns`, which imports the URLconf module
and nothing else. Django builds the *reverse* table separately and lazily, on
the first `reverse()` or `{% url %}` - and since essentially every template
opens with a `{% url %}`, that work landed on the first request to reach any
view, in every process, for the life of the process.

Measured in-container: the import is 1.4-2.3s of CPU and the reverse table a
further 0.364s, built by normalising every pattern's regex (4,206 calls into
`django/utils/regex_helper.normalize`). Warming only the import left the first
`GET /` at 0.568s CPU against 0.005s warm; warming both takes it to 0.078s.

This corrects N22 H63, which recorded the cost as the template's lazy
`{% include %}` chain and concluded a fix would need a rendered request at
boot. That was wrong - `cProfile` sorted by cumulative time attributed it to
`rendered_content`, which is where the `{% url %}` sits, rather than to
`URLResolver._populate` underneath it. Sorting by self time named the real one.

The probes run in fresh interpreters. This one imported the URLconf and
populated its reverse table long before any test ran, so an in-process check
reports WARM whatever the code does.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

from urbanlens.core.tests.environment import probe_environ
from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

#: Reports each half of the warm-up independently. `_reverse_dict` is read
#: directly because the public `reverse_dict` *builds* the table on access -
#: touching it to ask whether it exists would make every run say yes.
_STATE = """from django.conf import settings
from django.urls import get_resolver
from django.utils.translation import get_language
resolver = get_resolver()
assert hasattr(resolver, "_reverse_dict"), "Django renamed _reverse_dict; this probe is blind"
print(f"imported={settings.ROOT_URLCONF in sys.modules} reversible={get_language() in resolver._reverse_dict}")
"""

_PRELUDE = """import sys, os
sys.path.insert(0, {src!r})
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")
import django
django.setup()
"""


def _run(body: str) -> str:
    """Report both halves' state in a fresh interpreter, after running `body`."""
    source = _PRELUDE.format(src=str(REPO_ROOT / "src")) + body + "\n" + _STATE
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
        env=probe_environ(),
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"probe failed:\n{result.stderr[-2500:]}")
    return result.stdout.strip().splitlines()[-1]


class TheWarmUpCoversBothHalvesTests(SimpleTestCase):
    def test_warming_imports_the_urlconf_and_builds_the_reverse_table(self) -> None:
        from urbanlens.core.warmup import warm_urlconf

        self.assertEqual(
            _run("from urbanlens.core.warmup import warm_urlconf; warm_urlconf()"), "imported=True reversible=True"
        )
        self.assertEqual(len(warm_urlconf()), 2, "warm_urlconf should report what it warmed, so a caller can log proof")

    def test_a_bare_setup_has_neither(self) -> None:
        """Anti-vacuity: without this, "imported=True reversible=True" could
        just be what `django.setup()` leaves behind."""
        self.assertEqual(_run(""), "imported=False reversible=False")

    def test_importing_the_urlconf_alone_leaves_the_reverse_table_cold(self) -> None:
        """The exact bug: the old warm-up did this and called it done."""
        self.assertEqual(
            _run("from django.urls import get_resolver; get_resolver().url_patterns"), "imported=True reversible=False"
        )


class TheEntrypointsUseItTests(SimpleTestCase):
    """Both servers warm the same way, because both paid the same cost."""

    def test_asgi_import_warms_both_halves(self) -> None:
        self.assertEqual(_run("import urbanlens.UrbanLens.asgi  # noqa: F401"), "imported=True reversible=True")

    def test_the_gunicorn_hook_warms_both_halves(self) -> None:
        hook = (
            "import importlib.util\n"
            "from types import SimpleNamespace\n"
            f"spec = importlib.util.spec_from_file_location('_conf', {str(REPO_ROOT / 'gunicorn.conf.py')!r})\n"
            "conf = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(conf)\n"
            "_noop = lambda *a, **k: None\n"
            "conf.post_worker_init(SimpleNamespace(pid=1, log=SimpleNamespace(info=_noop, exception=_noop)))\n"
        )

        self.assertEqual(_run(hook), "imported=True reversible=True")


class FreezingTheWarmHeapTests(SimpleTestCase):
    """P207: run in a fresh interpreter, since a freeze here would keep the test process's heap out of collections."""

    def test_a_frozen_heap_leaves_a_full_collection_little_to_walk(self) -> None:
        body = (
            "import gc\n"
            "from urbanlens.core.warmup import freeze_warm_heap, warm_urlconf\n"
            "warm_urlconf()\n"
            "gc.collect()\n"
            "before = len(gc.get_objects(2))\n"
            "frozen = freeze_warm_heap()\n"
            "print(before, frozen, gc.get_freeze_count(), len(gc.get_objects(2)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", _PRELUDE.format(src=str(REPO_ROOT / "src")) + body],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
            env=probe_environ(),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2500:])
        before, frozen, counted, left = (int(value) for value in result.stdout.strip().splitlines()[-1].split())

        self.assertGreater(frozen, 100_000)
        self.assertEqual(frozen, counted)
        self.assertLess(left, before // 100)

    def test_what_a_freeze_still_collects_and_what_it_never_will(self) -> None:
        """The docstring's safety claim: requests' cycles are still collected; one through a dead frozen object is not."""
        body = (
            "import gc, weakref\n"
            "class Node: pass\n"
            "kept, dropped = Node(), Node()\n"
            "gc.collect()\n"
            "gc.freeze()\n"
            "a, b = Node(), Node()\n"
            "a.other, b.other, a.frozen = b, a, kept\n"
            "fresh = weakref.ref(a)\n"
            "del a, b\n"
            "held = Node()\n"
            "held.back, dropped.held = dropped, held\n"
            "through = weakref.ref(held)\n"
            "del held, dropped\n"
            "gc.collect()\n"
            "print(fresh() is None, through() is None)\n"
        )
        result = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-2500:])

        self.assertEqual(result.stdout.split(), ["True", "False"])

    def test_a_pytest_process_freezes_nothing(self) -> None:
        import gc

        from urbanlens.core.warmup import freeze_warm_heap

        before = gc.get_freeze_count()

        self.assertEqual(freeze_warm_heap(), 0)
        self.assertEqual(gc.get_freeze_count(), before)
