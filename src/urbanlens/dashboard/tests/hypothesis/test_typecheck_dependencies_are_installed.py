"""A typecheck that cannot resolve its imports reports nothing useful.

`bun run typecheck` runs two projects: the root one, and
`tests/integration/tsconfig.json`. The integration suite is a self-contained npm
project - its own `package.json`, its own lockfile, its own TypeScript version -
and the root `bun install` installs none of it. On a developer's machine the
suite's `node_modules` is there from working on it, so the command passes; on a
CI runner it is not, and every import of `@playwright/test`, `@axe-core/playwright`
and `axe-core` fails TS2307. That is what CI reported on 2026-09-11, from a commit
that touched no TypeScript.

`bin/check_typescript_coverage.py` requires the root script to name every tracked
project, on the sound reasoning that a project nothing runs covers its files on
paper only. Satisfying that put a project into a command that cannot resolve it.
Both halves are right; the missing piece was the install.

So the rule is: whichever CI job runs a typecheck over a project that sits beside
its own `package.json` must also install that project's dependencies. Asserted
against the workflow file, because the gap only ever appeared there.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PACKAGE_JSON = _REPO_ROOT / "package.json"


def _typechecked_projects() -> list[str]:
    """The tsconfig paths the root `typecheck` script runs.

    Returns:
        Repo-relative paths, in the order they appear.
    """
    script = json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]["typecheck"]
    return [token for token in script.split() if token.endswith("tsconfig.json")]


def _nested_projects() -> list[str]:
    """Typechecked projects whose dependencies come from their own install.

    Returns:
        The directory of each such project, repo-relative.
    """
    directories = []
    for config in _typechecked_projects():
        directory = pathlib.PurePosixPath(config).parent
        if str(directory) != "." and (_REPO_ROOT / directory / "package.json").is_file():
            directories.append(str(directory))
    return directories


def _job_running_typecheck() -> dict:
    """The CI job whose steps run `bun run typecheck`.

    Returns:
        The job definition.

    Raises:
        AssertionError: No job runs it, which would make every assertion here
            vacuous rather than satisfied.
    """
    jobs = yaml.safe_load(_CI_PATH.read_text(encoding="utf-8"))["jobs"]
    for job in jobs.values():
        if any("bun run typecheck" in str(step.get("run", "")) for step in job.get("steps", [])):
            return job
    raise AssertionError("no CI job runs `bun run typecheck`; this file is asserting against nothing")


class TheRootTypecheckCanResolveWhatItReadsTests(SimpleTestCase):
    """Each project the root script runs must have its imports installed."""

    def test_the_premise_holds(self) -> None:
        """A nested project exists; otherwise the rule below has no subject."""
        self.assertEqual(_nested_projects(), ["tests/integration"])

    def test_the_ci_job_installs_each_nested_project(self) -> None:
        steps = _job_running_typecheck().get("steps", [])
        for directory in _nested_projects():
            installs = [
                step
                for step in steps
                if step.get("working-directory") == directory and "npm" in str(step.get("run", ""))
            ]
            self.assertTrue(
                installs,
                f"{directory} is typechecked by `bun run typecheck` but nothing in that CI job installs its "
                "dependencies, so every import in it resolves to TS2307 and the check reports errors that are "
                "not there",
            )

    def test_the_install_runs_before_the_typecheck(self) -> None:
        """Order is the whole content of an install step."""
        steps = _job_running_typecheck().get("steps", [])
        typecheck_at = next(
            index for index, step in enumerate(steps) if "bun run typecheck" in str(step.get("run", ""))
        )
        for directory in _nested_projects():
            install_at = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.get("working-directory") == directory and "npm" in str(step.get("run", ""))
                ),
                None,
            )
            self.assertIsNotNone(install_at)
            self.assertLess(install_at, typecheck_at, f"{directory} is installed after the typecheck that needs it")
