# Generic imports
from __future__ import annotations

from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "urbanlens.dashboard"

    def ready(self):
        # Teach Pillow to open HEIC/HEIF before anything reads an upload.
        # Registered here rather than at each call site because every path that
        # opens an image needs it - thumbnails, EXIF extraction, the GPS strip -
        # and a path that missed it would fail the way HEIC used to: silently,
        # keeping a file whose coordinates the uploader asked to have removed.
        from pillow_heif import register_heif_opener

        register_heif_opener()

        from django.core.signals import request_finished, request_started
        from django.db.models.signals import post_save

        from urbanlens.dashboard.models.achievements.signals import connect as connect_achievement_signals
        import urbanlens.dashboard.models.aliases.signals
        import urbanlens.dashboard.models.cache.signals
        import urbanlens.dashboard.models.comments.signals
        import urbanlens.dashboard.models.floorplans.signals
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

        # Memoise the SiteSettings singleton for the length of a request only - see that
        # module's docstring for why this is scoped to requests instead of cached globally.
        from urbanlens.dashboard.models.site_settings import request_cache as site_settings_cache
        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        request_started.connect(site_settings_cache.begin_scope, dispatch_uid="site_settings_cache_begin")
        request_finished.connect(site_settings_cache.end_scope, dispatch_uid="site_settings_cache_end")
        post_save.connect(site_settings_cache.invalidate, sender=SiteSettings, dispatch_uid="site_settings_cache_invalidate")

        # Achievements subscribe to a dozen unrelated models, so their receivers
        # are registered from a table rather than one import per sender.
        connect_achievement_signals()

        # Plugin discovery only imports modules and instantiates plugin
        # classes - it must never touch the database this early.
        plugin_registry.discover()
