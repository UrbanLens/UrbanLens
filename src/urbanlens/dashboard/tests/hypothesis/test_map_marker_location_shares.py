"""P21: a markup map shares the places it marks, whether or not its sender has a pin there.

Matching only the sender's pins let anyone launder a place: receive it, never pin it, draw it on a map, send that.
The chain ended there, where a typed coordinate in a message would have continued it.
"""

from __future__ import annotations

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.pin_share.exposure import LocationExposure
from urbanlens.dashboard.services.sharing.map_sharing import MAX_MARKED_PLACES_PER_SEND, share_markup_map_with_profile

from .test_map_pin_share_detection_integration import _LAT, _LNG, _MapShareTestCase

#: About 7 km from the sender's one pin, so nothing there matches it.
_FAR = (_LAT + 0.05, _LNG + 0.05)


def _point(latitude: float, longitude: float) -> dict:
    return {"type": "Point", "coordinates": [longitude, latitude]}


class MarkedPlaceShareTests(_MapShareTestCase):
    def _send(self, markup_map, sender: str = "a", recipient: str = "b") -> list[PinShare]:
        return share_markup_map_with_profile(self.profiles[sender], self.profiles[recipient], markup_map)

    def _marked(self, markup_type: str, geometry: dict, *, owner: str = "a"):
        markup_map = self._map(zoom=4, profile=self.profiles[owner])
        self._markup_item(markup_map, markup_type, geometry)
        return markup_map

    def test_a_marker_where_the_sender_has_no_pin_shares_its_place(self) -> None:
        markup_map = self._marked(MarkupType.PIN, _point(*_FAR))

        shares = self._send(markup_map)

        self.assertEqual(len(shares), 1)
        share = shares[0]
        self.assertIsNone(share.pin_id)
        self.assertAlmostEqual(float(share.location.latitude), _FAR[0], places=5)
        self.assertAlmostEqual(float(share.location.longitude), _FAR[1], places=5)
        self.assertEqual(share.origin, PinShareOrigin.MAP_DETECTED)
        self.assertEqual(share.status, PinShareStatus.DETECTED)
        self.assertEqual(share.detected_via_map_id, markup_map.pk)
        self.assertTrue(LocationExposure.objects.filter(profile=self.profiles["b"], share=share).exists())

    def test_a_text_label_shares_its_point(self) -> None:
        shares = self._send(self._marked(MarkupType.TEXT, _point(*_FAR)))

        self.assertEqual(len(shares), 1)
        self.assertAlmostEqual(float(shares[0].location.latitude), _FAR[0], places=5)

    def test_a_circle_around_a_property_shares_its_centre(self) -> None:
        circle = {"type": "Circle", "coordinates": [_FAR[1], _FAR[0]], "radius": 120}

        shares = self._send(self._marked(MarkupType.CIRCLE, circle))

        self.assertEqual(len(shares), 1)
        self.assertAlmostEqual(float(shares[0].location.latitude), _FAR[0], places=5)

    def test_a_circle_around_a_district_shares_no_place(self) -> None:
        circle = {"type": "Circle", "coordinates": [_FAR[1], _FAR[0]], "radius": 5000}

        self.assertEqual(self._send(self._marked(MarkupType.CIRCLE, circle)), [])

    def test_lines_arrows_squares_and_polygons_share_no_place_of_their_own(self) -> None:
        lng, lat = _FAR[1], _FAR[0]
        ring = [[lng, lat], [lng + 0.001, lat], [lng + 0.001, lat + 0.001], [lng, lat + 0.001], [lng, lat]]
        shapes = {
            MarkupType.LINE: {"type": "LineString", "coordinates": [[lng, lat], [lng + 0.01, lat]]},
            MarkupType.ARROW: {"type": "LineString", "coordinates": [[lng, lat], [lng + 0.01, lat]]},
            MarkupType.SQUARE: {"type": "Polygon", "coordinates": [ring]},
            MarkupType.POLYGON: {"type": "Polygon", "coordinates": [ring]},
        }
        for markup_type, geometry in shapes.items():
            with self.subTest(markup_type=markup_type):
                self.assertEqual(self._send(self._marked(markup_type, geometry)), [])

    def test_a_marker_on_the_sender_s_pin_shares_that_pin_once(self) -> None:
        shares = self._send(self._marked(MarkupType.PIN, _point(_LAT, _LNG)))

        self.assertEqual([share.pin_id for share in shares], [self.pin.pk])
        self.assertEqual(PinShare.objects.filter(to_profile=self.profiles["b"]).count(), 1)

    def test_a_place_the_recipient_has_pinned_is_not_shared(self) -> None:
        mine = Location.objects.create(latitude=f"{_FAR[0]:.6f}", longitude=f"{_FAR[1]:.6f}")
        Pin.objects.create(profile=self.profiles["b"], location=mine)

        self.assertEqual(self._send(self._marked(MarkupType.PIN, _point(*_FAR))), [])

    def test_sending_again_shares_no_place_twice(self) -> None:
        markup_map = self._marked(MarkupType.PIN, _point(*_FAR))
        self._send(markup_map)

        self.assertEqual(self._send(markup_map), [])
        self.assertEqual(PinShare.objects.filter(to_profile=self.profiles["b"]).count(), 1)

    def test_one_send_shares_a_bounded_number_of_places(self) -> None:
        markup_map = self._map(zoom=4)
        for index in range(MAX_MARKED_PLACES_PER_SEND + 3):
            self._markup_item(markup_map, MarkupType.PIN, _point(_FAR[0] + index * 0.01, _FAR[1]))

        self.assertEqual(len(self._send(markup_map)), MAX_MARKED_PLACES_PER_SEND)

    def test_a_received_place_redrawn_and_sent_on_keeps_its_chain(self) -> None:
        """The laundering case: told about a place, never pinned it, drew it, sent the drawing on."""
        (received,) = self._send(self._marked(MarkupType.PIN, _point(*_FAR)))

        (forwarded,) = self._send(self._marked(MarkupType.PIN, _point(*_FAR), owner="b"), sender="b", recipient="c")

        self.assertEqual(forwarded.parent_share_id, received.pk)
