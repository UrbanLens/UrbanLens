"""
A draft pull request's push runs the quick checks; the 2-hour test suite waits for the full run before merge.

A pull request pushed four to six times while its review fixes go in would otherwise start a 2-hour suite run on
every push. `.forgejo/workflows/ci.yml` has two tiers ("Tiers" there). Waypoint opens agents' pull requests titled
``WIP: `` and drops the prefix once the draft tier is green; that edit is the full run.

Asserted against the workflow file, since the tiers live only there. Waypoint reads the same file: it opens pull
requests as drafts only where a workflow runs on ``edited``, and it waits for every check to report again after a
pull request is marked ready, so a job that ran only in the draft tier would hold it forever (Waypoint WP-552).
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

import yaml

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
#: Forgejo-side only (.forgejo/ stays off the public mirror), so the tests below skip where .forgejo/ is absent.
_FORGEJO = _REPO_ROOT / ".forgejo"
_DRAFT = "!startsWith(github.event.pull_request.title, 'WIP:')"


def _workflow() -> dict:
    if not _FORGEJO.is_dir():
        raise unittest.SkipTest(".forgejo/ is Forgejo-side only and absent from this tree")
    return yaml.safe_load((_FORGEJO / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _full_run_script():
    if not _FORGEJO.is_dir():
        raise unittest.SkipTest(".forgejo/ is Forgejo-side only and absent from this tree")
    spec = importlib.util.spec_from_file_location("full_run", _FORGEJO / "scripts" / "full_run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_step(job: dict) -> dict:
    return next(step for step in job["steps"] if "pytest" in str(step.get("run", "")))


class TheWorkflowHasTwoTiersTests(SimpleTestCase):
    def test_an_edit_runs_the_workflow(self) -> None:
        """Without `edited`, dropping ``WIP: `` starts nothing, and Waypoint opens every pull request ready."""
        # PyYAML reads the bare key `on` as True.
        self.assertIn("edited", _workflow()[True]["pull_request"]["types"])

    def test_a_draft_skips_the_test_suite(self) -> None:
        jobs = _workflow()["jobs"]
        self.assertIn(_DRAFT, _test_step(jobs["python-quality"]).get("if", ""))
        self.assertIn(_DRAFT, jobs["docker"]["if"])

    def test_a_draft_still_runs_the_quick_checks(self) -> None:
        jobs = _workflow()["jobs"]
        tests = _test_step(jobs["python-quality"])
        for name in ("python-quality", "frontend"):
            self.assertNotIn(_DRAFT, jobs[name]["if"], f"{name} is the draft tier and must run on a draft")
            # Every command but the suite; the coverage upload goes with the suite.
            quick = [step for step in jobs[name]["steps"] if "run" in step and step is not tests]
            for step in quick:
                self.assertNotIn(_DRAFT, str(step.get("if", "")), step.get("name"))
        runs = " ".join(str(step.get("run", "")) for step in jobs["python-quality"]["steps"] if step is not tests)
        for check in ("ruff check", "mypy", "bin/check_", "makemigrations --check"):
            self.assertIn(check, runs)

    def test_no_job_runs_in_the_draft_tier_alone(self) -> None:
        """Waypoint counts a check as part of the full run only once it reports after the pull request went ready."""
        draft_only = re.compile(r"(?<!!)startsWith\(github\.event\.pull_request\.title")
        for name, job in _workflow()["jobs"].items():
            self.assertIsNone(draft_only.search(str(job.get("if", ""))), name)

    def test_an_edit_that_is_not_the_full_run_runs_nothing(self) -> None:
        """A body edit (forge-sync makes one whenever the GitHub mirror's description changes) costs one small job."""
        jobs = _workflow()["jobs"]
        for name in ("python-quality", "frontend"):
            self.assertIn("full-run", jobs[name]["needs"])
            self.assertIn("needs.full-run.outputs.run == 'true'", jobs[name]["if"])


class AnEditIsTheFullRunTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.script = _full_run_script()
        self.context = self.script.FULL_TIER_CONTEXT

    def status(self, status_id: int, state: str, description: str = "") -> dict:
        return {"id": status_id, "context": self.context, "status": state, "description": description}

    def test_a_draft_edit_is_not(self) -> None:
        run, _ = self.script.is_full_run("WIP: fix the map", [])
        self.assertFalse(run)

    def test_dropping_wip_on_a_head_that_only_had_the_draft_tier_is(self) -> None:
        skipped = self.status(1, "success", "Has been skipped")
        run, _ = self.script.is_full_run("fix the map", [skipped, self.status(2, "pending")])
        self.assertTrue(run)

    def test_an_edit_after_a_passed_full_run_is_not(self) -> None:
        run, _ = self.script.is_full_run(
            "fix the map", [self.status(1, "success"), self.status(2, "success", "Has been skipped")]
        )
        self.assertFalse(run)

    def test_an_edit_after_a_failed_full_run_runs_it_again(self) -> None:
        run, _ = self.script.is_full_run("fix the map", [self.status(1, "success"), self.status(2, "failure")])
        self.assertTrue(run)

    def test_the_context_names_the_workflow_s_full_tier_job(self) -> None:
        name = _workflow()["jobs"]["docker"]["name"]
        self.assertIn("'Docker image build'", name)
        self.assertEqual(self.context, "CI / Docker image build (pull_request)")
