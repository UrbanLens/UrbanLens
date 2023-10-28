"""
	
	Metadata:
	
		File: queryset.pyi
		Project: UrbanLens
		
		Author: Jess Mann
		Email: jess@manlyphotos.com
	
		-----
	
		
		Modified By: Jess Mann
	
		-----
	
		Copyright (c) 2023 Urban Lens 
"""
from django.db import models
from djangofoundry import models

class QuerySet(models.QuerySet):
    pass

class Manager(models.Manager, QuerySet):
    def all(self) -> "QuerySet": ...
