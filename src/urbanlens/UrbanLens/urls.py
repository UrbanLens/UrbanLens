"""URL configuration for urbanlens project."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponseServerError
from django.shortcuts import render
from django.urls import include, path, re_path

from urbanlens.dashboard.controllers.account import (
    CustomLoginView,
    E2EEPasswordResetConfirmView,
    LoginTwoFactorCancelView,
    LoginTwoFactorCodeView,
    LoginTwoFactorOptionsView,
    LoginTwoFactorVerifyView,
    LoginTwoFactorView,
    PostLoginRedirectView,
    ResendVerificationView,
    SetPasswordPromptView,
    SetPasswordSkipView,
    SignupView,
    SsoAwarePasswordResetForm,
    VerifyEmailSentView,
    VerifyEmailView,
    suggest_passphrases,
    validate_password_policy,
)
from urbanlens.dashboard.controllers.health import HealthController
from urbanlens.dashboard.controllers.index import IndexController
from urbanlens.dashboard.controllers.media import MediaGateView
from urbanlens.dashboard.services.security.throttle import ANONYMOUS_EXPENSIVE, throttled
from urbanlens.dashboard.urls import urlpatterns as dashboard_urls
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

admin.autodiscover()


def _render_404_page(request: HttpRequest) -> HttpResponse:
    """Render the styled 404 page."""
    return render(request, "dashboard/pages/errors/404.html", status=404)


urlpatterns = [
    path("admin/", admin.site.urls, name="admin"),
    # Only the auth views this app uses; unlisted URLs fall through to the 404 catch-all.
    path("accounts/login/", CustomLoginView.as_view(), name="login"),
    # Passkey second factor, reached via pending user id in session.
    path("accounts/login/2fa/", LoginTwoFactorView.as_view(), name="login.2fa"),
    path("accounts/login/2fa/options/", LoginTwoFactorOptionsView.as_view(), name="login.2fa.options"),
    path("accounts/login/2fa/verify/", LoginTwoFactorVerifyView.as_view(), name="login.2fa.verify"),
    path("accounts/login/2fa/code/", LoginTwoFactorCodeView.as_view(), name="login.2fa.code"),
    path("accounts/login/2fa/cancel/", LoginTwoFactorCancelView.as_view(), name="login.2fa.cancel"),
    path("accounts/post-login/", PostLoginRedirectView.as_view(), name="post_login"),
    path("accounts/set-password/", SetPasswordPromptView.as_view(), name="account.set_password"),
    path("accounts/set-password/skip/", SetPasswordSkipView.as_view(), name="account.set_password.skip"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "accounts/password_reset/",
        throttled("password_reset", ANONYMOUS_EXPENSIVE)(
            auth_views.PasswordResetView.as_view(
                form_class=SsoAwarePasswordResetForm,
                subject_template_name="registration/password_reset_subject.txt",
                email_template_name="registration/password_reset_email.txt",
                html_email_template_name="registration/password_reset_email.html",
            ),
        ),
        name="password_reset",
    ),
    path("accounts/password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "accounts/reset/<uidb64>/<token>/",
        E2EEPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    # Registration
    path("signup/", throttled("signup", ANONYMOUS_EXPENSIVE)(SignupView.as_view()), name="signup"),
    path("accounts/suggest-passphrases/", suggest_passphrases, name="suggest_passphrases"),
    path("accounts/validate-password/", validate_password_policy, name="validate_password_policy"),
    # Email verification
    path("verify-email/sent/", VerifyEmailSentView.as_view(), name="verify_email_sent"),
    path("verify-email/<uuid:token>/", VerifyEmailView.as_view(), name="verify_email"),
    path("resend-verification/", throttled("resend_verification", ANONYMOUS_EXPENSIVE)(ResendVerificationView.as_view()), name="resend_verification"),
    path("dashboard/", include(dashboard_urls), name="dashboard"),
    # OAuth2 provider for native clients; see external_api.views.
    path("oauth/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path("health/", HealthController.as_view({"get": "check"}), name="health"),
    # Split probes; /health/ stays for compose healthchecks.
    path("health/live", HealthController.as_view({"get": "live"}), name="health-live"),
    path("health/ready", HealthController.as_view({"get": "ready"}), name="health-ready"),
    path("health/primary", HealthController.as_view({"get": "primary"}), name="health-primary"),
    path("", IndexController.as_view(), name="index"),
    # Authenticated media gate; must stay ahead of the 404 catch-all.
    path("media/<path:path>", MediaGateView.as_view(), name="media"),
]

# Demo login exists only on a demo instance; appended before the catch-all.
if app_settings.demo_mode:
    from urbanlens.dashboard.controllers.demo import DemoLoginView

    urlpatterns += [path("demo/start/", throttled("demo_start", ANONYMOUS_EXPENSIVE)(DemoLoginView.as_view()), name="demo.start")]

# Metrics endpoint; absent entirely when disabled. See controllers/metrics.py.
if app_settings.metrics_enabled:
    from urbanlens.dashboard.controllers.metrics import MetricsController

    urlpatterns += [path("metrics", MetricsController.as_view(), name="metrics")]

urlpatterns += [
    # 404 catch-all - must be last.
    re_path(".*", _render_404_page, name="404"),
]


def handler404(request: HttpRequest, exception: Exception) -> HttpResponse:
    """Render the styled 404 page for explicitly-raised Http404s."""
    return _render_404_page(request)


def handler500(request: HttpRequest) -> HttpResponse:
    """Render the styled 500 page, with bare fallback."""
    try:
        return render(request, "dashboard/pages/errors/500.html", status=500)
    except Exception:
        logger.exception("Failed to render the styled 500 page")
        return HttpResponseServerError("Server Error (500)")
