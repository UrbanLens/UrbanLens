"""External-API routes for the AI assistant.

Owns every endpoint backed by the pluggable AI gateway: conversational requests
about a location or a user's own data, AI-assisted tagging, link extraction and
import, generated suggestions, and reading back the result of a request made
earlier.

Everything AI-shaped is collected here rather than being scattered next to the
resource it talks about, because these calls share properties nothing else in
this API has. They cost real money per invocation and must record that cost;
they are slow enough to need a job-shaped request/poll flow with a client
progress indicator; they need their own throttle, since the relevant limit is
spend rather than requests per minute; and their output is a *suggestion*, never
a fact - so no route here may write to a pin or a wiki without a separate,
explicit confirmation step from the user. Spreading them across domains would
mean re-deciding all four of those things in every module.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
