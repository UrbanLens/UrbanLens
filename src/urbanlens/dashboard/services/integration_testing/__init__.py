"""Disposable accounts for the on-demand integration suite."""

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
