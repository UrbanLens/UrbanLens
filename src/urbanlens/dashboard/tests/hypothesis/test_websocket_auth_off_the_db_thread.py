"""A WebSocket key check must not run a legacy key's PBKDF2 on the one thread every socket shares.

N21 H40. ``database_sync_to_async`` is thread-sensitive by default, so every call to it in a daphne
process runs in asgiref's single shared executor thread. A key issued before P146 still verifies
through ``check_password`` (~0.9s of PBKDF2) until its first use rewrites it, and one client
reconnecting in a loop with such a key would otherwise queue every other socket's database work
behind that hash. Current-format keys cost one SHA-256, but the path is the same for both, so the
verification stays off the shared thread.

Deliberately not fixed by caching the verdict: a cached credential check means a revoked key keeps
working until the entry expires.
"""

from __future__ import annotations

import threading
from unittest import mock

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from django.contrib.auth import hashers
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth import api_keys
from urbanlens.dashboard.services.auth.api_keys import _HASH_SCHEME, _PREFIX_LENGTH, KEY_LABEL, generate_api_key
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware

_PRODUCTION_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.MD5PasswordHasher",
]


def _run(coro):
    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


@database_sync_to_async
def _shared_db_thread() -> int:
    """The thread every `database_sync_to_async` call shares."""
    return threading.get_ident()


class _KeyCase(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Socket client")
        ApiKey.objects.filter(pk=self.api_key.pk).update(scopes=[ApiKeyScope.NOTIFICATIONS_READ.value])
        self.middleware = ApiKeyAuthMiddleware(lambda scope, receive, send: None)

    def _make_legacy(self) -> None:
        secret = self.raw_key[len(f"{KEY_LABEL}_") + _PREFIX_LENGTH :]
        hasher = hashers.PBKDF2PasswordHasher()
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash=hasher.encode(secret, hasher.salt(), iterations=1000))

    def _stored(self) -> str:
        return ApiKey.objects.get(pk=self.api_key.pk).key_hash


@override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
class TheLegacyHashDoesNotRunOnTheSharedThreadTests(_KeyCase):
    """Expensive is survivable; expensive on the thread everyone queues behind is not."""

    def test_a_legacy_keys_password_hash_runs_off_the_shared_database_thread(self) -> None:
        self._make_legacy()
        hashing_threads: list[int] = []
        # Patched where the verifier looks it up, not on the hashers module it was imported from.
        original = api_keys.check_password

        def _record(*args, **kwargs):
            hashing_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with mock.patch.object(api_keys, "check_password", _record):
            resolved = _run(self.middleware._resolve(self.raw_key))
        shared = _run(_shared_db_thread())

        self.assertIsNotNone(resolved, "the legacy key should still authenticate")
        self.assertEqual(len(hashing_threads), 1, f"expected one hash, saw {len(hashing_threads)}")
        self.assertNotEqual(
            hashing_threads[0], shared, "the hash ran on the thread every socket's database work shares"
        )

    def test_a_legacy_key_is_upgraded_by_the_socket_path(self) -> None:
        self._make_legacy()
        self.assertIsNotNone(_run(self.middleware._resolve(self.raw_key)))
        self.assertTrue(self._stored().startswith(_HASH_SCHEME))

    def test_a_current_format_key_never_reaches_check_password(self) -> None:
        with mock.patch.object(api_keys, "check_password", side_effect=AssertionError("PBKDF2 on a ulk1 key")):
            self.assertIsNotNone(_run(self.middleware._resolve(self.raw_key)))


class TheKeyStillAuthenticatesTests(_KeyCase):
    """The half that stops the thread assertion passing against auth that stopped working."""

    def test_a_valid_key_resolves_to_its_user(self) -> None:
        resolved = _run(self.middleware._resolve(self.raw_key))

        assert resolved is not None  # nosec B101
        user, credential = resolved
        self.assertEqual(user.pk, self.user.pk)
        self.assertIsInstance(credential, ApiKey)

    def test_a_wrong_secret_does_not_resolve(self) -> None:
        wrong = f"{KEY_LABEL}_{self.api_key.prefix}{'x' * 43}"

        self.assertIsNone(_run(self.middleware._resolve(wrong)))

    def test_an_unknown_key_does_not_resolve(self) -> None:
        self.assertIsNone(_run(self.middleware._resolve("ulk_nosuchprefixnosuchsecret")))

    def test_a_corrupt_row_does_not_resolve_or_raise(self) -> None:
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash="pbkdf2_sha256$1200000$salt")

        with override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS):
            self.assertIsNone(_run(self.middleware._resolve(self.raw_key)))

    def test_a_revoked_key_is_refused_on_the_next_connection(self) -> None:
        self.assertIsNotNone(_run(self.middleware._resolve(self.raw_key)))
        ApiKey.objects.filter(pk=self.api_key.pk).update(revoked_at=timezone.now())

        self.assertIsNone(_run(self.middleware._resolve(self.raw_key)))

    def test_last_used_is_still_recorded(self) -> None:
        _run(self.middleware._resolve(self.raw_key))

        self.assertIsNotNone(ApiKey.objects.get(user=self.user).last_used_at)
