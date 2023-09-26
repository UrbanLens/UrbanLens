"""

	Metadata:

		File: serializer.py
		Project: UrbanLens
		
		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----

		
		Modified By: Jess Mann

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
from rest_framework import serializers
from djangofoundry import models

class Serializer(models.Serializer):
	id = serializers.ReadOnlyField()

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		# Dynamically include or exclude fields based on context
		context = kwargs.get('context', {})
		exclude_fields = context.get('exclude_fields', None)
		include_fields = context.get('include_fields', None)

		fields = self.fields
		for field in list(fields.keys()):
			if exclude_fields and field in exclude_fields:
				fields.pop(field)
			elif include_fields and field not in include_fields:
				fields.pop(field)

	@classmethod
	def get_fieldnames(cls) -> list:
		return cls.Meta.fields

	@classmethod
	def get_native_fields(cls) -> list:
		"""
		Get fields that are native to this model, (i.e. normal fields), not generated or calculated properties.

		Returns:
			list: A truncated list of cls.get_fieldnames()
		"""
		fields = cls.get_fieldnames()
		for field in cls.Meta.generated_fields:
			if field in fields:
				fields.remove(field)
		return fields

	class Meta(models.Serializer.Meta):
		fields = [
			'id'
		]
		generated_fields = []
