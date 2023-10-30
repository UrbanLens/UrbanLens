from __future__ import annotations
from djangofoundry.models import CharField
from dashboard.models.abstract import Model

class Tag(Model):
    name = CharField(max_length=255)

    class Meta(Model.Meta):
        db_table = 'dashboard_tags'
        get_latest_by = 'updated'
