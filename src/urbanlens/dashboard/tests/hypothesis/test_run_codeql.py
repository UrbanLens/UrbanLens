"""CodeQL wrapper: only a finalised database is reusable, and notes stay quiet.

A failed JavaScript extract still writes ``codeql-database.yml``. Treating that
file as success makes ``database analyze`` fail with "needs to be finalized".
Findings without a per-result ``level`` take the rule default, so notes must
not be dumped on every run.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase

_SCRIPT = Path(__file__).resolve().parents[5] / "bin" / "run_codeql.py"


def _load_runner():
    """Import ``bin/run_codeql.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_run_codeql", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sarif(*, note_rule: str = "py/cyclic-import", error_rule: str = "py/log-injection") -> dict:
    """Return a minimal SARIF document with one note and one error."""
    return {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {"id": note_rule, "defaultConfiguration": {"level": "note"}},
                            {"id": error_rule, "defaultConfiguration": {"level": "error"}},
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": note_rule,
                        "message": {"text": "cycle"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "a.py"},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    },
                    {
                        "ruleId": error_rule,
                        "message": {"text": "log"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "b.py"},
                                    "region": {"startLine": 2},
                                }
                            }
                        ],
                    },
                ],
            }
        ]
    }


class RunCodeqlWrapperTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.runner = _load_runner()

    def test_top_level_finalised_true_is_ready(self) -> None:
        text = "primaryLanguage: python\nfinalised: true\n"
        self.assertTrue(self.runner._yaml_finalised(text))

    def test_top_level_finalised_false_is_not_ready(self) -> None:
        text = "primaryLanguage: javascript\ninProgress:\n  primaryLanguage: javascript\nfinalised: false\n"
        self.assertFalse(self.runner._yaml_finalised(text))

    def test_indented_finalised_is_ignored(self) -> None:
        text = "inProgress:\n  finalised: true\nfinalised: false\n"
        self.assertFalse(self.runner._yaml_finalised(text))

    def test_missing_finalised_is_not_ready(self) -> None:
        self.assertFalse(self.runner._yaml_finalised("primaryLanguage: python\n"))

    def test_database_ready_requires_finalised_true(self) -> None:
        with TemporaryDirectory() as tmp:
            cluster = Path(tmp)
            yml = cluster / "javascript" / "codeql-database.yml"
            yml.parent.mkdir()
            yml.write_text("finalised: false\n", encoding="utf-8")
            with patch.object(self.runner, "DB_CLUSTER", cluster):
                self.assertFalse(self.runner._database_ready("javascript"))
            yml.write_text("finalised: true\n", encoding="utf-8")
            with patch.object(self.runner, "DB_CLUSTER", cluster):
                self.assertTrue(self.runner._database_ready("javascript"))

    def test_print_sarif_omits_notes_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "python.sarif"
            path.write_text(json.dumps(_sarif()), encoding="utf-8")
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                count = self.runner._print_sarif(path)
        text = captured.getvalue()
        self.assertEqual(count, 1)
        self.assertIn("py/log-injection", text)
        self.assertIn("b.py:2: error:", text)
        self.assertNotIn("a.py:1: note:", text)
        self.assertIn("1 error/warning, 1 note", text)
        self.assertIn("omitted", text)

    def test_print_sarif_verbose_includes_notes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "python.sarif"
            path.write_text(json.dumps(_sarif()), encoding="utf-8")
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                self.runner._print_sarif(path, verbose=True)
        text = captured.getvalue()
        self.assertIn("a.py:1: note:", text)
        self.assertNotIn("omitted", text)

    def test_print_sarif_quiet_is_summary_only(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "python.sarif"
            path.write_text(json.dumps(_sarif()), encoding="utf-8")
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                self.runner._print_sarif(path, quiet=True)
        text = captured.getvalue()
        self.assertIn("py/log-injection", text)
        self.assertIn("py/cyclic-import", text)
        self.assertNotIn("b.py:2:", text)
        self.assertNotIn("a.py:1:", text)

    @given(st.booleans())
    @settings(max_examples=20, deadline=None)
    def test_yaml_finalised_matches_the_top_level_flag(self, flag: bool) -> None:
        value = "true" if flag else "false"
        text = f"primaryLanguage: python\nfinalised: {value}\n"
        self.assertEqual(self.runner._yaml_finalised(text), flag)
