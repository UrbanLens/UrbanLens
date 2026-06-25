from __future__ import annotations

import logging

from django.apps import AppConfig

from urbanlens.core.controllers.backups import DatabaseBackup

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        # Create an instance of DatabaseBackup to schedule backups
        DatabaseBackup()
