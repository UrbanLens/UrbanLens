from __future__ import annotations
from djangofoundry.models import CharField
from dashboard.models import abstract

class Category(abstract.Model):
    """
    Records category data.
    """
    name = CharField(max_length=255)
    icon = CharField(max_length=255, choices=[
        ('church', 'church'),
        ('factory', 'factory'),
        ('home', 'home'),
        ('hospital', 'hospital'),
        ('school', 'school'),
        ('warehouse', 'warehouse'),
        ('office_building', 'office_building'),
        ('shopping_mall', 'shopping_mall'),
        ('hotel', 'hotel'),
        ('stadium', 'stadium'),
    ])

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_categories'
        get_latest_by = 'updated'
