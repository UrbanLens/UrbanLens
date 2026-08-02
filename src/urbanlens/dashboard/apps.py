# Generic imports
from __future__ import annotations

from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "urbanlens.dashboard"

    def ready(self):
        from django.db.models.signals import post_save

        from urbanlens.dashboard.models.achievements.signals import connect as connect_achievement_signals
        import urbanlens.dashboard.models.aliases.signals
        import urbanlens.dashboard.models.cache.signals
        import urbanlens.dashboard.models.comments.signals
        from urbanlens.dashboard.models.labels.signals import create_default_tags
        import urbanlens.dashboard.models.links.signals
        import urbanlens.dashboard.models.location.signals
        import urbanlens.dashboard.models.markup.signals
        import urbanlens.dashboard.models.notifications.signals
        import urbanlens.dashboard.models.pin.signals
        import urbanlens.dashboard.models.pin_list.signals
        from urbanlens.dashboard.models.profile.model import Profile
        import urbanlens.dashboard.models.profile.signals
        import urbanlens.dashboard.models.trips.signals
        import urbanlens.dashboard.models.wiki.signals
        import urbanlens.dashboard.models.wiki_edit.signals
        from urbanlens.dashboard.plugins import plugin_registry

        post_save.connect(create_default_tags, sender=Profile, dispatch_uid="label_create_default_tags")

        # Achievements subscribe to a dozen unrelated models, so their receivers
        # are registered from a table rather than one import per sender.
        connect_achievement_signals()

        # Plugin discovery only imports modules and instantiates plugin
        # classes - it must never touch the database this early.
        plugin_registry.discover()
