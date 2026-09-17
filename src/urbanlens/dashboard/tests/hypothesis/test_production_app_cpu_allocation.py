"""P125: the app container is CPU-bound under concurrent load, and production has cores to spare.

`capacity-content` (2026-09-15) found the app container pegged at `docker-compose.yml`'s shared
`CPU_LIMIT__APP` default of 2 cores - 84.41% throttled at 1,000 concurrent users, uniformly across
every endpoint, which is the signature of a starved shared resource rather than any one slow query.

Production and staging/dev share a host (P114), so raising the *shared* default would hand staging
and every local checkout the same increase and let them compete with production for it - exactly
what P114 exists to prevent. Production alone gets more, via its own override, the same mechanism
`staging.sample.env` already uses to make staging lose.
"""

from __future__ import annotations

import pathlib
import re

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_PRODUCTION_SAMPLE_PATH = _REPO_ROOT / "src" / "urbanlens" / "config" / "env" / "production.sample.env"
_STAGING_SAMPLE_PATH = _REPO_ROOT / "src" / "urbanlens" / "config" / "env" / "staging.sample.env"

#: The floor P125 asked for.
_PRODUCTION_APP_CPU_MINIMUM = 4.0

#: The shared default every other environment (local, dev, testing, and staging unless it
#: overrides lower) must not exceed.
_SHARED_APP_CPU_DEFAULT = 2.0


def _sample_values(path: pathlib.Path) -> dict[str, str]:
    """One `key=value` sample env file, parsed.

    Args:
        path: The sample file to read.

    Returns:
        Variable name to value.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


#: Same two-pass parse `test_staging_limits_are_below_production.py` uses: capture to end of
#: line first, because the nested `${CPU_LIMIT:-2}}` spelling closes with two `}` in a row and a
#: single non-greedy `[^}]+` stops at the first one.
_VARIABLE = re.compile(r"\$\{([A-Z_]+):-([^\n]+)")
_NESTED_DEFAULT = re.compile(r"\$\{[A-Z_]+:-([^}]+)\}")


def _compose_default(name: str) -> str:
    """`docker-compose.yml`'s own default for one variable, e.g. `CPU_LIMIT__APP`.

    Args:
        name: The variable name, without the `${...}` wrapper.

    Returns:
        The default, with a nested `${CPU_LIMIT:-n}` fallback flattened to `n`.
    """
    for match in _VARIABLE.finditer(_COMPOSE_PATH.read_text(encoding="utf-8")):
        if match.group(1) != name:
            continue
        raw = match.group(2).strip()
        nested = _NESTED_DEFAULT.search(raw)
        return nested.group(1) if nested else raw.rstrip("}")
    raise AssertionError(f"docker-compose.yml no longer reads {name}")


class ProductionGetsMoreAppCpuTests(SimpleTestCase):
    """The point of `production.sample.env`, stated as the comparison it exists to win."""

    def test_production_sample_env_exists(self) -> None:
        self.assertTrue(
            _PRODUCTION_SAMPLE_PATH.exists(),
            "production.sample.env is missing - P125 needs production's app tier to differ from "
            "the shared default, and nothing else should carry that override",
        )

    def test_production_app_cpu_limit_is_at_least_four_cores(self) -> None:
        production = _sample_values(_PRODUCTION_SAMPLE_PATH)
        self.assertIn("CPU_LIMIT__APP", production)
        self.assertGreaterEqual(
            float(production["CPU_LIMIT__APP"]),
            _PRODUCTION_APP_CPU_MINIMUM,
            f"production's CPU_LIMIT__APP={production.get('CPU_LIMIT__APP')} is below the P125 floor "
            f"of {_PRODUCTION_APP_CPU_MINIMUM}",
        )

    def test_the_shared_default_is_still_two_cores(self) -> None:
        """Local, dev and testing read the shared default directly - it must not have moved."""
        self.assertEqual(
            _compose_default("CPU_LIMIT__APP"),
            "2",
            "docker-compose.yml's shared CPU_LIMIT__APP default changed - that raises every "
            "environment that does not override it, not just production",
        )

    def test_staging_app_cpu_limit_did_not_move_up_to_match_production(self) -> None:
        staging = _sample_values(_STAGING_SAMPLE_PATH)
        production = _sample_values(_PRODUCTION_SAMPLE_PATH)
        self.assertLessEqual(
            float(staging["CPU_LIMIT__APP"]),
            _SHARED_APP_CPU_DEFAULT,
            "staging's app CPU limit rose above the shared default - P125 asked for production only",
        )
        self.assertLess(
            float(staging["CPU_LIMIT__APP"]),
            float(production["CPU_LIMIT__APP"]),
            "staging must keep losing to production under contention, same as P114",
        )
