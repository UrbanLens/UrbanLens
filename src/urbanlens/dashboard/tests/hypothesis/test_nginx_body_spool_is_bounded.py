"""nginx must not be able to spool request bodies onto the database's disk.

`client_max_body_size` is 200m and nginx buffers a request body larger than
`client_body_buffer_size` (a page or two) to `client_body_temp_path` before the
upstream sees a byte of it. That path is `/tmp`, which in these containers is
the image's writable layer - the same host filesystem that holds the
`postgres-data` volume. No login is required to send a body, and nothing bounds
how many are in flight, so anonymous POST volume is unbounded write pressure on
the disk Postgres lives on.

Response buffering is the same hazard read backwards: `proxy_max_temp_file_size`
defaults to 1024m per connection.

The fix is a sized tmpfs, so the spool is a bounded RAM area that cannot reach
the disk at all, and the failure mode when it fills is one refused upload rather
than a database with nowhere left to write. Two numbers have to hold together
for that to be true rather than merely stated, and neither file can see the
other:

- the tmpfs must be **at least** the vhost's `client_max_body_size`, or a
  legitimate maximum-size upload fails where it used to work;
- `mem_limit` must be **above** the tmpfs size, because tmpfs pages are charged
  to the container's cgroup - a tmpfs that can fill past the memory limit turns
  one oversized upload into an OOM-killed nginx, which is a worse outage than
  the one this prevents.

These read the deployment files rather than a running stack; they assert the
wiring, not the behaviour.
"""

from __future__ import annotations

import pathlib
import re

import yaml

from urbanlens.core.tests.nginx_config import directive_arguments
from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
NGINX_DIR = REPO_ROOT / "src" / "urbanlens" / "config" / "nginx"

#: Which vhost each nginx service loads, since the ceiling is written there.
VHOST_BY_SERVICE = {
    "nginx": "django.conf",
    "media-nginx": "media.conf.template",
}

_SIZE = re.compile(r"^(\d+)\s*([kmg]?)b?$", re.IGNORECASE)
_MULTIPLIER = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_COMPOSE_DEFAULT = re.compile(r"^\$\{[^:}]+:-(.*)\}$")


def _bytes(value: str) -> int:
    """Parse an nginx or docker size (``200m``, ``1g``, ``512``) into bytes."""
    match = _SIZE.match(value.strip())
    if match is None:
        raise ValueError(f"not a size: {value!r}")
    return int(match.group(1)) * _MULTIPLIER[match.group(2).lower()]


def _resolved(value: str) -> str:
    """The default a ``${VAR:-default}`` compose value falls back to.

    Nested defaults (``${A:-${B:-128m}}``) resolve to the innermost, which is
    what a deployment that sets neither variable gets.
    """
    while (match := _COMPOSE_DEFAULT.match(value.strip())) is not None:
        value = match.group(1)
    return value


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _tmpfs_mounts(service: dict) -> dict[str, dict[str, str]]:
    """``{mount point: {option: value}}`` for the service's tmpfs entries."""
    mounts = {}
    for entry in service.get("tmpfs") or []:
        target, _, raw_options = entry.partition(":")
        options = {}
        for option in raw_options.split(","):
            key, _, value = option.partition("=")
            options[key.strip()] = value.strip()
        mounts[target] = options
    return mounts


def _temp_directories() -> set[str]:
    """Every directory nginx.conf tells nginx to write temporary files into."""
    text = (NGINX_DIR / "nginx.conf").read_text()
    directories = set()
    for directive in (
        "client_body_temp_path",
        "proxy_temp_path",
        "fastcgi_temp_path",
        "uwsgi_temp_path",
        "scgi_temp_path",
    ):
        for arguments in directive_arguments(text, directive):
            directories.add(arguments[0])
    return directories


