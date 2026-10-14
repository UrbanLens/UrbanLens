# Generic imports
from __future__ import annotations

import csv

from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_out
from django.core.signals import request_finished, request_started
from django.db.models.signals import post_delete, post_save
from pillow_heif import register_heif_opener


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "urbanlens.dashboard"

    def ready(self):
        register_heif_opener()

        # csv caps a cell at 128 KiB by default; imports allow larger cells. Process-wide
        # because csv keeps one limit; 2**31 - 1 fits a C long everywhere, below the 1 GB upload-entry cap.
        csv.field_size_limit(2**31 - 1)

        from urbanlens.core.integer_dumpers import install_checked_integer_dumpers

        install_checked_integer_dumpers()

        # Importing the module registers its system checks; media authorization is
        # default-deny so unauthorized file families fail at startup, not 404.
        import urbanlens.dashboard.checks

        # drf-spectacular resolves target_class by mutating a shared attribute with no
        # lock - unsafe under threaded workers. See schema.patch_extension_thread_safety.
        from urbanlens.dashboard.external_api.schema import patch_extension_thread_safety

        patch_extension_thread_safety()

        # channels_redis's backup-queue script declares no keys, which Dragonfly rejects outright
        from urbanlens.dashboard.services.core.channels_redis_dragonfly_patch import patch_backup_queue_script

        patch_backup_queue_script()

        from urbanlens.dashboard.models.achievements.signals import connect as connect_achievement_signals
        import urbanlens.dashboard.models.aliases.signals
        import urbanlens.dashboard.models.api_rate_limit.signals
        import urbanlens.dashboard.models.cache.signals
        import urbanlens.dashboard.models.calendar_sync.signals
        import urbanlens.dashboard.models.comments.signals
        import urbanlens.dashboard.models.floorplans.signals
        import urbanlens.dashboard.models.images.signals
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
        import urbanlens.dashboard.models.wiki_edit.signals  # noqa: F401 - imported for the @receiver registrations, like every signals module above it
        from urbanlens.dashboard.plugins import plugin_registry

        # Removes replaced or orphaned icon/avatar files; Django never removes FileField files,
        # and stranded files are indistinguishable from undisclosed ones.
        from urbanlens.dashboard.services.media.file_cleanup import connect as connect_file_cleanup

        post_save.connect(create_default_tags, sender=Profile, dispatch_uid="label_create_default_tags")
        connect_file_cleanup()

        from urbanlens.dashboard.services.map.tile_authorisation import forget_tile_viewer

        user_logged_out.connect(forget_tile_viewer, dispatch_uid="basemap_tile_auth_forget")

        # Memoises the SiteSettings singleton for a request only; see that module for why.
        from urbanlens.dashboard.models.site_settings import request_cache as site_settings_cache
        from urbanlens.dashboard.models.site_settings.model import SiteSettings
        from urbanlens.dashboard.plugins.hooks import hooks

        # Plugin discovery must never touch the database this early - and it runs
        # before the signal connections below so plugin-contributed senders
        # (cache-invalidation filters) are registered when they connect.
        plugin_registry.discover()

        # Caches navbar subscriptions across requests; see that module for invalidation.
        from urbanlens.dashboard.models.subscriptions.access_state import connect_invalidation as connect_access_invalidation
        from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole, UserSubscription

        connect_access_invalidation()

        request_started.connect(site_settings_cache.begin_scope, dispatch_uid="site_settings_cache_begin")
        request_finished.connect(site_settings_cache.end_scope, dispatch_uid="site_settings_cache_end")
        post_save.connect(site_settings_cache.invalidate, sender=SiteSettings, dispatch_uid="site_settings_cache_invalidate")
        # Plugins watch their own tables through the site_settings_invalidation_senders filter.
        for sender in (UserSubscription, SubscriptionRole, *hooks.apply_filters("site_settings_invalidation_senders", [])):
            post_save.connect(
                site_settings_cache.invalidate,
                sender=sender,
                dispatch_uid=f"site_settings_cache_invalidate_{sender.__name__}_save",
            )
            post_delete.connect(
                site_settings_cache.invalidate,
                sender=sender,
                dispatch_uid=f"site_settings_cache_invalidate_{sender.__name__}_delete",
            )

        # Achievements fan out to many models, so receivers register from a table.
        connect_achievement_signals()

        # rule_key choices come from this registry, so populate before forms render it.
        # Registration only builds dataclasses.
        from urbanlens.dashboard.services.reputation.builtin_rules import register_builtin_rules

        register_builtin_rules()

        # Ledger write is synchronous - see that module's docstring.
        from urbanlens.dashboard.models.reputation.signals import connect as connect_reputation_signals

        connect_reputation_signals()

        # Model admins contributed by plugins (billing tables and the like) register
        # here rather than in the main admin module, which never imports them.
        from django.contrib import admin
        from django.contrib.admin.sites import AlreadyRegistered

        for model, admin_class in plugin_registry.model_admins():
            try:
                admin.site.register(model, admin_class)
            except AlreadyRegistered:
                continue

        # Plugins name the origins their pages load. django-csp reads the policy per request, so appending here is enough;
        # the enforcing and report-only policies share one directives dict, hence the de-duplication.
        from django.conf import settings as django_settings

        for policy_name in ("CONTENT_SECURITY_POLICY", "CONTENT_SECURITY_POLICY_REPORT_ONLY"):
            policy = getattr(django_settings, policy_name, None)
            if policy:
                for directive, origins in plugin_registry.csp_sources().items():
                    existing = policy["DIRECTIVES"].setdefault(directive, [])
                    existing.extend(origin for origin in origins if origin not in existing)

        # Logs callable egress once per process after discovery, so overrides are never silent.
        from urbanlens.dashboard.services.core.egress import log_egress_policy

        log_egress_policy()
