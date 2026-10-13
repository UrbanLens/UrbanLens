"""
Migrate the test database once per migration set, and create every test database as a copy of it.

Building the schema from migrations takes about 6.5 minutes, nearly all of it Python, and pytest-django pays it
once per pytest-xdist worker and once per new ``UL_TEST_DB_NAME``. ``CREATE DATABASE ... TEMPLATE`` copies the
migrated database in about a second. Django already supports this: ``DATABASES[...]["TEST"]["TEMPLATE"]`` names the
database every test database is created from, and ``migrate`` then finds nothing left to apply.

The template is called ``test_template_<hash>``, where the hash covers every installed app's migration files and
the ``urbanlens`` modules they import, so a changed migration builds a new template rather than reusing a stale one.
It is marked ``IS_TEMPLATE`` and refuses connections, and the host's disk cleanup, which drops idle ``test_*``
databases, skips it by both its flag and its name. It is migrated as ``test_tplbuild_<hash>`` and renamed when
complete, so an abandoned build is left to that cleanup. Concurrent sessions and workers wait on a Postgres advisory
lock while one of them builds it. The four most recently used templates are kept; older ones are dropped here.

``UL_TEST_DB_TEMPLATE=0`` turns this off (every database migrates as before); ``UL_TEST_DB_TEMPLATE=rebuild`` drops
and rebuilds the current template.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper

#: Bump to invalidate every existing template, after a change to how one is built.
SCHEME_VERSION = "1"
PREFIX = "test_template_"
BUILD_PREFIX = "test_tplbuild_"
KEEP = 4
#: First key of the two-key advisory locks; the second is 0 for the shared lock, or the template's own number.
LOCK_CLASS = 0x554C
COMMENT = "urbanlens test template, last used "
#: Shared by every pytest-xdist worker of one run, so ``rebuild`` rebuilds once per run rather than once per worker.
RUN_ID = "".join(c for c in os.getenv("PYTEST_XDIST_TESTRUNUID", "") if c.isalnum()) or uuid.uuid4().hex


def setting() -> str:
    """``UL_TEST_DB_TEMPLATE``, lowercased: ``"0"``/``"off"`` disables, ``"rebuild"`` rebuilds."""
    return os.getenv("UL_TEST_DB_TEMPLATE", "1").strip().lower()


def enabled(connection: BaseDatabaseWrapper) -> bool:
    """Whether test databases for ``connection`` should be copied from a template."""
    if setting() in {"0", "off", "false", "no"}:
        return False
    return connection.vendor == "postgresql" and connection.settings_dict["TEST"].get("MIGRATE", True) is not False


def _imported_urbanlens_files(path: Path) -> set[Path]:
    """The ``urbanlens`` source files a migration imports, found without importing anything."""
    found: set[Path] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        for name in names:
            if not name.startswith("urbanlens."):
                continue
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                continue
            if spec is not None and spec.origin and spec.origin.endswith(".py"):
                found.add(Path(spec.origin))
    return found


def migrations_hash() -> str:
    """A digest of every installed app's migrations and the ``urbanlens`` modules they import."""
    import django
    from django.apps import apps

    digest = hashlib.sha256(f"{SCHEME_VERSION}\0{django.__version__}\0".encode())
    files: dict[str, Path] = {}
    for app in apps.get_app_configs():
        directory = Path(app.path) / "migrations"
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            files[f"{app.label}/{path.relative_to(directory).as_posix()}"] = path
            if app.name.startswith("urbanlens"):
                for imported in _imported_urbanlens_files(path):
                    files.setdefault(f"imports/{imported.as_posix().rsplit('/urbanlens/', 1)[-1]}", imported)
    for key in sorted(files):
        digest.update(key.encode() + b"\0" + files[key].read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def template_name(digest: str) -> str:
    return f"{PREFIX}{digest}"


def _drop_template(cursor: Any, quoted_name: str) -> None:
    """Drop a template; Postgres refuses to drop a database while it is flagged as one."""
    cursor.execute(f"ALTER DATABASE {quoted_name} WITH IS_TEMPLATE false")
    cursor.execute(f"DROP DATABASE {quoted_name}")


@contextlib.contextmanager
def _advisory_lock(cursor: Any, key: int) -> Iterator[None]:
    cursor.execute("SELECT pg_advisory_lock(%s, %s)", [LOCK_CLASS, key])
    try:
        yield
    finally:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_CLASS, key])


