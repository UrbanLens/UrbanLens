"""Disposable accounts for the on-demand integration suite.

The suite in ``tests/integration/`` drives a *deployed* instance over HTTP: it
has no ORM, no fixtures, and no way to click a link in an email. Sign-up alone
therefore cannot produce an account it can use - ``RegistrationForm.save`` sets
``is_active = False`` and waits for a verification click - so the accounts are
provisioned here instead, by a management command run on the target deployment.

Two conventions make the accounts identifiable and safely removable, and both
are load-bearing for ``--purge``:

- :data:`INTEGRATION_USERNAME_PREFIX` on the username.
- :data:`INTEGRATION_EMAIL_DOMAIN` on the address.

Selection for deletion requires *both*, plus the account not being staff. That
is deliberately stricter than ``purge_demo_accounts``, which selects on a
username prefix alone: the demo runs against a database that holds nothing else,
while this may be pointed at a staging instance that people also use by hand.

Neither convention is reachable by an ordinary sign-up.
``services.auth.username.USERNAME_RE`` forbids ``-`` in a username, so no
registered account can carry the prefix; ``.invalid`` is reserved by RFC 2606 and
can never accept mail, so nothing can be delivered to one of these addresses by
accident.
"""

from __future__ import annotations

#: Username prefix every provisioned integration account carries. Contains a
#: hyphen specifically because the sign-up validator rejects one, so this
#: namespace cannot collide with a real account.
INTEGRATION_USERNAME_PREFIX = "e2e-"

#: Address domain every provisioned account carries. RFC 2606 reserves
#: ``.invalid``, so mail addressed here is undeliverable by definition.
INTEGRATION_EMAIL_DOMAIN = "e2e.invalid"

#: Environment variable that must be true before provisioning will run against
#: an instance whose ``UL_ENVIRONMENT`` is ``production``. It is the second of
#: two locks; ``--force`` is the first.
INTEGRATION_OVERRIDE_ENV_VAR = "UL_ALLOW_INTEGRATION_PROVISIONING"

__all__ = [
    "INTEGRATION_EMAIL_DOMAIN",
    "INTEGRATION_OVERRIDE_ENV_VAR",
    "INTEGRATION_USERNAME_PREFIX",
]
