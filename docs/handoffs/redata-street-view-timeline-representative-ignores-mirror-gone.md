# REData's street-view timeline picks a date's representative without looking at `mirror_gone`

- **Status: SENT 2026-10-06.** Found while fixing UrbanLens's P325. Read from REData `origin/main` (`4f52e491`) on
  2026-10-06; nothing here was run against a REData deployment.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What REData does

`parcels/services/street_view/timeline.py::_provider_timeline` groups a point's captures by provider and date. For
each date, `_representative` picks the frame nearest the point, and a panorama wins a tie. It does not look at
`attributes.mirror_gone`, which `mirror_state` sets once every image URL a capture names has answered that the image is
gone (`404`/`410`, or KartaView's `BlobArchived`/`BlobNotFound`). A date can therefore be represented by a frame whose
`image_url` and `thumbnail_url` will not load, and whose `/street-view/{uuid}/download/` 404s, while other frames
taken that day still load. The date's `count` also includes the gone frames.

`mirror_state`'s own docstring measured this on REData staging on 2026-10-05: about 600 of 1,300 KartaView captures
were stuck that way.

## What UrbanLens did (P325)

UrbanLens now leaves out every row and capture flagged `mirror_gone` (`services/locations/redata_point_data.py`).

- **From `/locations/context/`:** it gets the captures themselves, so it groups the ones still there and picks the
  nearest of those.
- **From `/street-view/timeline/`:** it gets only `representative` per date. It does not ask for
  `include_captures=true`, which the API reference warns can run to hundreds of frames at a busy corner. A date whose
  representative is gone has no stand-in there, so UrbanLens drops the whole date, and that date's other frames are
  lost with it.

## The ask

1. In `_representative`, choose among the frames not marked `mirror_gone`, and fall back to the nearest overall only
   when every frame of the date is gone, or leave such a date out.
2. Count only frames still there in the date's `count`, or publish the gone ones separately (`gone_count`).
   UrbanLens does not display `count` today.

With (1), UrbanLens's timeline path keeps every date that still has a picture, as the context path already does. Its
filter stays as the backstop for a date whose every frame is gone.
