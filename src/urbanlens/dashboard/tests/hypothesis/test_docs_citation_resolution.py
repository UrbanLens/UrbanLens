"""One citation string can resolve from one directory and not another.

`bin/check_docs_refs.py` tries a citation against the repository root, against
`docs/`, and against the citing file's own directory - so `GUIDE.md` is a live
pointer from a directory that holds one and a dangling pointer from anywhere
else. Its resolution cache was keyed on the citation string alone, which made
whichever file happened to be scanned first decide the answer for every other
file citing the same name, in both directions.

That was harmless while every citation carried a `docs/` prefix. Matching bare
capitalised filenames - which is what lets a root `TODO.md` citation be seen at
all - makes basenames that recur in several directories the normal case.

The second class covers the other spelling the checker promises to handle: a
path into a sibling checkout. Its docstring says such a path "counts as resolved
when that checkout is absent ... failing on it would make the check depend on how
a developer laid out their workspace" - and that is exactly what it did. CI, which
checks out this repository alone, failed on five `../REData/docs/...` citations and
one `../infrastructure/docs/...` that every developer with the siblings beside them
saw pass.

These run the checker against throwaway repositories rather than this one, so a
regression fails here instead of waiting for a filename to collide in the tree.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile

from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_docs_refs.py"


def _load_checker():
    """Import ``bin/check_docs_refs.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_docs_refs", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CitationResolutionTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def _repo(self, files: dict[str, str]) -> pathlib.Path:
        """Build a throwaway git repository holding `files`, and track them."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", "-A"]):
            subprocess.run(command, cwd=root, check=True, capture_output=True)
        return root

    def test_a_citation_that_resolves_beside_one_file_does_not_excuse_another(self) -> None:
        """The false negative: a neighbour's valid citation covering a broken one."""
        root = self._repo(
            {
                "docs/sub/GUIDE.md": "# guide\n",
                "docs/sub/note.md": "see GUIDE.md\n",
                "src/app.py": '"""see GUIDE.md."""\n',
            },
        )
        broken_code, _ = self.checker.broken_citations(root)
        self.assertEqual(broken_code, {"GUIDE.md": ["src/app.py"]})

    def test_a_broken_citation_elsewhere_does_not_condemn_a_valid_one(self) -> None:
        """The false positive, the same bug in the other direction."""
        root = self._repo(
            {
                "bin/tool.py": '"""see GUIDE.md."""\n',
                "lib/GUIDE.md": "# guide\n",
                "lib/api.py": '"""see GUIDE.md."""\n',
            },
        )
        broken_code, _ = self.checker.broken_citations(root)
        self.assertEqual(broken_code, {"GUIDE.md": ["bin/tool.py"]})

    def test_a_bare_name_resolves_against_docs_as_well_as_the_root(self) -> None:
        """`PROBLEMS.md` means `docs/PROBLEMS.md` in most of this codebase."""
        root = self._repo({"docs/PROBLEMS.md": "# problems\n", "src/app.py": '"""see PROBLEMS.md."""\n'})
        broken_code, _ = self.checker.broken_citations(root)
        self.assertEqual(broken_code, {})

    def test_a_gitignored_target_fails_like_a_missing_one(self) -> None:
        """It resolves for whoever wrote the citation and for nobody else."""
        root = self._repo(
            {
                ".gitignore": "docs/scratch/\n",
                "docs/scratch/NOTES.md": "# local only\n",
                "src/app.py": '"""see docs/scratch/NOTES.md."""\n',
            },
        )
        broken_code, _ = self.checker.broken_citations(root)
        self.assertIn("docs/scratch/NOTES.md", broken_code)

    def test_a_citation_from_a_document_is_reported_but_not_fatal(self) -> None:
        root = self._repo({"docs/a.md": "see docs/gone.md\n"})
        broken_code, broken_docs = self.checker.broken_citations(root)
        self.assertEqual(broken_code, {})
        self.assertEqual(broken_docs, {"docs/gone.md": ["docs/a.md"]})


class SiblingCheckoutCitationTests(SimpleTestCase):
    """A path into a sibling repository is verified only when it is there.

    Each repository here is built *inside* a workspace directory, so its parent
    is somewhere siblings can be created - which is the layout the citations
    describe and the one the checker resolves against.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def _workspace(self, files: dict[str, str]) -> tuple[pathlib.Path, pathlib.Path]:
        """Build `<workspace>/UrbanLens` holding `files`, tracked in git.

        Args:
            files: Repo-relative path to contents.

        Returns:
            ``(workspace, root)``.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspace = pathlib.Path(directory.name)
        root = workspace / "UrbanLens"
        root.mkdir()
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", "-A"]):
            subprocess.run(command, cwd=root, check=True, capture_output=True)
        return workspace, root

    def test_an_absent_sibling_is_not_a_broken_citation(self) -> None:
        """What CI sees: this repository alone, and no siblings anywhere."""
        _workspace, root = self._workspace({"src/app.py": '"""see ../REData/docs/api-reference.md."""\n'})

        broken_code, _ = self.checker.broken_citations(root)

        self.assertEqual(broken_code, {}, "a citation into an absent sibling checkout was treated as broken")

    def test_a_present_sibling_missing_the_file_still_fails(self) -> None:
        """The check is skipped for want of evidence, not waived."""
        workspace, root = self._workspace({"src/app.py": '"""see ../REData/docs/api-reference.md."""\n'})
        (workspace / "REData" / "docs").mkdir(parents=True)

        broken_code, _ = self.checker.broken_citations(root)

        self.assertEqual(broken_code, {"../REData/docs/api-reference.md": ["src/app.py"]})

    def test_a_present_sibling_holding_the_file_resolves(self) -> None:
        workspace, root = self._workspace({"src/app.py": '"""see ../REData/docs/api-reference.md."""\n'})
        (workspace / "REData" / "docs").mkdir(parents=True)
        (workspace / "REData" / "docs" / "api-reference.md").write_text("# api\n", encoding="utf-8")

        broken_code, _ = self.checker.broken_citations(root)

        self.assertEqual(broken_code, {})

    def test_a_deep_citing_file_is_judged_from_the_repository_root(self) -> None:
        """`../REData/...` means "beside this repository", wherever it is written.

        Resolved from the citing file's own directory instead, the same string
        lands back *inside* the repository - which is how a test six directories
        down turned one sibling citation into a fatal one.
        """
        _workspace, root = self._workspace(
            {
                "src/urbanlens/dashboard/tests/hypothesis/test_ops.py": '"""see ../infrastructure/docs/OPS_TOOLING.md."""\n'
            },
        )

        broken_code, _ = self.checker.broken_citations(root)

        self.assertEqual(broken_code, {})
