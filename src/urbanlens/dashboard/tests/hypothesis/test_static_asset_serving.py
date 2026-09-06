"""`/static/` has to be served by something, and the assets have to be in the image.

Every `/static/` URL 404'd on the k8s deployment, which runs gunicorn with no
nginx in front of it and no static volume mounted. Two independent causes, and
fixing either alone leaves the site unstyled:

- nothing in `MIDDLEWARE` served static files, and
- the published image contained only the fraction of the collected tree that
  had been committed to git, against a manifest describing all of it.

The tests here pin both, plus the ordering argument that decides *where* the
middleware goes: WhiteNoise short-circuits in the request phase, so everything
above it still runs on the way out and everything below it is skipped.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
_INIT_PATH = _REPO_ROOT / "src" / "bin" / "init.py"

_WHITENOISE = "whitenoise.middleware.WhiteNoiseMiddleware"

#: Must run on a static response: these attach security headers and cost
#: nothing, so they belong above the short-circuit.
_ABOVE = (
    "django.middleware.security.SecurityMiddleware",
    "urbanlens.dashboard.middleware.SecurityHeadersMiddleware",
    "csp.middleware.CSPMiddleware",
    "corsheaders.middleware.CorsMiddleware",
)

#: Must not run on a static response: each costs a query, a cookie, or both,
#: and a `Set-Cookie` is what stops a CDN edge from caching the asset.
_BELOW = (
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "urbanlens.dashboard.middleware.MediaOriginCookieMiddleware",
    "urbanlens.dashboard.middleware.ProfilePreviewMiddleware",
)


def _load_init_module():
    """Import ``src/bin/init.py``, which is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_init_static", _INIT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StaticMiddlewarePositionTests(SimpleTestCase):
    """WhiteNoise is installed, and sits where the short-circuit is harmless."""

    def test_whitenoise_is_installed(self) -> None:
        self.assertIn(_WHITENOISE, settings.MIDDLEWARE)

    def test_static_storage_backend_still_hashes_outside_tests(self) -> None:
        """The middleware and the storage backend have to agree about hashing.

        The manifest backend is what turns `dashboard/style.css` into a hashed
        name, and WhiteNoise's `immutable_file_test` asks that same storage
        whether a requested file is a hashed one before it promises a ten-year
        `Cache-Control`. Swapping it for a plain backend would serve every asset
        with `max-age=60` instead, which is the difference between a cached edge
        and an uncached one - and nothing else in the suite would notice, since
        TESTING deliberately runs on the plain backend.
        """
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"], "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
        source = (_REPO_ROOT / "src" / "urbanlens" / "UrbanLens" / "settings" / "base.py").read_text()
        self.assertIn(
            '"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if TESTING else "whitenoise.storage.CompressedManifestStaticFilesStorage"',
            source,
        )

    def test_security_header_middleware_runs_on_static_responses(self) -> None:
        index = settings.MIDDLEWARE.index(_WHITENOISE)
        for name in _ABOVE:
            with self.subTest(middleware=name):
                self.assertLess(
                    settings.MIDDLEWARE.index(name),
                    index,
                    f"{name} must stay above WhiteNoise or static responses lose its header",
                )

    def test_cookie_and_query_middleware_is_skipped_for_static_responses(self) -> None:
        index = settings.MIDDLEWARE.index(_WHITENOISE)
        for name in _BELOW:
            with self.subTest(middleware=name):
                self.assertGreater(
                    settings.MIDDLEWARE.index(name),
                    index,
                    f"{name} must stay below WhiteNoise or every static response carries its cost",
                )


