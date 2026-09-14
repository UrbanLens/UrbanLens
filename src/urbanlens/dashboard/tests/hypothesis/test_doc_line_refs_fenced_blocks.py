"""A line number inside a fenced code block is quoted output, not a citation (P49).

`bin/check_doc_line_refs.py` failed on a traceback pasted into `PROBLEMS.md`: the frames carry the line numbers the code
had when it crashed. Renumbering them would falsify the quote, so the check reads prose only. These run it against a
throwaway repository, since this one's documents change daily and the test image carries no `docs/`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_doc_line_refs.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_doc_line_refs", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FencedBlockTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def _check(self, document: str) -> int:
        with tempfile.TemporaryDirectory() as root:
            repo = pathlib.Path(root)
            (repo / "services").mkdir()
            (repo / "services" / "boundaries.py").write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
            (repo / "docs").mkdir()
            (repo / "docs" / "PROBLEMS.md").write_text(document, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            previous = pathlib.Path.cwd()
            os.chdir(repo)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    return self.checker.check()
            finally:
                os.chdir(previous)

    def test_a_traceback_frame_in_a_fenced_block_is_not_checked(self) -> None:
        document = "The crash:\n\n```\n    get_buildings (services/boundaries.py:328)\n```\n"

        self.assertEqual(self._check(document), 0)

    def test_the_same_citation_in_prose_still_fails(self) -> None:
        document = "The crash is at `services/boundaries.py:328`.\n"

        self.assertEqual(self._check(document), 1)

    def test_prose_after_a_closed_fence_is_checked_again(self) -> None:
        document = "```\nservices/boundaries.py:328\n```\n\nNow at `services/boundaries.py:329`.\n"

        self.assertEqual(self._check(document), 1)
