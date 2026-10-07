"""The User-Agent this site sends to a third party: it names the site and a contact, as Wikimedia's policy and the
OpenStreetMap Foundation's require. Wikimedia answers 403 to the default ``python-requests`` one.

The contact is the project's URL, never a person's email address: every third party this site calls logs the header,
and both policies accept a URL."""

from __future__ import annotations

from typing import Final

USER_AGENT: Final = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens) python-requests/2.x"
