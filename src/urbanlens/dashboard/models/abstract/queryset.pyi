"""

Metadata:

        File: queryset.pyi
        Project: UrbanLens

        Author: Jess Mann
        Email: jess@urbanlens.org

        -----


        Modified By: Jess Mann

        -----

        Copyright (c) 2023 Urban Lens
"""

from djangofoundry import models

class QuerySet(models.QuerySet): ...

class Manager(models.Manager, QuerySet):
    def all(self) -> QuerySet: ...
