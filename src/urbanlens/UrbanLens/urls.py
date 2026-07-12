"""URL configuration for urbanlens project."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings as django_settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import render
from django.urls import include, path, re_path
from django.views.generic import TemplateView

from urbanlens.dashboard.controllers.account import (
    CustomLoginView,
    PostLoginRedirectView,
    ResendVerificationView,
    SignupView,
    VerifyEmailSentView,
    VerifyEmailView,
    suggest_passphrases,
)
from urbanlens.dashboard.controllers.health import HealthController
from urbanlens.dashboard.controllers.index import IndexController
from urbanlens.dashboard.urls import urlpatterns as dashboard_urls

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

admin.autodiscover()

urlpatterns = [
    path("admin/", admin.site.urls, name="admin"),
    # Override Django's default login view with our custom one (must come before accounts/ include)
    path("accounts/login/", CustomLoginView.as_view(), name="login"),
    path("accounts/post-login/", PostLoginRedirectView.as_view(), name="post_login"),
    path("accounts/", include("django.contrib.auth.urls")),
    # Registration
    path("signup/", SignupView.as_view(), name="signup"),
    path("accounts/suggest-passphrases/", suggest_passphrases, name="suggest_passphrases"),
    # Email verification
    path("verify-email/sent/", VerifyEmailSentView.as_view(), name="verify_email_sent"),
    path("verify-email/<uuid:token>/", VerifyEmailView.as_view(), name="verify_email"),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend_verification"),
    path("dashboard/", include(dashboard_urls), name="dashboard"),
    path("health/", HealthController.as_view({"get": "check"}), name="health"),
    path("", IndexController.as_view(), name="index"),
    *static(django_settings.MEDIA_URL, document_root=django_settings.MEDIA_ROOT),
    # 404 catch-all - must be last
    re_path(".*", TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name="404"),
]


def handler404(request: HttpRequest, exception: Exception) -> HttpResponse:
    """Render the styled 404 page for explicitly-raised Http404s (e.g. missing profile/pin lookups).

    Django's built-in fallback only looks for a template literally named ``404.html`` at the
    root of a template loader path, which doesn't exist here - it lives under
    ``dashboard/pages/errors/``. Without this handler, Http404s raised inside views (as opposed
    to genuinely unmatched URLs, which fall through to the catch-all route above) render Django's
    plain-text fallback instead of the site's styled error page.
    """
    return render(request, "dashboard/pages/errors/404.html", status=404)
