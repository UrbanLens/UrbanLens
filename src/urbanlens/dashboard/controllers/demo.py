"""The demo instance's one-click login.

Only reachable when ``UL_DEMO_MODE`` is on, and the route itself is registered
conditionally - an instance holding real data has no such URL at all, rather
than a URL that guards itself. That distinction matters: a guard is a line of
code somebody can move, while an unregistered route cannot be reached by
mistake.

Every visit mints a **new** account. Sharing one would mean the first visitor to
delete everything, or type something unpleasant into a bio, defines the product
for everyone after them, with no owner to revert it - and two visitors editing
one account see each other's changes mid-session, which is neither a demo nor
isolated.
"""

from __future__ import annotations

import logging

from django.contrib.auth import login as auth_login
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View

logger = logging.getLogger(__name__)

#: Backend path used to log the seeded account in. Named explicitly because the
#: project runs several authentication backends and ``auth_login`` cannot pick
#: one on its own for a user it did not authenticate.
_AUTH_BACKEND = "urbanlens.dashboard.services.auth.auth_backend.EmailOrUsernameModelBackend"


class DemoLoginView(View):
    """POST: seed a throwaway account and sign in as it."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Create a demo account and log the visitor into it.

        POST rather than GET deliberately: seeding writes several hundred rows,
        and a GET would be fired by every crawler, link preview and prefetch
        that ever saw the button.

        Args:
            request: The current request.

        Returns:
            A redirect to the map, as the newly seeded user.

        Raises:
            Http404: This is not a demo instance.
        """
        from urbanlens.dashboard.services.demo.seeding import seed_demo_account
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.demo_mode:
            raise Http404

        user = seed_demo_account()
        auth_login(request, user, backend=_AUTH_BACKEND)
        logger.info("demo: signed in as %s", user.username)
        return redirect(reverse("map.view"))
