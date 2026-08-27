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


class RewrapKdfParametersTests(TestCase):
    """A re-wrap must record the parameters the new blob was actually made with.

    Enrolment accepts stronger-than-default parameters on purpose, and stores
    them. `/rewrap` replaced `password_wrapped_secret` and left them untouched -
    but the client wraps the replacement with its own *pinned* constants, and
    must (it cannot take cost parameters from a server response for a blob it is
    about to store). So a bundle enrolled above the floor ended up advertising a
    cost its stored blob was not made with, and every later password unlock
    derived the wrong key.

    That is a permanent lockout from the password path, reachable through the
    public API by enrolling above the floor: the device holding the cached key
    loops re-wrapping, and a device without one has only the recovery key left.
    """

    def setUp(self) -> None:
        super().setUp()
        user = baker.make(User)
        user.set_password(_PASSWORD)
        user.save(update_fields=["password"])
        self.user = user
        self.profile = Profile.objects.get(user=user)
        self.client.force_login(user)

    def _enrol(self, *, opslimit: int, memlimit: int) -> None:
        response = self.client.post(
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
        self.assertNotEqual(response.status_code, 400, response.content[:300])

    def _rewrap(self, **extra):
        body = {
            "password_wrapped_secret": _b64(os.urandom(72)),
            "password_wrap_salt": _b64(os.urandom(16)),
            "current_password": _PASSWORD,
            **extra,
        }
        return self.client.post(reverse("e2ee.rewrap"), data=json.dumps(body), content_type="application/json")

    def _bundle(self) -> MessagingKeyBundle:
        bundle = MessagingKeyBundle.objects.for_profile(self.profile).first()
        assert bundle is not None
        return bundle

    def test_a_rewrap_records_the_parameters_it_was_sent(self) -> None:
        self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT + 2, memlimit=DEFAULT_KDF_MEMLIMIT * 2)

        response = self._rewrap(kdf_opslimit=DEFAULT_KDF_OPSLIMIT, kdf_memlimit=DEFAULT_KDF_MEMLIMIT)

        self.assertEqual(response.status_code, 200, response.content[:300])
        bundle = self._bundle()
        self.assertEqual(bundle.kdf_opslimit, DEFAULT_KDF_OPSLIMIT)
        self.assertEqual(bundle.kdf_memlimit, DEFAULT_KDF_MEMLIMIT)

    def test_a_rewrap_that_sends_nothing_is_recorded_as_the_defaults(self) -> None:
        """An older client sends no parameters, but always wrapped with the pinned ones.

        Leaving the bundle's own values in place is what caused the lockout, so
        "absent" has to mean the defaults rather than "unchanged".
        """
        self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT + 2, memlimit=DEFAULT_KDF_MEMLIMIT * 2)

        response = self._rewrap()

        self.assertEqual(response.status_code, 200, response.content[:300])
        bundle = self._bundle()
        self.assertEqual(bundle.kdf_opslimit, DEFAULT_KDF_OPSLIMIT)
        self.assertEqual(bundle.kdf_memlimit, DEFAULT_KDF_MEMLIMIT)

    def test_the_floor_applies_to_a_rewrap_too(self) -> None:
        """Otherwise the floor enrol enforces could be walked around one step later."""
        self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT, memlimit=DEFAULT_KDF_MEMLIMIT)
        before = self._bundle().password_wrapped_secret

        response = self._rewrap(kdf_opslimit=1, kdf_memlimit=1)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._bundle().password_wrapped_secret, before, "a refused re-wrap must not have replaced the blob")

    def test_a_recovery_only_rewrap_leaves_the_password_parameters_alone(self) -> None:
        """It replaces no password blob, so it describes no new password wrapping."""
        self._enrol(opslimit=DEFAULT_KDF_OPSLIMIT + 2, memlimit=DEFAULT_KDF_MEMLIMIT * 2)

        response = self.client.post(
            reverse("e2ee.rewrap"),
            data=json.dumps({"recovery_wrapped_secret": _b64(os.urandom(72)), "current_password": _PASSWORD}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        bundle = self._bundle()
        self.assertEqual(bundle.kdf_opslimit, DEFAULT_KDF_OPSLIMIT + 2)
        self.assertEqual(bundle.kdf_memlimit, DEFAULT_KDF_MEMLIMIT * 2)