class StaticAssetsInImageTests(SimpleTestCase):
    """The image build has to produce the collected tree, not inherit it from git."""

    def test_dockerfile_runs_the_frontend_build(self) -> None:
        dockerfile = (_REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("init.py --frontend-only", dockerfile)

    def test_dockerfile_build_runs_before_the_entrypoint_is_installed(self) -> None:
        """Ordering, so the build cannot be moved somewhere it has no venv or no source."""
        dockerfile = (_REPO_ROOT / "Dockerfile").read_text()
        build_at = dockerfile.index("init.py --frontend-only")
        self.assertGreater(build_at, dockerfile.index("COPY --chown=appuser:appuser . /app"))
        self.assertLess(build_at, dockerfile.index("COPY docker-entrypoint.sh"))

    def test_collected_root_is_not_tracked_in_git(self) -> None:
        """STATIC_ROOT is build output; committing part of it is what broke the manifest."""
        ignore_rules = {line.strip() for line in (_REPO_ROOT / ".gitignore").read_text().splitlines()}
        self.assertIn("/src/urbanlens/frontend/static/", ignore_rules)


class StaticManifestVerificationTests(SimpleTestCase):
    """`build_frontend` refuses to finish on a manifest that does not match the disk."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.init_module = _load_init_module()

    def setUp(self) -> None:
        super().setUp()
        # The module is imported once per class and shared; put APP_DIR back so
        # a later test does not inherit a temporary directory that is gone.
        original = self.init_module.APP_DIR
        self.addCleanup(setattr, self.init_module, "APP_DIR", original)

    def _initializer(self):
        """An initializer with no constructor run, since none of it is needed here."""
        return object.__new__(self.init_module.DjangoProjectInitializer)

    def _write_manifest(self, root: pathlib.Path, paths: dict[str, str], *, create: tuple[str, ...]) -> None:
        static_root = root / "frontend" / "static"
        static_root.mkdir(parents=True, exist_ok=True)
        for target in create:
            written = static_root / target
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_bytes(b"x")
        (static_root / "staticfiles.json").write_text(json.dumps({"version": "1.1", "paths": paths}))

    #: The smallest manifest that is actually complete: one output from sass and
    #: one from the bundler.
    _COMPLETE = {
        "dashboard/style.css": "dashboard/style.abc123.css",
        "dashboard/js/core.js": "dashboard/js/core.def456.js",
    }

    def test_matching_manifest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write_manifest(root, dict(self._COMPLETE), create=tuple(self._COMPLETE.values()))
            self.init_module.APP_DIR = root
            self._initializer().verify_static_manifest()

    def test_a_build_step_that_produced_nothing_is_fatal(self) -> None:
        """The failure mode the existence check cannot see.

        `bun run sass` runs with raise_error=False, so a failed compile leaves no
        stylesheet, collectstatic collects nothing to replace it, and the
        resulting manifest is internally consistent with no CSS in it - at which
        point every page raises on the stylesheet's `{% static %}` call.
        """
        for dropped, remaining in (
            ("dashboard/style.css", "dashboard/js/core.js"),
            ("dashboard/js/core.js", "dashboard/style.css"),
        ):
            with self.subTest(missing=dropped), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                partial = {remaining: self._COMPLETE[remaining]}
                self._write_manifest(root, partial, create=tuple(partial.values()))
                self.init_module.APP_DIR = root
                with self.assertRaises(self.init_module.UnrecoverableError):
                    self._initializer().verify_static_manifest()

    def test_missing_target_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            # Complete on paper, and one of the two files is not on disk.
            self._write_manifest(root, dict(self._COMPLETE), create=("dashboard/js/core.def456.js",))
            self.init_module.APP_DIR = root
            with self.assertRaises(self.init_module.UnrecoverableError):
                self._initializer().verify_static_manifest()

    def test_backslash_separator_is_fatal_even_though_the_file_exists(self) -> None:
        """A Windows-generated manifest names files that exist and URLs that never match.

        51 of the shipped manifest's 166 entries looked like this. On Linux a
        backslash is a legal filename character, so an existence check alone
        passes - which is why the separator is tested separately.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._write_manifest(
                root,
                {**self._COMPLETE, "dashboard/images/logo.png": "dashboard\\images\\logo.abc123.png"},
                create=(*self._COMPLETE.values(), "dashboard\\images\\logo.abc123.png"),
            )
            self.init_module.APP_DIR = root
            with self.assertRaises(self.init_module.UnrecoverableError):
                self._initializer().verify_static_manifest()

    def test_unreadable_manifest_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "frontend" / "static").mkdir(parents=True)
            self.init_module.APP_DIR = root
            with self.assertRaises(self.init_module.UnrecoverableError):
                self._initializer().verify_static_manifest()
