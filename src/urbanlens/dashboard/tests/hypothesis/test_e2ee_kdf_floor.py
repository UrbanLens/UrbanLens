"""Enrolment must refuse Argon2 parameters weaker than the pinned defaults.

``MessagingKeyBundle.password_wrapped_secret`` is the user's private key wrapped
under a key derived from their password, and the security claim is that whoever
holds that blob - *including this server* - cannot open it. How expensive that
is comes down entirely to the Argon2 opslimit/memlimit the wrap used.

The enrolment endpoint took both from the request and validated only that they
were positive, so a caller could enrol with `opslimit=1, memlimit=1` and leave
its own account's wrapped private key recoverable from the password at trivial
cost. It also *stores* those values, so every later re-wrap inherits them.

A floor costs nothing in compatibility: the server's default has always been
``(2, 64 MiB)`` - one migration, never changed - and the real client sends
exactly those constants (`e2ee-crypto.KDF_OPSLIMIT`/`KDF_MEMLIMIT`, pinned to
match). Stronger values are still accepted so a future client can raise them
without a server change.
"""

from __future__ import annotations

import base64
import json
import os

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
from urbanlens.dashboard.models.e2ee.key_bundle import DEFAULT_KDF_MEMLIMIT, DEFAULT_KDF_OPSLIMIT
from urbanlens.dashboard.models.profile.model import Profile

_PASSWORD = "correct-horse-battery-staple"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class EnrolKdfFloorTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        user = baker.make(User)
        user.set_password(_PASSWORD)
        user.save(update_fields=["password"])
        self.profile = Profile.objects.get(user=user)
        self.client.force_login(user)

    def _enrol(self, *, opslimit: int, memlimit: int):
        return self.client.post(
            reverse("e2ee.enroll"),
            data=json.dumps(
                {
                    "public_key": _b64(os.urandom(32)),
                    "recovery_wrapped_secret": _b64(os.urandom(72)),
                    "password_wrapped_secret": _b64(os.urandom(72)),
                    "password_wrap_salt": _b64(os.urandom(16)),
                    "kdf_opslimit": opslimit,
                    "kdf_memlimit": memlimit,
                    "current_password": _PASSWORD,
                },
            ),
            content_type="application/json",
        )

    def test_weak_opslimit_is_refused(self) -> None:
        response = self._enrol(opslimit=1, memlimit=DEFAULT_KDF_MEMLIMIT)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MessagingKeyBundle.objects.for_profile(self.profile).exists())

    def test_weak_memlimit_is_refused(self) -> None:
        response = self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT, memlimit=8192)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MessagingKeyBundle.objects.for_profile(self.profile).exists())

    def test_default_parameters_are_accepted(self) -> None:
        """The floor must not reject what the real client actually sends."""
        response = self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT, memlimit=DEFAULT_KDF_MEMLIMIT)

        self.assertNotEqual(response.status_code, 400, response.content[:300])

    def test_stronger_than_default_is_still_accepted(self) -> None:
        """A future client must be able to raise the parameters without a server change."""
        response = self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT + 1, memlimit=DEFAULT_KDF_MEMLIMIT * 2)

        self.assertNotEqual(response.status_code, 400, response.content[:300])
