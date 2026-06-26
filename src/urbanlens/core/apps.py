from __future__ import annotations

import logging

from django.apps import AppConfig

from urbanlens.core.controllers.backups import DatabaseBackup
from urbanlens.core.version import get_git_commit_at_start

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        # Create an instance of DatabaseBackup to schedule backups
        DatabaseBackup()
        get_git_commit_at_start()
