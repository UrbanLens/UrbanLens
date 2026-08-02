from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import re
from shutil import which
import subprocess  # nosec B404

from django.core.cache import cache

from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

# Matches this class's own `backup_<YYYYMMDD>_<HHMMSS>.sql` naming scheme, so retention
# counting/purging never treats a stray non-backup file (a `.gitkeep`, a stray log, a
# leftover `.tmp` from a killed pg_dump) as a real backup.
BACKUP_FILENAME_RE = re.compile(r"^backup_\d{8}_\d{6}\.sql$")

# cache.add() is atomic across processes/workers, unlike a threading.Lock (which only
# protects a single process) - see the comment on schedule_backup() for why this matters.
_SCHEDULE_LOCK_CACHE_KEY = "urbanlens:backup:schedule-lock"
_SCHEDULE_LOCK_TIMEOUT_SECONDS = 300


def is_backup_filename(name: str) -> bool:
    """Check whether a filename matches this class's own backup-file naming scheme.

    Args:
        name: A bare filename (no directory component) to check.

    Returns:
        True if the name looks like a backup this class created.
    """
    return bool(BACKUP_FILENAME_RE.match(name))


class DatabaseBackup:
    """Creates and manages scheduled/on-demand PostgreSQL database backups via ``pg_dump``."""

    def __init__(self, *, auto_schedule: bool = True):
        """Initialize the backup manager.

        Args:
            auto_schedule: When True (the default, used by ``CoreConfig.ready()`` on
                application startup), immediately checks whether a scheduled backup is due
                and enqueues one if so. Pass False when constructing an instance purely to
                run an on-demand/task-driven backup (see ``tasks._run_database_backup``).
        """
        self.backup_dir = settings.backups_dir
        self.backup_retention = settings.backup_retention

        if auto_schedule:
            self.schedule_backup()

    def create_backup_dir(self) -> bool:
        """Create the backup directory if it doesn't already exist.

        Returns:
            True if the directory was just created, False if it already existed or
            creation failed.
        """
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
        """Delete backups beyond ``self.backup_retention``, oldest first.

        Only files matching this class's own backup filename pattern are considered, so a
        stray non-backup file placed in the directory is never counted toward retention or
        deleted alongside real backups.
        """
        backup_files = [f for f in os.listdir(self.backup_dir) if is_backup_filename(f)]

        # Sort the files by modification time in descending order
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

    def run(self) -> bool:
        """Run ``pg_dump`` and purge old backups per the retention policy.

        Returns:
            True if the backup completed successfully, False otherwise.
        """
        # Belt-and-suspenders: callers (e.g. tasks._run_database_backup) already create this
        # directory explicitly before calling run(), but this method should be safe to call
        # on its own too.
        self.create_backup_dir()

        backup_filename = f"backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sql"
        final_path = os.path.join(self.backup_dir, backup_filename)
        # pg_dump writes to a temp path first; only a successful run gets renamed to the
        # real filename. This makes a mid-dump process death (OOM kill, container restart)
        # leave behind an obviously-partial `.tmp` file instead of a truncated file under the
        # real backup name that backup_files()/purge_old_backups() would otherwise treat as a
        # normal completed backup.
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
            str(Path(pg_dump).resolve()),
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
            subprocess.run(pg_dump_command, check=True, env=env)  # nosec B603
        except subprocess.CalledProcessError as e:
            logger.exception("Error occurred while performing database backup: %s", e)
            with suppress(OSError):
                os.remove(temp_path)
            return False

        # A single filesystem rename is effectively atomic, so backup_files()/
        # purge_old_backups() can never observe a partially-written file under the final name.
        os.replace(temp_path, final_path)
        logger.info("Backup completed successfully: %s", backup_filename)
        self.purge_old_backups()

        return True

    def schedule_backup(self) -> bool:
        """Enqueue a scheduled backup task if one is due and not already in flight.

        Returns:
            True if a backup task was enqueued, False if none was due or one is already
            pending.
        """
        from urbanlens.dashboard.services.admin.backups import scheduled_backup_due
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_scheduled_database_backup

        if not scheduled_backup_due():
            return False
        # scheduled_backup_due() only sees a backup as "done" once its file lands on disk,
        # which can be minutes after this check for a slow dump - cache.add() (atomic across
        # processes, unlike a threading.Lock) stops a burst of near-simultaneous callers right
        # at the due-threshold from each enqueuing their own redundant backup task before the
        # first one finishes.
        if not cache.add(_SCHEDULE_LOCK_CACHE_KEY, value=True, timeout=_SCHEDULE_LOCK_TIMEOUT_SECONDS):
            return False
        return safely_enqueue_task(run_scheduled_database_backup) is not None
