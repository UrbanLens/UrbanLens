"""X26: a capacity run given a ready-made manifest seeded no tiles and measured 503s.

``run_capacity_tests.sh`` seeded the basemap tile cache inside its ``--provision-container``
branch, so the cheap way to re-measure - reuse the 1,000-account population already in the
database and pass ``--manifest`` - skipped seeding entirely. Every tile then missed, hit the
proxy's upstream slot bound and answered 503, and the report still rendered a full table of
plausible latencies. X26 records 8,360 of 8,352 tile requests answering 503 in exactly that way.

The run is driven here with stub ``docker`` and ``bun`` on ``PATH``, so what is asserted is the
command the script would have issued rather than the shape of its source.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import pathlib
import shutil
import subprocess
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_RUNNER = _REPO_ROOT / "bin" / "run_capacity_tests.sh"

#: Stands in for the manifest path, which is only known once the temporary directory exists.
_MANIFEST = "<manifest>"

#: A `bun -e` evaluating the grid out of `lib/capacity.js` is how the script and the k6 side are
#: kept from drifting; the stub answers what that expression prints.
_BUN_STUB = """#!/usr/bin/env bash
echo "--layer terrain --zoom 13 --origin-x 2400 --origin-y 3072 --size 32"
"""

#: Records every invocation and succeeds. `cp` writes its destination, which is how the
#: provisioning path gets a manifest; `logs` writes nothing, so the script's own `-f`/`-s` guards
#: skip the reporting steps that would need a real run's output.
_DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"${UL_TEST_DOCKER_LOG}"
if [[ "${1}" == "cp" ]]; then printf '%s' "${UL_TEST_MANIFEST_JSON}" >"${3}"; fi
exit 0
"""

#: What a provisioned manifest holds, as far as this script is concerned.
_MANIFEST_JSON = '{"kind": "population", "accounts": 1, "pins": 1, "routes": {}, "users": []}'


@dataclass(frozen=True)
class Run:
    """What one stubbed invocation of the runner did."""

    status: int
    stdout: str
    stderr: str
    docker: list[str]

    def seeding(self) -> list[str]:
        """The `docker` invocations that seeded the tile cache."""
        return [line for line in self.docker if "seed_basemap_tile_cache" in line]


class CapacityRunnerSeedsTilesTests(SimpleTestCase):
    """The tile cache is seeded for whichever population the run is about to measure."""

    def drive(self, *args: str) -> Run:
        """Run the capacity runner with stubbed `docker` and `bun`.

        Args:
            *args: Arguments after the fixed `--url` and `--out`. :data:`_MANIFEST` is replaced
                with the path of a manifest written for this run.

        Returns:
            What the run did.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = pathlib.Path(workspace)
            stubs = root / "stubs"
            stubs.mkdir()
            for name, body in (("docker", _DOCKER_STUB), ("bun", _BUN_STUB)):
                stub = stubs / name
                stub.write_text(body)
                stub.chmod(0o755)
            log = root / "docker.log"
            log.touch()
            manifest = root / "manifest.json"
            manifest.write_text(_MANIFEST_JSON)

            environment = dict(os.environ)
            environment["PATH"] = f"{stubs}{os.pathsep}{environment['PATH']}"
            environment["UL_TEST_DOCKER_LOG"] = str(log)
            environment["UL_TEST_MANIFEST_JSON"] = _MANIFEST_JSON
            resolved = [str(manifest) if argument == _MANIFEST else argument for argument in args]
            completed = subprocess.run(
                [
                    shutil.which("bash") or "bash",
                    str(_RUNNER),
                    "--url",
                    "http://localhost:31000",
                    "--out",
                    str(root / "out"),
                    *resolved,
                ],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                timeout=120,
            )
            return Run(completed.returncode, completed.stdout, completed.stderr, log.read_text().splitlines())

    def test_a_supplied_manifest_still_seeds_the_tile_cache(self) -> None:
        """The `--manifest` path seeds, or the run measures a cold cache and reports it as latency."""
        run = self.drive("--manifest", _MANIFEST, "--app-container", "ul_perf_app")
        self.assertEqual(run.status, 0, msg=f"{run.stdout}\n{run.stderr}")
        seeding = run.seeding()
        self.assertTrue(seeding, msg=f"no seeding among {run.docker}")
        self.assertIn("--layer terrain", seeding[0])
        self.assertIn("ul_perf_app", seeding[0])

    def test_provisioning_seeds_the_tile_cache(self) -> None:
        """The path that always seeded still does, and still only once."""
        run = self.drive("--provision-container", "ul_perf_app", "--population", "2")
        self.assertEqual(run.status, 0, msg=f"{run.stdout}\n{run.stderr}")
        self.assertEqual(len(run.seeding()), 1, msg=f"{run.docker}")

    def test_no_tiles_seeds_nothing(self) -> None:
        """`--no-tiles` prices the journey without them, so seeding would be wasted work."""
        run = self.drive("--manifest", _MANIFEST, "--app-container", "ul_perf_app", "--no-tiles")
        self.assertEqual(run.status, 0, msg=f"{run.stdout}\n{run.stderr}")
        self.assertEqual(run.seeding(), [])

    def test_tiles_without_a_container_to_seed_in_is_refused(self) -> None:
        """Nothing can seed, so the run would measure the proxy's refusals; say so instead."""
        run = self.drive("--manifest", _MANIFEST)
        self.assertNotEqual(run.status, 0)
        self.assertIn("seed", run.stderr.lower())
