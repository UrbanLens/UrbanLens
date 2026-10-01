"""The public demo instance: seeding, and the conventions that make it purgeable.
A demo instance runs the same image against its own database, seeded entirely with synthetic data, and is never pointed at a database holding real user content."""

from __future__ import annotations

#: Username prefix every seeded demo account carries.
#: This is the only handle the purge has, so it is a module constant rather than an inline literal,
#: and the signup validator refuses it (see ``services.auth.username``) so a real account can never
#: impersonate one.
DEMO_USERNAME_PREFIX = "demo-"

__all__ = ["DEMO_USERNAME_PREFIX"]
