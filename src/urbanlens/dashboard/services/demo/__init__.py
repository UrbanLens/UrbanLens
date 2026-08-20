"""The public demo instance: seeding, and the conventions that make it purgeable.

Isolation here is the **deployment boundary**, not a filter in application code.
A demo instance runs the same image against its own database, seeded entirely
with synthetic data, and is never pointed at a database holding real user
content. Nothing in this package is safe to enable on the real site - see
``docs/DEMO.md`` for the provisioning contract and why it was chosen over a
per-row "demo realm" column.

That choice is what keeps this package free of schema changes: a demo account
is identified by its username prefix (:data:`DEMO_USERNAME_PREFIX`), so the real
site carries no migration, no extra column, and no visibility guard on its
behalf.
"""

from __future__ import annotations

#: Username prefix every seeded demo account carries. This is the only handle
#: the purge has, so it is a module constant rather than an inline literal, and
#: the signup validator refuses it (see ``services.auth.username``) so a real
#: account can never impersonate one.
DEMO_USERNAME_PREFIX = "demo-"

__all__ = ["DEMO_USERNAME_PREFIX"]