def _covering_mount(mounts: dict[str, dict[str, str]], directory: str) -> dict[str, str] | None:
    """The tmpfs whose mount point contains *directory*, if any."""
    path = pathlib.PurePosixPath(directory)
    for target, options in mounts.items():
        if path == pathlib.PurePosixPath(target) or pathlib.PurePosixPath(target) in path.parents:
            return options
    return None


class TheSpoolCannotReachTheDatabasesDiskTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.services = _compose()["services"]
        self.temp_directories = _temp_directories()

    def test_nginx_declares_temp_paths_at_all(self) -> None:
        """The negative half: with no temp paths declared, everything below is vacuous."""
        self.assertTrue(self.temp_directories, "nginx.conf declares no *_temp_path, so these tests assert nothing")

    def test_every_temp_directory_is_on_a_tmpfs(self) -> None:
        for name in VHOST_BY_SERVICE:
            service = self.services[name]
            mounts = _tmpfs_mounts(service)
            for directory in sorted(self.temp_directories):
                with self.subTest(service=name, directory=directory):
                    self.assertIsNotNone(
                        _covering_mount(mounts, directory),
                        f"{name} writes {directory} to the image's writable layer, which is the filesystem holding postgres-data",
                    )

    def test_every_tmpfs_states_a_size(self) -> None:
        """An unsized tmpfs defaults to half of host RAM, which bounds nothing useful."""
        for name in VHOST_BY_SERVICE:
            mounts = _tmpfs_mounts(self.services[name])
            for directory in sorted(self.temp_directories):
                options = _covering_mount(mounts, directory)
                if options is None:
                    continue
                with self.subTest(service=name, directory=directory):
                    self.assertIn("size", options, f"{name}'s tmpfs for {directory} has no size=")

    def test_the_tmpfs_holds_a_maximum_size_request_body(self) -> None:
        """Sized under the vhost's own ceiling, this would refuse uploads that used to work."""
        for name, vhost in VHOST_BY_SERVICE.items():
            ceilings = directive_arguments((NGINX_DIR / vhost).read_text(), "client_max_body_size")
            self.assertTrue(
                ceilings, f"{vhost} sets no client_max_body_size, so its bodies are capped at nginx's 1m default"
            )
            largest_body = max(_bytes(arguments[0]) for arguments in ceilings)
            options = _covering_mount(_tmpfs_mounts(self.services[name]), "/tmp/client_temp")
            self.assertIsNotNone(options, f"{name} has no tmpfs covering the body spool")
            with self.subTest(service=name):
                self.assertGreaterEqual(
                    _bytes(_resolved(options["size"])),
                    largest_body,
                    f"{name}'s spool is smaller than the {vhost} body it must hold",
                )

    def test_memory_limit_is_above_the_tmpfs_so_filling_it_cannot_kill_nginx(self) -> None:
        """tmpfs pages are charged to the cgroup: an OOM-killed nginx is the whole site."""
        for name in VHOST_BY_SERVICE:
            service = self.services[name]
            limit = _bytes(_resolved(service["mem_limit"]))
            for directory, options in _tmpfs_mounts(service).items():
                if "size" not in options:
                    continue
                with self.subTest(service=name, directory=directory):
                    self.assertGreater(
                        limit,
                        _bytes(_resolved(options["size"])),
                        f"{name} may fill {directory} past mem_limit and be OOM-killed",
                    )

    def test_response_buffering_is_bounded_too(self) -> None:
        """Left at nginx's default a single upstream response may spool 1024m."""
        text = (NGINX_DIR / "nginx.conf").read_text()
        settings = directive_arguments(text, "proxy_max_temp_file_size")
        self.assertTrue(settings, "proxy_max_temp_file_size is unset, so it is nginx's 1024m default per connection")
        smallest_spool = min(
            _bytes(_resolved(options["size"]))
            for name in VHOST_BY_SERVICE
            for options in _tmpfs_mounts(self.services[name]).values()
            if "size" in options
        )
        self.assertLessEqual(
            _bytes(settings[0][0]),
            smallest_spool,
            "a response may spool more than the tmpfs holding it",
        )
