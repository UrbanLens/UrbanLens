"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    db.py                                                                                                *
*        Path:    /core/controllers/backups/db.py                                                                      *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from datetime import datetime
import logging
import os
import subprocess
from threading import Lock

from django.core.signals import request_finished

from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class DatabaseBackup:
    def __init__(self):
        self.backup_dir = settings.backups_dir
        self.backup_retention = settings.backup_retention
        self.lock = Lock()

        # Call the schedule_backup() function on application startup
        self.schedule_backup()

        # Connect the trigger_backup() function to the request_finished signal
        request_finished.connect(self.trigger_backup)

    def create_backup_dir(self) -> bool:
        if os.path.exists(self.backup_dir):
            return False

        try:
            os.makedirs(self.backup_dir)
            logger.info(f"Created backup directory: {self.backup_dir}")
            return True
        except OSError as e:
            logger.error(f"Failed to create backup directory: {self.backup_dir}. Error: {e}")

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
                    logger.info(f"Removed old backup: {file}")
                except OSError as e:
                    logger.error(f"Failed to remove old backup: {file}. Error: {e}")

    def run(self) -> bool:
        # TODO temporarily disable
        datetime.now(tz=settings.TIME_ZONE).date()

        backup_filename = f'backup_{datetime.now(tz=settings.TIME_ZONE).strftime("%Y%m%d_%H%M%S")}.sql'

        pg_dump_command = [
            "pg_dump",
            "-U",
            settings.databases["default"]["USER"],
            "-h",
            settings.databases["default"]["HOST"],
            "-p",
            str(settings.databases["default"]["PORT"]),
            settings.databases["default"]["NAME"],
            "-f",
            os.path.join(self.backup_dir, backup_filename),
        ]

        try:
            subprocess.run(pg_dump_command, check=True)
            logger.info(f"Backup completed successfully: {backup_filename}")

            # Update the last backup date
            datetime.now(tz=settings.TIME_ZONE).date()

            self.purge_old_backups()
        except subprocess.CalledProcessError as e:
            logger.error(f"Error occurred while performing database backup: {e}")
            return False

        return True

    def schedule_backup(self) -> bool:
        # TODO: Temporarily disable
        last_backup_date = datetime.now(tz=settings.TIME_ZONE).date()

        # Check if backup was already performed today
        current_date = datetime.now(tz=settings.TIME_ZONE).date()
        if last_backup_date >= current_date:
            return False

        # Acquire the lock to perform the backup
        with self.lock:
            try:
                self.create_backup_dir()
                result = self.run()
            except Exception as e:
                logger.error(f"Error occurred while scheduling database backup: {e}")
                return False

        return result

    def trigger_backup(self, _, **kwargs):
        self.schedule_backup()
