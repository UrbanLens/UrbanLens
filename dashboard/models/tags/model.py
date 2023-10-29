from django.db import models
from dashboard.models.abstract import models

class Tag(models.Model):
    name = models.CharField(max_length=255)

    class Meta:
        db_table = 'dashboard_tags'
        get_latest_by = 'updated'
