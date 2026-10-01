"""Every compiled JS reference must point at a file that exists."""

from __future__ import annotations

from pathlib import Path
import re

from urbanlens.core.tests.testcase import SimpleTestCase

_JS_DIR = Path(__file__).resolve().parents[3] / "dashboard" / "frontend" / "static" / "dashboard" / "js"
_TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "dashboard" / "templates"

#: `from"./chunk.js"`, `import"./chunk.js"`, and their spaced variants.
_RELATIVE_IMPORT = re.compile(r"""(?:from|import)\s*["'](\./[^"']+\.js)["']""")

#: A static path naming a compiled bundle, as written in a template.
_TEMPLATE_JS_REFERENCE = re.compile(r"dashboard/js/([a-zA-Z0-9_.-]+\.js)")


def _bundle_files() -> list[Path]:
    return sorted(_JS_DIR.glob("*.js"))


def _template_referenced_bundles() -> set[str]:
    names: set[str] = set()
    for template in _TEMPLATE_DIR.rglob("*.html"):
        names.update(_TEMPLATE_JS_REFERENCE.findall(template.read_text(encoding="utf-8", errors="ignore")))
    return names


#: What to tell someone whose build is missing or behind. The bundles are not in
#: git, so "it works on my machine" here means "my machine ran the build".
_REBUILD = "run `bun run build` (or `bin/sync_app.sh --frontend`) and re-run"


class CompiledJsReferenceTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Not a tolerated absence - a refusal to make a claim. With no build at
        # all, every check below passes by iterating over nothing, which is the
        # one outcome worse than failing.
        if not _bundle_files():
            self.skipTest(f"no compiled bundles in {_JS_DIR} - nothing to check until you {_REBUILD}")

    def test_every_chunk_import_resolves(self) -> None:
        missing: list[str] = []
        for bundle in _bundle_files():
            for target in _RELATIVE_IMPORT.findall(bundle.read_text(encoding="utf-8", errors="ignore")):
                if not (_JS_DIR / target.removeprefix("./")).is_file():
                    missing.append(f"{bundle.name} imports {target}, which the build did not emit")

        self.assertEqual(
            missing, [], f"compiled JS imports a chunk that does not exist - {_REBUILD}:\n" + "\n".join(missing)
        )

    def test_every_bundle_a_template_names_exists(self) -> None:
        missing = sorted(name for name in _template_referenced_bundles() if not (_JS_DIR / name).is_file())

        self.assertEqual(
            missing, [], f"templates name compiled bundles this build did not emit - {_REBUILD}: {missing}"
        )

    # -- guard the guard ----------------------------------------------------

    def test_the_scan_finds_bundles_and_imports(self) -> None:
        """Both checks pass trivially if the directory or the regex stops matching."""
        bundles = _bundle_files()
        imports = [
            target
            for bundle in bundles
            for target in _RELATIVE_IMPORT.findall(bundle.read_text(encoding="utf-8", errors="ignore"))
        ]

        self.assertGreater(len(bundles), 10, f"found only {len(bundles)} compiled bundles in {_JS_DIR}")
        self.assertGreater(
            len(imports), 0, "no relative chunk imports matched - has the bundler's output format changed?"
        )
        self.assertGreater(len(_template_referenced_bundles()), 5, "no templates appear to reference a compiled bundle")
