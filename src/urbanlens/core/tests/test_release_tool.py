"""
``bin/release.py``, the git-cliff release tool that replaced release-please.

Nothing here reaches Forgejo or runs git-cliff: the client's transport is replaced, and the
release step is tested through the pieces that decide what a release PR contains.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from urbanlens.core.tests.testcase import SimpleTestCase

_BIN = Path(__file__).resolve().parents[4] / "bin"


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _BIN / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


forgejo_api = _load("forgejo_api")
release = _load("release")

TOKEN = "fj_secret_token_value"  # noqa: S105 - a fake, to prove it stays out of errors
BASE_TIP = "b" * 40


class FakeForgejo(forgejo_api.Forgejo):
    """A client whose transport answers from a script of (method, path suffix) -> (status, body)."""

    def __init__(self, answers: dict[tuple[str, str], tuple[int, object]]) -> None:
        super().__init__(token=TOKEN, api="https://forgejo.invalid/api/v1")
        self.answers = answers
        self.sent: list[tuple[str, str, object]] = []

    def _send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        path = url.split("/api/v1/", 1)[1].split("?", maxsplit=1)[0]
        self.sent.append((method, path, json.loads(body) if body else None))
        for (want_method, suffix), (status, payload) in self.answers.items():
            if method == want_method and path.endswith(suffix):
                return status, json.dumps(payload).encode()
        return 404, b'{"message": "not scripted"}'


class ClientTests(SimpleTestCase):
    def test_the_token_stays_out_of_errors(self) -> None:
        fake = FakeForgejo({("GET", "/pulls/8"): (500, {"message": "boom"})})
        with self.assertRaises(forgejo_api.ForgejoError) as caught:
            fake.pull(8)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_a_missing_tag_or_release_is_none(self) -> None:
        fake = FakeForgejo({})
        self.assertIsNone(fake.tag_sha("v0.9.0"))
        self.assertIsNone(fake.release_by_tag("v0.9.0"))

    def test_a_release_is_published_on_the_merge_commit(self) -> None:
        fake = FakeForgejo({("POST", "/releases"): (201, {"html_url": "x"})})
        fake.create_release(tag="v0.9.0", target=BASE_TIP, name="v0.9.0", body="notes", prerelease=True)
        self.assertEqual(
            fake.sent[-1],
            (
                "POST",
                "repos/UrbanLens/UrbanLens/releases",
                {
                    "tag_name": "v0.9.0",
                    "target_commitish": BASE_TIP,
                    "name": "v0.9.0",
                    "body": "notes",
                    "draft": False,
                    "prerelease": True,
                },
            ),
        )


class ReleasePullTests(SimpleTestCase):
    def test_a_release_commit_on_an_older_main_is_stale(self) -> None:
        self.assertIsNone(release.stale_problem(BASE_TIP, BASE_TIP))
        self.assertIn("stale", release.stale_problem("c" * 40, BASE_TIP))

    def test_only_the_release_branch_into_main_is_the_release_pr(self) -> None:
        class Pulls:
            def open_pulls(self, base: str | None = None) -> list[dict[str, Any]]:
                return [
                    {"number": 1, "head": {"ref": "chore/release-main"}, "base": {"ref": "release/v_0_9_0"}},
                    {"number": 2, "head": {"ref": "fix/x"}, "base": {"ref": "main"}},
                    {"number": 3, "head": {"ref": "chore/release-main"}, "base": {"ref": "main"}},
                ]

        self.assertEqual(release._release_pull(Pulls())["number"], 3)

    def test_the_release_branch_is_not_an_integration_branch(self) -> None:
        # release/* branches are where development integrates; the bump PR must not look like one.
        self.assertFalse(release.RELEASE_BRANCH.startswith("release/"))
        self.assertEqual(release.TARGET, "main")


class VersionTests(SimpleTestCase):
    def test_below_1_0_a_feature_or_a_breaking_change_bumps_the_minor(self) -> None:
        self.assertEqual(release.next_version("0.8.0", minor=False), "0.8.1")
        self.assertEqual(release.next_version("0.8.0", minor=True), "0.9.0")
        self.assertEqual(release.next_version("0.9.9", minor=True), "0.10.0")

    def test_what_bumps_the_minor(self) -> None:
        fix = release.Entry("Bug Fixes", "", "a fix", "1" * 40)
        feat = release.Entry("Features", "", "a feature", "2" * 40)
        breaking = release.Entry("", "api", "drop it", "3" * 40, "GET /old/ is gone")
        self.assertFalse(release.bumps_minor([fix]))
        self.assertTrue(release.bumps_minor([fix, feat]))
        self.assertTrue(release.bumps_minor([breaking]))

    def test_it_never_leaves_0_x(self) -> None:
        with self.assertRaises(release.ReleaseError):
            release.next_version("1.0.0", minor=False)
        with self.assertRaises(release.ReleaseError):
            release.release_as("chore: go\n\nRelease-As: 1.0.0\n", "0.9.0")

    def test_release_as_names_the_version(self) -> None:
        # Newest first, as git log prints them.
        messages = "fix: b\n\nRelease-As: 0.9.2\n\nfeat: v0.9.0 (#12)\n\nrelease-as: 0.9.1\n"
        self.assertEqual(release.release_as(messages, "0.8.0"), "0.9.2")
        self.assertIsNone(release.release_as("feat: a\n", "0.8.0"))

    def test_a_release_as_at_or_below_the_current_version_is_ignored(self) -> None:
        self.assertIsNone(release.release_as("chore: x\n\nRelease-As: 0.8.0\n", "0.8.0"))
        self.assertEqual(
            release.release_as("chore: y\n\nRelease-As: 0.8.0\n\nchore: x\n\nRelease-As: 0.9.0\n", "0.8.0"), "0.9.0"
        )

    def _files(self, version: str) -> dict[str, str]:
        return {
            ".release-please-manifest.json": '{\n  ".": "0.8.0"\n}\n',
            "package.json": f'{{\n  "name": "urbanlens",\n  "version": "{version}",\n  "private": true\n}}\n',
            "pyproject.toml": f'[project]\nname = "UrbanLens"\nversion = "{version}"\n\n[tool.x]\nversion = "{version}"\n',
            "uv.lock": f'version = 1\n\n[[package]]\nname = "other"\nversion = "{version}"\n\n[[package]]\nname = "urbanlens"\nversion = "{version}"\nsource = {{ editable = "." }}\n',
        }

    def test_each_version_file_is_bumped_in_place(self) -> None:
        files = self._files("0.8.0")
        self.assertEqual(set(files), set(release.VERSION_FILES))
        for path, text in files.items():
            with self.subTest(path=path):
                bumped = release.bump_text(path, text, "0.9.0")
                self.assertEqual(release.recorded_version(path, bumped), "0.9.0")
                self.assertEqual(bumped.count("0.9.0"), 1)
                self.assertEqual(release.without_version(path, bumped), release.without_version(path, text))

    def test_a_file_already_at_the_new_version_is_left_alone(self) -> None:
        # release/v_0_9_0 records 0.9.0 in everything but the manifest before 0.9.0 is released.
        for path, text in self._files("0.9.0").items():
            if path == release.MANIFEST:
                continue
            with self.subTest(path=path):
                self.assertEqual(release.bump_text(path, text, "0.9.0"), text)

    def test_a_file_above_the_new_version_stops_the_bump(self) -> None:
        with self.assertRaises(release.ReleaseError):
            release.bump_text("pyproject.toml", '[project]\nversion = "0.10.0"\n', "0.9.0")


def _commit(
    sha: str,
    group: str | None,
    message: str,
    scope: str | None = None,
    *,
    breaking: bool = False,
    breaking_description: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sha * 40,
        "group": group,
        "message": message,
        "scope": scope,
        "breaking": breaking,
        "breaking_description": breaking_description,
    }


class ChangelogTests(SimpleTestCase):
    def _entries(self) -> list[Any]:
        context = [
            {
                "version": None,
                "commits": [
                    _commit("1", "<!-- 1 -->Bug Fixes", "quote the nginx static-asset regex", "nginx"),
                    _commit("2", "<!-- 0 -->Features", "export a route", "routes"),
                    _commit("3", "<!-- 4 -->Documentation", "the release tool"),
                    _commit("4", "<!-- 4 -->Documentation", "deploy runbook"),
                    _commit("5", "<!-- 4 -->Documentation", "record a decision", "adr"),
                    _commit("6", "<!-- 1 -->Bug Fixes", "quote the nginx static-asset regex", "nginx"),  # rebased twin
                    _commit(
                        "7", None, "drop the old API", "api", breaking=True, breaking_description="GET /old/ is gone"
                    ),
                    _commit("8", None, "tidy"),  # a hidden type
                ],
            },
        ]
        return release.entries_from_context(context)

    def test_the_section_matches_release_please_s_format(self) -> None:
        web = forgejo_api.WEB_URL
        section = release.render_section("0.9.0", "0.8.0", dt.date(2026, 10, 12), self._entries())
        self.assertEqual(
            section,
            f"## [0.9.0]({web}/compare/v0.8.0...v0.9.0) (2026-10-12)\n"
            "\n\n### ⚠ BREAKING CHANGES\n\n"
            f"* **api:** GET /old/ is gone ([7777777]({web}/commit/{'7' * 40}))\n"
            "\n\n### Features\n\n"
            f"* **routes:** export a route ([2222222]({web}/commit/{'2' * 40}))\n"
            "\n\n### Bug Fixes\n\n"
            f"* **nginx:** quote the nginx static-asset regex ([1111111]({web}/commit/{'1' * 40}))\n"
            "\n\n### Documentation\n\n"
            f"* **adr:** record a decision ([5555555]({web}/commit/{'5' * 40}))\n"
            f"* deploy runbook ([4444444]({web}/commit/{'4' * 40}))\n"
            f"* the release tool ([3333333]({web}/commit/{'3' * 40}))\n",
        )

    def test_the_section_goes_above_the_newest_release_and_reads_back(self) -> None:
        changelog = "# Changelog\n\n## [0.8.0](x) (2026-10-01)\n\nAbout 1,600 commits.\n\n### Bug Fixes\n\n* a\n\n## [0.7.0](y) (2026-09-01)\n"
        section = release.render_section("0.8.1", "0.8.0", dt.date(2026, 10, 12), self._entries()[:1])
        updated = release.insert_section(changelog, section)
        self.assertEqual(
            updated,
            "# Changelog\n\n"
            + section
            + "\n## [0.8.0](x) (2026-10-01)\n\nAbout 1,600 commits.\n\n### Bug Fixes\n\n* a\n\n## [0.7.0](y) (2026-09-01)\n",
        )
        self.assertEqual(release.section_for(updated, "0.8.1"), section)
        self.assertEqual(
            release.section_for(updated, "0.8.0"),
            "## [0.8.0](x) (2026-10-01)\n\nAbout 1,600 commits.\n\n### Bug Fixes\n\n* a\n",
        )
        with self.assertRaises(release.ReleaseError):
            release.insert_section(updated, section)

    def test_the_cliff_config_sorts_into_the_sections_the_tool_knows(self) -> None:
        import tomllib

        config = tomllib.loads((_BIN.parent / "cliff.toml").read_text(encoding="utf-8"))
        groups = {p["group"].split("-->")[-1] for p in config["git"]["commit_parsers"] if "group" in p}
        self.assertEqual(groups, set(release.SECTIONS))
        self.assertFalse(config["bump"]["breaking_always_bump_major"])
