"""Site-admin management of subscription roles, their features and limits, and the grants that give them to users."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.controllers.site_admin import _parse_duration_months
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.subscriptions import SubscriptionRole, UserSubscription, grant_subscription
from urbanlens.dashboard.models.subscriptions.model import site_feature_choices, site_feature_values
from urbanlens.dashboard.services.core.numbers import safe_int_or_none

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)


class SiteAdminSubscriptionsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """
    Manage subscription roles and grants without exposing a general user directory.

    Every POST names an ``action``; one with none grants a role. HTMX requests get a partial or an empty response with
    an ``HX-Trigger`` event, others a redirect back with a ``saved`` or ``error`` message. Plugins add to each role's
    card through the ``site_admin.roles`` slot, posting to routes of their own.
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    ROLE_ACTIONS: ClassVar[dict[str, str]] = {
        "role_quota": "_set_quota",
        "role_email_limits": "_set_email_limits",
        "role_features": "_set_features",
        "role_rename": "_rename",
        "role_delete": "_delete",
    }

    def get(self, request: HttpRequest) -> HttpResponse:
        roles = SubscriptionRole.objects.all().annotate(
            active_grant_count=Count("user_subscriptions", filter=Q(user_subscriptions__revoked_at__isnull=True), distinct=True),
        )
        return render(
            request,
            "dashboard/pages/site_admin_subscriptions.html",
            {
                "page_name": "site-admin-subscriptions",
                "roles": roles,
                "grants": UserSubscription.objects.grants_for_site_admin(),
                "site_features": site_feature_choices(),
                "site_settings": SiteSettings.get_current(),
                "saved": request.GET.get("saved"),
                "error": request.GET.get("error"),
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request.user, User):
            return HttpResponseForbidden()
        action = request.POST.get("action", "grant")
        if action in self.ROLE_ACTIONS:
            role = SubscriptionRole.objects.get_by_slug(request.POST.get("role_slug", ""))
            if role is None:
                return self._failure(404, "Role not found - no changes saved.")
            handler: Callable[[SubscriptionRole], HttpResponse] = getattr(self, self.ROLE_ACTIONS[action])
            return handler(role)
        if action == "revoke":
            return self._revoke(request.user)
        if action == "update":
            return self._update_duration(request.user)
        if action == "role_create":
            return self._create()
        if action == "default_features":
            return self._set_default_features()
        return self._grant(request.user)

    # Responses

    @property
    def _is_htmx(self) -> bool:
        return bool(self.request.headers.get("HX-Request"))

    def _redirect(self, **params: str) -> HttpResponseRedirect:
        query = f"?{urlencode(params)}" if params else ""
        return HttpResponseRedirect(reverse("site_admin_subscriptions") + query)

    def _failure(self, status: int, message: str) -> HttpResponse:
        if self._is_htmx:
            response = HttpResponse(status=status)
            response["HX-Trigger"] = json.dumps({"showToast": {"level": "error", "message": message}})
            return response
        return self._redirect(error=message)

    def _role_saved(self, field_group: str, role_slug: str, saved: str) -> HttpResponse:
        if self._is_htmx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps({"roleSettingsSaved": {"field_group": field_group, "role": role_slug}})
            return response
        return self._redirect(saved=saved)

    def _grants_list(self, *, toast: tuple[str, str] | None = None, status: int = 200) -> HttpResponse:
        response = render(self.request, "dashboard/partials/site_admin/_subscription_grants_list.html", {"grants": UserSubscription.objects.grants_for_site_admin()}, status=status)
        if toast:
            response["HX-Trigger"] = json.dumps({"showToast": {"level": toast[0], "message": toast[1]}})
        return response

    def _selected_features(self) -> str:
        valid = site_feature_values()
        return ",".join(sorted(value for value in self.request.POST.getlist("features") if value in valid))

    # Grants

    def _grant(self, granter: User) -> HttpResponse:
        from urbanlens.dashboard.services.auth.identity import find_user_by_identifier

        identifier = self.request.POST.get("user_identifier", "").strip()
        role = SubscriptionRole.objects.get_by_slug(self.request.POST.get("role_slug", ""))
        user = find_user_by_identifier(identifier) if identifier else None
        if user is None or not user.is_active or role is None:
            if self._is_htmx:
                return self._grants_list(toast=("error", "User or role not found."), status=400)
            return self._redirect(error="User or role not found.")
        grant_subscription(user, role, granter, _parse_duration_months(self.request.POST.get("duration_months")))
        if self._is_htmx:
            return self._grants_list(toast=("success", f"Granted {role.name} to {user.username}."))
        return self._redirect(saved="granted")

    def _revoke(self, revoker: User) -> HttpResponse:
        subscription_id = safe_int_or_none(self.request.POST.get("subscription_id"))
        if UserSubscription.objects.not_revoked().filter(pk=subscription_id).update(revoked_at=timezone.now(), revoked_by=revoker):
            logger.info("Subscription grant %s revoked by user %s", subscription_id, revoker.pk)
        if self._is_htmx:
            return self._grants_list(toast=("info", "Subscription revoked."))
        return self._redirect(saved="revoked")

    def _update_duration(self, editor: User) -> HttpResponse:
        grant = UserSubscription.objects.not_revoked().filter(pk=safe_int_or_none(self.request.POST.get("subscription_id"))).first()
        if grant:
            grant.set_duration_months(_parse_duration_months(self.request.POST.get("duration_months")))
            grant.save(update_fields=["expires_at", "updated"])
            logger.info("Subscription grant %s duration changed by user %s", grant.pk, editor.pk)
        if self._is_htmx:
            return self._grants_list(toast=("success", "Subscription updated."))
        return self._redirect(saved="updated")

    # Roles

    def _create(self) -> HttpResponse:
        name = self.request.POST.get("name", "").strip()[:100]
        if not name:
            return self._redirect(error="Role name is required.")
        description = self.request.POST.get("description", "").strip()[:255]
        role = SubscriptionRole.objects.create(slug=SubscriptionRole.unique_slug(name), name=name, description=description)
        return self._redirect(saved=f'"{role.name}" role created')

    def _rename(self, role: SubscriptionRole) -> HttpResponse:
        name = self.request.POST.get("name", "").strip()[:100]
        if not name:
            return self._redirect(error="Role name is required.")
        description = self.request.POST.get("description", "").strip()[:255]
        SubscriptionRole.objects.filter(pk=role.pk).update(name=name, description=description)
        return self._redirect(saved="role renamed")

    def _delete(self, role: SubscriptionRole) -> HttpResponse:
        name = role.name
        role.delete()
        return self._redirect(saved=f'"{name}" role deleted')

    def _set_quota(self, role: SubscriptionRole) -> HttpResponse:
        raw = (self.request.POST.get("storage_quota_gb") or "").strip()
        quota: int | None = None
        if raw:
            try:
                quota = max(0, int(raw))
            except ValueError:
                return self._failure(400, "Storage quota must be a whole number of GB.")
        SubscriptionRole.objects.filter(pk=role.pk).update(storage_quota_gb=quota)
        return self._role_saved("role_quota", role.slug, "storage quota saved")

    def _set_email_limits(self, role: SubscriptionRole) -> HttpResponse:
        updates: dict[str, int | None] = {}
        for field in ("email_limit_per_hour", "email_limit_per_day", "email_limit_per_month"):
            raw = (self.request.POST.get(field) or "").strip()
            try:
                updates[field] = max(0, int(raw)) if raw else None
            except ValueError:
                return self._failure(400, "Email limits must be whole numbers.")
        SubscriptionRole.objects.filter(pk=role.pk).update(**updates)
        return self._role_saved("role_email_limits", role.slug, "email limits saved")

    def _set_features(self, role: SubscriptionRole) -> HttpResponse:
        SubscriptionRole.objects.filter(pk=role.pk).update(features=self._selected_features())
        return self._role_saved("role_features", role.slug, "features saved")

    def _set_default_features(self) -> HttpResponse:
        site_settings = SiteSettings.get_current()
        # save(), not update(): get_current() memoises the row for this request, and only post_save drops the memo.
        site_settings.default_features = self._selected_features()
        site_settings.save(update_fields=["default_features", "updated"])
        return self._role_saved("default_features", "__default__", "default features saved")
