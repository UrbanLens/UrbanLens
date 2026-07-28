"""External-API routes for pin-list actions beyond CRUD and item membership.

See ``views_lists_extra``'s and ``urls_pin_extra.py``'s docstrings - same
wiring rules apply.

**List-level bulk delete/edit was scoped out of this pass.** The internal
``dashboard/controllers/pin_lists.py`` has no multi-list bulk action to
mirror - only item-level operations exist (add/remove/reorder pins within one
list). Inventing new bulk semantics with nothing internal to match would risk
diverging from whatever the web UI eventually grows here; see
``docs/notes/mobile_app_notes.md`` Part 7 for this noted as a deliberate gap,
not an oversight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_lists_extra

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

urlpatterns: list[URLPattern] = [
    path("lists/<slug:list_slug>/markup-map/", views_lists_extra.PinListMarkupMapView.as_view(), name="lists.markup-map"),
]
