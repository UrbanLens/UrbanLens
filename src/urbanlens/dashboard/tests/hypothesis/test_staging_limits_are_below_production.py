"""Staging and production share a host, so staging's limits have to lose to it.

Measured on damballa on 2026-09-11: `urbanlens_staging_app` and
`urbanlens_staging_db` each carried `CpuShares: 2048`, while every production
container carried `0` - the key was absent when they were created, so the kernel
gives them 1024. Under contention staging's web tier and database each had twice
production's weight. See P114.

Staging's numbers were exactly `docker-compose.yml`'s defaults, so nothing was
misconfigured: staging simply ran what the file says, and the file's defaults are
written for the stack that matters. `config/env/staging.sample.env` is the set of
values that are not, and this is what keeps it complete and keeps it lower - a
sample that drifts from the file it shadows is worse than none, because it reads
as coverage.

The weight ceiling is the part worth stating twice: half of 2048 is 1024, which
*ties* with a container that sets nothing rather than losing to it, and a tie is
what the measurement above found. So the assertion is against 1024 directly, not
against the file's default.
"""

from __future__ import annotations

import pathlib
import re

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_SAMPLE_PATH = _REPO_ROOT / "src" / "urbanlens" / "config" / "env" / "staging.sample.env"

#: What the kernel gives a container whose `cpu_shares` is unset, which is what
#: every production container on damballa currently is.
_UNSET_SHARES = 1024

#: The test stack, which never runs beside production.
_EXEMPT = "TEST_"

_VARIABLE = re.compile(r"\$\{(CPU_SHARES__[A-Z_]+|CPU_LIMIT__[A-Z_]+|MEM_LIMIT__[A-Z_]+):-([^\n]+)")
_NESTED_DEFAULT = re.compile(r"\$\{[A-Z_]+:-([^}]+)\}")


def _compose_defaults() -> dict[str, str]:
    """Every resource variable `docker-compose.yml` reads, with its default.

    Returns:
        Variable name to default value, with the nested ``${CPU_LIMIT:-n}``
        spelling flattened to its inner default.
    """
    defaults: dict[str, str] = {}
    for match in _VARIABLE.finditer(_COMPOSE_PATH.read_text(encoding="utf-8")):
        name, raw = match.group(1), match.group(2).strip()
        nested = _NESTED_DEFAULT.search(raw)
        defaults.setdefault(name, nested.group(1) if nested else raw.rstrip("}"))
    return {name: value for name, value in defaults.items() if _EXEMPT not in name}


def _sample_values() -> dict[str, str]:
    """The staging overrides, parsed.

    Returns:
        Variable name to value.
    """
    values: dict[str, str] = {}
    for raw in _SAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def _as_number(value: str) -> float:
    """One limit as a comparable number.

    Args:
        value: A share count, a CPU count, or a memory size like ``512m``.

    Returns:
        Shares and CPUs as themselves; memory in megabytes.
    """
    match = re.fullmatch(r"([\d.]+)([a-z]?)", value)
    if match is None:
        raise ValueError(f"unparseable limit: {value!r}")
    number, unit = float(match.group(1)), match.group(2)
    return number * 1024 if unit == "g" else number


class TheStagingSampleCoversTheFileTests(SimpleTestCase):
    """A sample that has drifted reads as coverage while providing none."""

    def test_the_premise_holds(self) -> None:
        """Both files parse into something; otherwise every test here is empty."""
        self.assertGreater(len(_compose_defaults()), 40)
        self.assertGreater(len(_sample_values()), 40)

    def test_every_variable_the_file_reads_is_in_the_sample(self) -> None:
        missing = sorted(set(_compose_defaults()) - set(_sample_values()))

        self.assertEqual(
            missing, [], f"docker-compose.yml reads {len(missing)} variable(s) staging does not set: {missing}"
        )

    def test_the_sample_sets_nothing_the_file_does_not_read(self) -> None:
        """A value nothing interpolates is a value somebody will trust."""
        unread = sorted(set(_sample_values()) - set(_compose_defaults()))

        self.assertEqual(unread, [], f"the sample sets {unread}, which docker-compose.yml never reads")


class StagingLosesToProductionTests(SimpleTestCase):
    """The point of the sample, stated as the comparison it exists to win."""

    def test_every_staging_limit_is_below_the_default(self) -> None:
        defaults, sample = _compose_defaults(), _sample_values()
        for name, default in sorted(defaults.items()):
            with self.subTest(name):
                self.assertLess(
                    _as_number(sample[name]),
                    _as_number(default),
                    f"{name}: staging {sample[name]} is not below the default {default}",
                )

    def test_every_staging_weight_is_below_an_unset_container(self) -> None:
        """Against 1024, not against the file's default.

        Production's containers carry no `cpu_shares` at all, so halving a 2048
        default gives staging a tie rather than a loss - which is precisely the
        state P114 measured.
        """
        for name, value in sorted(_sample_values().items()):
            if name.startswith("CPU_SHARES__"):
                with self.subTest(name):
                    self.assertLess(
                        _as_number(value), _UNSET_SHARES, f"{name}={value} ties or beats a container that sets nothing"
                    )
