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
from urbanlens.dashboard.urls import urlpatterns as dashboard_urls
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

admin.autodiscover()


def _render_404_page(request: HttpRequest) -> HttpResponse:
    """Render the styled 404 page with a genuine 404 status code."""
    return render(request, "dashboard/pages/errors/404.html", status=404)


urlpatterns = [
    path("admin/", admin.site.urls, name="admin"),
    # Custom login/logout/password-reset views. We deliberately enumerate only the
    # django.contrib.auth views this app actually uses (with app-branded templates)
    # instead of `include("django.contrib.auth.urls")`, which also wires up
    # password_change/password_change_done - views this app has no UI for and no
    # templates for. Anything not listed here falls through to the 404 catch-all.
    path("accounts/login/", CustomLoginView.as_view(), name="login"),
    # Optional passkey (WebAuthn) second factor - reached only via CustomLoginView
    # stashing a pending user id in the session after password verification.
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
        auth_views.PasswordResetView.as_view(
            form_class=SsoAwarePasswordResetForm,
            subject_template_name="registration/password_reset_subject.txt",
            email_template_name="registration/password_reset_email.txt",
            html_email_template_name="registration/password_reset_email.html",
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
    path("signup/", SignupView.as_view(), name="signup"),
    path("accounts/suggest-passphrases/", suggest_passphrases, name="suggest_passphrases"),
    path("accounts/validate-password/", validate_password_policy, name="validate_password_policy"),
    # Email verification
    path("verify-email/sent/", VerifyEmailSentView.as_view(), name="verify_email_sent"),
    path("verify-email/<uuid:token>/", VerifyEmailView.as_view(), name="verify_email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend_verification"),
    path("dashboard/", include(dashboard_urls), name="dashboard"),
    # OAuth2 provider (django-oauth-toolkit). Native clients (the mobile app)
    # register a *public* application here and authenticate with PKCE
    # (PKCE_REQUIRED is on globally); the tokens are honored only by the external
    # API (see external_api.views).
    #
    # This mounts django-oauth-toolkit's *whole* URL set, which is wider than the
    # authorize/token/revoke + application-management it is reached for: it also
    # exposes token introspection, the RFC 8628 device-code endpoints,
    # and the OpenID discovery documents. Neither of the first two is reachable
    # in practice today - no application is registered with the device grant, and
    # `introspect` is not among OAUTH2_PROVIDER["SCOPES"], so no token can carry
    # it - but they are mounted, so narrow this include if that stops being true.
    path("oauth/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path("health/", HealthController.as_view({"get": "check"}), name="health"),
    path("", IndexController.as_view(), name="index"),
    # Authenticated media gate - replaces the old unconditional
    # `*static(MEDIA_URL, ...)` entry (which only ever served files when
    # DEBUG=True, leaving production /media/ to an unauthenticated nginx
    # alias). Every /media/... request, dev and production alike, now goes
    # through MediaGateView, which authenticates + authorizes and then either
    # streams the file (dev) or X-Accel-Redirects to nginx (production).
    # Must stay ahead of the 404 catch-all below.
    path("media/<path:path>", MediaGateView.as_view(), name="media"),
]

# The demo login exists only on a demo instance. Registered conditionally rather
# than guarded inside the view, so an instance holding real data has no such URL
# to reach at all - a guard is a line somebody can move, an absent route is not.
# It must be appended before the catch-all below, which swallows everything.
if app_settings.demo_mode:
    from urbanlens.dashboard.controllers.demo import DemoLoginView

    urlpatterns += [path("demo/start/", DemoLoginView.as_view(), name="demo.start")]

urlpatterns += [
    # 404 catch-all - must be last. Anything not explicitly routed above (including
    # Django/library default URLs we haven't deliberately wired up) lands here.
    re_path(".*", _render_404_page, name="404"),
]


def handler404(request: HttpRequest, exception: Exception) -> HttpResponse:
    """Render the styled 404 page for explicitly-raised Http404s (e.g. missing profile/pin lookups).

    Django's built-in fallback only looks for a template literally named ``404.html`` at the
    root of a template loader path, which doesn't exist here - it lives under
    ``dashboard/pages/errors/``. Without this handler, Http404s raised inside views (as opposed
    to genuinely unmatched URLs, which fall through to the catch-all route above) render Django's
    plain-text fallback instead of the site's styled error page.
    """
    return _render_404_page(request)


def handler500(request: HttpRequest) -> HttpResponse:
    """Render the styled 500 page for uncaught server errors.

    Falls back to a bare response if rendering the styled page itself fails
    (e.g. a context processor hitting a database that's the reason we're
    here in the first place), so a second failure never masks the first.
    """
    try:
        return render(request, "dashboard/pages/errors/500.html", status=500)
    except Exception:
        logger.exception("Failed to render the styled 500 page")
        return HttpResponseServerError("Server Error (500)")
