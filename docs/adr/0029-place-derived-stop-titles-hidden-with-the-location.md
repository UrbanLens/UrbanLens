---
status: accepted
date: 2026-10-07
---

# A hidden stop's title is withheld with its location when it came from the place

Decided by Jess for UrbanLens#303 ("a hidden stop shows its place's name as its title").

`TripActivity.title_from_place` records a title taken from a place search or an imported event's location. Every surface that masks a hidden stop withholds such a title with the location (`trip_visibility.shown_activity_title`), and a title its author typed is still shown, the calendar export included. A stop whose only place is such a title is withheld by its adder's setting as a located stop is.

## Considered options

- Masking every hidden stop's title, as the calendar export did after P335: rejected, since a typed title such as "Meet at the gate" is written for the other members.

## Consequences

- Migration 0069 cannot tell a typed title stored before it from a place's name, so it marks every located stop's title and every imported stop's. Such a typed title reads "Secret Location" to members who may not see the stop until its author types it again.
