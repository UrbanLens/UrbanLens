from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from shutil import which
import subprocess  # nosec B404
from threading import Lock

from django.core.signals import request_finished

from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class DatabaseBackup:
    def __init__(self, *, auto_schedule: bool = True):
        self.backup_dir = settings.backups_dir
        self.backup_retention = settings.backup_retention
        self.lock = Lock()

        if auto_schedule:
            # Call the schedule_backup() function on application startup
            self.schedule_backup()

            # Connect the trigger_backup() function to the request_finished signal
            request_finished.connect(self.trigger_backup)

    def create_backup_dir(self) -> bool:
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
        backup_files = os.listdir(self.backup_dir)

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
        backup_filename = f"backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sql"

        db = settings.databases["default"]
        db_user = db.get("USER")
        db_host = db.get("HOST") or "localhost"
        db_port = str(db.get("PORT") or 5432)
        db_name = db.get("NAME")

        if not db_user or not db_name:
            raise RuntimeError("Database USER and NAME must be configured for backups.")

        pg_dump = which("pg_dump")
        if pg_dump is None:
            raise FileNotFoundError("pg_dump executable not found on PATH")

        pg_dump_command = [
            str(Path(pg_dump).resolve()),
            "-U",
            db_user,
            "-h",
            db_host,
            "-p",
            db_port,
            db_name,
            "-f",
            os.path.join(self.backup_dir, backup_filename),
        ]

        try:
            subprocess.run(pg_dump_command, check=True)  # nosec B603
            logger.info("Backup completed successfully: %s", backup_filename)
            self.purge_old_backups()
        except subprocess.CalledProcessError as e:
            logger.exception("Error occurred while performing database backup: %s", e)
            return False

        return True

    def schedule_backup(self) -> bool:
        from urbanlens.dashboard.services.backups import scheduled_backup_due
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import run_scheduled_database_backup

        if not scheduled_backup_due():
            return False
        return safely_enqueue_task(run_scheduled_database_backup) is not None

    def trigger_backup(self, _, **kwargs):
        self.schedule_backup()
