from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
import logging
import os
import re
from shutil import which
import subprocess  # nosec B404

from django.core.cache import cache

from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

# Only backup_<timestamp>.sql names count toward retention.
BACKUP_FILENAME_RE = re.compile(r"^backup_\d{8}_\d{6}\.sql$")

# Only reap .tmp files old enough to not be an in-flight dump.
STALE_TEMP_AGE_SECONDS = 24 * 60 * 60

# Fail here instead of hitting the Celery task limit, so temp files are cleaned up.
BACKUP_TIMEOUT_SECONDS = int(os.getenv("UL_BACKUP_TIMEOUT_SECONDS", "1800"))

# cache.add() is atomic across processes; threading.Lock is not.
_SCHEDULE_LOCK_CACHE_KEY = "urbanlens:backup:schedule-lock"
_SCHEDULE_LOCK_TIMEOUT_SECONDS = 300


def is_backup_filename(name: str) -> bool:
    """Return True when name matches this class's backup naming scheme."""
    return bool(BACKUP_FILENAME_RE.match(name))


def is_backup_temp_filename(name: str) -> bool:
    """Return True for an in-progress ``.tmp`` backup file."""
    return name.endswith(".tmp") and is_backup_filename(name.removesuffix(".tmp"))


class DatabaseBackup:
    """Create and manage scheduled/on-demand PostgreSQL backups via ``pg_dump``."""

    def __init__(self, *, auto_schedule: bool = True):
        """Initialize the backup manager.

        Args:
            auto_schedule: When True, check on startup whether a backup is due.
        """
        self.backup_dir = settings.backups_dir
        self.backup_retention = settings.backup_retention

        if auto_schedule:
            self.schedule_backup()

    def create_backup_dir(self) -> bool:
        """Create the backup directory if missing; return True if created."""
        if os.path.exists(self.backup_dir):
            return False

        try:
            os.makedirs(self.backup_dir)
            logger.info("Created backup directory: %s", self.backup_dir)
            return True
        except OSError as e:
            logger.exception("Failed to create backup directory: %s. Error: %s", self.backup_dir, e)

        return False

    def purge_old_backups(self):
        """Delete backups beyond retention, oldest first."""
        backup_files = [f for f in os.listdir(self.backup_dir) if is_backup_filename(f)]

        backup_files.sort(key=lambda x: os.path.getmtime(os.path.join(self.backup_dir, x)), reverse=True)

        if len(backup_files) > self.backup_retention:
            old_backups = backup_files[self.backup_retention :]

            for file in old_backups:
                file_path = os.path.join(self.backup_dir, file)
                try:
                    os.remove(file_path)
                    logger.info("Removed old backup: %s", file)
                except OSError as e:
                    logger.exception("Failed to remove old backup: %s. Error: %s", file, e)

        self.purge_stale_temp_files()

    def purge_stale_temp_files(self) -> None:
        """Delete unfinished ``.tmp`` dumps older than the stale threshold."""
        cutoff = datetime.now(UTC).timestamp() - STALE_TEMP_AGE_SECONDS

        for name in os.listdir(self.backup_dir):
            if not is_backup_temp_filename(name):
                continue

            path = os.path.join(self.backup_dir, name)
            try:
                if os.path.getmtime(path) > cutoff:
                    continue
                os.remove(path)
                logger.info("Removed stale partial backup: %s", name)
            except OSError as e:
                logger.exception("Failed to remove stale partial backup: %s. Error: %s", name, e)

    def run(self) -> bool:
        """Run ``pg_dump`` and purge old backups; return True on success."""
        # Also safe to call standalone, not just via the task wrapper.
        self.create_backup_dir()

        backup_filename = f"backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sql"
        final_path = os.path.join(self.backup_dir, backup_filename)
        # Write to temp first so a killed dump leaves a partial .tmp, not a truncated backup.
        temp_path = f"{final_path}.tmp"

        db = settings.databases["default"]
        db_user = db.get("USER")
        db_host = db.get("HOST") or "localhost"
        db_port = str(db.get("PORT") or 5432)
        db_name = db.get("NAME")
        db_password = db.get("PASSWORD")

        if not db_user or not db_name:
            raise RuntimeError("Database USER and NAME must be configured for backups.")

        pg_dump_name = os.environ.get("UL_PG_DUMP_BIN", "pg_dump")
        pg_dump = which(pg_dump_name)
        if pg_dump is None:
            raise FileNotFoundError(f"{pg_dump_name} executable not found on PATH")

        pg_dump_command = [
            pg_dump,
            "-U",
            db_user,
            "-h",
            db_host,
            "-p",
            db_port,
            "-w",
            db_name,
            "-f",
            temp_path,
        ]

        env = os.environ.copy()
        if db_password:
            env["PGPASSWORD"] = str(db_password)

        try:
            subprocess.run(pg_dump_command, check=True, env=env, timeout=BACKUP_TIMEOUT_SECONDS)  # nosec B603
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.exception("Error occurred while performing database backup: %s", e)
            with suppress(OSError):
                os.remove(temp_path)
            return False

        # Atomic rename so readers never see a partial file.
        os.replace(temp_path, final_path)
        logger.info("Backup completed successfully: %s", backup_filename)
        self.purge_old_backups()

        return True

    def schedule_backup(self) -> bool:
        """Enqueue a backup if due and none is already pending."""
        from urbanlens.dashboard.services.admin.backups import scheduled_backup_due
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_scheduled_database_backup

        if not scheduled_backup_due():
            return False
        # Atomic lock stops concurrent callers each enqueuing a backup.
        if not cache.add(_SCHEDULE_LOCK_CACHE_KEY, value=True, timeout=_SCHEDULE_LOCK_TIMEOUT_SECONDS):
            return False
        return safely_enqueue_task(run_scheduled_database_backup) is not None
