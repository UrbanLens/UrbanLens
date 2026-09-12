from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DBRouter:
    route_app_labels = ["dashboard"]

    def db_for_read(self, model, **hints):
        """Route dashboard reads to default."""
        default = None
        if model._meta.app_label in self.route_app_labels:  # noqa: SLF001 - _meta is Django's metadata API
            default = "default"
        return getattr(model, "_database", default)

    def db_for_write(self, model, **hints):
        """Route dashboard writes to default."""
        default = None
        if model._meta.app_label in self.route_app_labels:  # noqa: SLF001 - _meta is Django's metadata API
            default = "default"
        return getattr(model, "_database", default)

    def allow_relation(self, obj1, obj2, **hints):
        return True
