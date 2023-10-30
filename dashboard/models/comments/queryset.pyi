from __future__ import annotations
from django.db import models
from dashboard.models.abstract import models

class QuerySet(models.QuerySet):
    pass

class Manager(models.Manager, QuerySet):
    def all(self) -> "QuerySet": ...