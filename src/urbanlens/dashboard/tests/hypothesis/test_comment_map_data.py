"""Regression tests for comment map attachment parsing."""

from __future__ import annotations

import json

from django.test import RequestFactory

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.comments import _parse_map_data


class CommentMapDataParsingTests(TestCase):
	"""Comment map payloads must be safe to persist and render."""

	def setUp(self) -> None:
		"""Create a request factory for parser-only tests."""
		super().setUp()
		self.factory = RequestFactory()

	def test_malicious_markup_style_values_are_sanitized(self) -> None:
		"""Stored markup cannot inject raw HTML attributes through style fields."""
		request = self.factory.post(
			"/",
			{
				"map_data": json.dumps(
					{
						"center_lat": 40.0,
						"center_lng": -74.0,
						"markup": [
							{
								"type": "text",
								"latlngs": [[40.0, -74.0]],
								"color": '#000" onmouseover="alert(1)',
								"border_color": 'red" onclick="alert(1)',
								"stroke_width": '16" onmouseover="alert(1)',
								"label": "safe label",
							},
						],
					},
				),
			},
		)

		map_data = _parse_map_data(request)

		self.assertIsNotNone(map_data)
		shape = map_data["markup"][0]
		self.assertEqual(shape["color"], "#e74c3c")
		self.assertNotIn("border_color", shape)
		self.assertNotIn("stroke_width", shape)
		self.assertEqual(shape["label"], "safe label")

	def test_invalid_center_coordinates_are_rejected(self) -> None:
		"""Out-of-range centers do not produce stored map attachments."""
		request = self.factory.post(
			"/",
			{"map_data": json.dumps({"center_lat": 120.0, "center_lng": -74.0})},
		)

		self.assertIsNone(_parse_map_data(request))
