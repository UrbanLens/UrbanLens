"""daphne must not leave the URLconf import for its first request to pay.

The gunicorn app warms the URLconf in `post_worker_init` because resolving it
reaches every controller and through them GeoPandas/Shapely - measured here at
2.26s of CPU, on top of 2.19s for `django.setup()`. daphne loads no gunicorn
config, and `asgi.py` imports only the *websocket* patterns, so the HTTP
URLconf stays unimported until something asks for an HTTP route.

Staging's first `/health/` on app-ws cost 4184ms wall and 4176ms CPU against
zero SQL, every restart. That container runs with `cpus: 1`, and CPU-bound
import work holds the GIL, so the event loop that serves every WebSocket on the
site is stalled for the duration - while the container has already been
reported healthy. It is the same defect `_warm_urlconf` was written for, in the
process that never got it.

Asserted in a fresh interpreter: this one imported the URLconf long before the
test ran, so an in-process check would pass whatever `asgi.py` does.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

PROBE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {src!r})
    import urbanlens.UrbanLens.asgi  # noqa: F401
    from django.conf import settings
    print("WARM" if settings.ROOT_URLCONF in sys.modules else "COLD")
    """,
)


def _probe() -> str:
    """Import the ASGI module in a clean interpreter and report what it left loaded."""
    source = PROBE.format(src=str(REPO_ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"the ASGI probe failed to run:\n{result.stderr[-2000:]}")
    return result.stdout.strip().splitlines()[-1]


class TheAsgiEntrypointWarmsTheUrlconfTests(SimpleTestCase):
    """Loading `asgi:application` must leave nothing expensive for request one."""

    def test_importing_asgi_imports_the_root_urlconf(self) -> None:
        self.assertEqual(_probe(), "WARM", "daphne's first HTTP request still pays the URLconf import")

    def test_the_probe_can_tell_the_difference(self) -> None:
        """Anti-vacuity: the probe must report COLD for a process that has only
        set Django up, or "WARM" would mean nothing."""
        source = textwrap.dedent(
            f"""
            import sys, os
            sys.path.insert(0, {str(REPO_ROOT / "src")!r})
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings")
            import django
            django.setup()
            from django.conf import settings
            print("WARM" if settings.ROOT_URLCONF in sys.modules else "COLD")
            """,
        )
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT), check=False
        )

        self.assertEqual(result.stdout.strip().splitlines()[-1], "COLD", f"stderr:\n{result.stderr[-1500:]}")

    def test_the_setting_names_a_real_module(self) -> None:
        """`ROOT_URLCONF in sys.modules` is the whole assertion, so a typo in
        the setting would make both tests above vacuous."""
        self.assertEqual(settings.ROOT_URLCONF, "urbanlens.UrbanLens.urls")