def _exists(cursor: Any, name: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
    return cursor.fetchone() is not None


def _record_use(cursor: Any, quoted_name: str) -> None:
    """Stamp the template's comment with the time and run; COMMENT takes no bind parameters, and this text needs no escaping."""
    stamp = dt.datetime.now(tz=dt.UTC).isoformat(timespec="seconds")
    cursor.execute(f"COMMENT ON DATABASE {quoted_name} IS '{COMMENT}{stamp} by run {RUN_ID}'")


def _built_this_run(cursor: Any, name: str) -> bool:
    cursor.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s", [name])
    row = cursor.fetchone()
    return bool(row and row[0] and row[0].endswith(f" by run {RUN_ID}"))


def _log(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def _build(connection: BaseDatabaseWrapper, cursor: Any, digest: str) -> None:
    """Migrate the template for ``digest``: built under another name and renamed, so it exists only once whole."""
    name, building = template_name(digest), BUILD_PREFIX + digest
    quote = connection.ops.quote_name
    cursor.execute(f"DROP DATABASE IF EXISTS {quote(building)}")
    test_settings = connection.settings_dict["TEST"]
    saved_name, saved_template = test_settings.get("NAME"), test_settings.get("TEMPLATE")
    original_name = connection.settings_dict["NAME"]
    test_settings["NAME"], test_settings["TEMPLATE"] = building, None
    _log(f"Migrating the test-database template {name} (once per set of migrations; later databases copy it)...")
    try:
        connection.creation.create_test_db(verbosity=0, autoclobber=True, keepdb=False)
        # Points the connection back at its own database and closes it, so nothing holds the build open.
        connection.creation.destroy_test_db(original_name, verbosity=0, keepdb=True)
    except BaseException:
        connection.close()
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(building)}")
        raise
    finally:
        test_settings["NAME"], test_settings["TEMPLATE"] = saved_name, saved_template
    cursor.execute(f"ALTER DATABASE {quote(building)} RENAME TO {quote(name)}")
    cursor.execute(f"ALTER DATABASE {quote(name)} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false")
    _record_use(cursor, quote(name))


def _touch_and_prune(connection: BaseDatabaseWrapper, cursor: Any, name: str) -> None:
    """Record that ``name`` was used, and drop all but the ``KEEP`` most recently used templates."""
    from django.db import DatabaseError

    quote = connection.ops.quote_name
    try:
        _record_use(cursor, quote(name))
    except DatabaseError as exc:  # another role's template: usable, but not ours to annotate or drop
        _log(f"Could not record the use of the test-database template {name}: {exc}")
    cursor.execute(
        "SELECT d.datname FROM pg_database d LEFT JOIN pg_shdescription s ON s.objoid = d.oid "
        "AND s.classoid = 'pg_database'::regclass "
        "WHERE d.datname ~ %s ORDER BY coalesce(s.description, '') DESC, d.datname",
        [f"^{PREFIX}[0-9a-f]{{16}}$"],
    )
    for (stale,) in cursor.fetchall()[KEEP:]:
        if stale == name:
            continue
        try:
            _drop_template(cursor, quote(stale))
        except DatabaseError as exc:  # in use by a session still copying it, or not ours to drop
            _log(f"Kept the old test-database template {stale}: {exc}")
        else:
            _log(f"Dropped the old test-database template {stale}")


def use_template(connection: BaseDatabaseWrapper, *, keepdb: bool) -> str | None:
    """
    Make sure the template for the current migrations exists, and have ``connection``'s test database copy it.

    Args:
        connection: The connection whose test database is about to be created.
        keepdb: Whether an existing test database is reused (``--reuse-db``); one that exists needs no template.

    Returns:
        The template's name, or None when the test database is reused as it is.
    """
    with connection._nodb_cursor() as cursor:
        if keepdb and _exists(cursor, connection.creation._get_test_db_name()):  # type: ignore[attr-defined]  # private, unstubbed
            return None
        digest = migrations_hash()
        name = template_name(digest)
        rebuild = setting() == "rebuild"
        if rebuild or not _exists(cursor, name):
            with _advisory_lock(cursor, int(digest[:7], 16)):
                if rebuild and _exists(cursor, name) and not _built_this_run(cursor, name):
                    _drop_template(cursor, connection.ops.quote_name(name))
                if not _exists(cursor, name):
                    _build(connection, cursor, digest)
        with _advisory_lock(cursor, 0):
            _touch_and_prune(connection, cursor, name)
    connection.settings_dict["TEST"]["TEMPLATE"] = name
    return name
