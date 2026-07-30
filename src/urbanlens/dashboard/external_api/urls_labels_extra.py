"""External-API routes for label actions beyond single-label CRUD and customization.

``urls.py`` owns ``labels/`` (browse/create), ``labels/{uuid}/`` (detail/patch/
delete), and ``labels/{uuid}/customization/`` and ``.../merge/``. Priority
reorder and the bulk delete/edit/convert actions live here instead, mirroring
how ``urls_pin_extra.py`` splits pin actions away from the frozen pin CRUD
routes.

Wiring: see ``urls_pin_extra.py``'s docstring - same rules apply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_labels_bulk

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

urlpatterns: list[URLPattern] = [
    # "reorder"/"bulk" are literals a label uuid converter cannot swallow -
    # order_by_specificity guarantees that regardless of concatenation order.
    path("labels/reorder/", views_labels_bulk.LabelReorderView.as_view(), name="labels.reorder"),
    path("labels/bulk/delete/", views_labels_bulk.LabelBulkDeleteView.as_view(), name="labels.bulk.delete"),
    path("labels/bulk/edit/", views_labels_bulk.LabelBulkEditView.as_view(), name="labels.bulk.edit"),
    path("labels/bulk/convert/", views_labels_bulk.LabelBulkConvertView.as_view(), name="labels.bulk.convert"),
]
