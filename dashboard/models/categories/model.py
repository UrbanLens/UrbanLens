from django.db.models import CharField
from dashboard.models import abstract

class Category(abstract.Model):
    """
    Records category data.
    """
    name = CharField(max_length=255)
    icon = CharField(max_length=255)

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_categories'
        get_latest_by = 'updated'
