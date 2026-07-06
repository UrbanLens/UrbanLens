"""Parsers that turn non-Google file formats (GPX, WKT/WKB, OSM XML, Shapefile) into pin dicts.

Each module exposes one or more ``..._to_dict(raw_bytes, user_profile) -> list[dict]``
functions returning the same pin-dict shape used throughout the import pipeline
(``latitude``, ``longitude``, ``profile``, ``name``, ``description``), so callers in
``services.apis.locations.google.maps`` can dispatch to them exactly like the
existing KML/JSON parsers.
"""
