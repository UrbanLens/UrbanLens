"""A WebSocket key check runs PBKDF2 on the one thread every socket shares.

N21 H40. `ApiKeyAuthMiddleware._resolve` is decorated `@database_sync_to_async`,
whose `thread_sensitive` defaults to True - so it runs in asgiref's single
shared executor thread, the same one every other `database_sync_to_async` call
in the daphne process uses for its database work. Inside it,
`authenticate_api_key` calls `check_password`, which is a deliberately expensive
password hash.

So the cost is not the problem; where it is paid is. One client reconnecting in
a loop with `?key=` occupies that thread for a hash at a time, and every other
socket's database work in the process queues behind it. The hashing must stay
expensive - that is the whole point of it - so the fix is to stop doing it
somewhere that blocks everybody else.

The lookup stays on the shared thread: it is one indexed query by the key's
public prefix, which is cheap and needs the connection handling
`database_sync_to_async` provides. Only the hash moves.

Deliberately not fixed by caching the verdict: a cached credential check means a
revoked key keeps working until the entry expires, which trades an availability
problem for a security one.
"""

from __future__ import annotations

import threading

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.websocket_auth import ApiKeyAuthMiddleware


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
        api_key, self.raw_key = generate_api_key(self.user, "Socket client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.NOTIFICATIONS_READ.value])
        self.middleware = ApiKeyAuthMiddleware(lambda scope, receive, send: None)


class TheHashDoesNotRunOnTheSharedThreadTests(_KeyCase):
    """Expensive is fine; expensive on the thread everyone queues behind is not."""

    def test_the_password_hash_runs_off_the_shared_database_thread(self) -> None:
        from unittest import mock

        from urbanlens.dashboard import websocket_auth

        hashing_threads: list[int] = []
        # Patched on the module under test, not on the hashers module it imports
        # from: a `from x import y` binds the name here at import time, so
        # patching the source would replace something this code never looks at.
        original = websocket_auth.check_password

        def _record(*args, **kwargs):
            hashing_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with mock.patch.object(websocket_auth, "check_password", _record):
            resolved = _run(self.middleware._resolve(self.raw_key))
        shared = _run(_shared_db_thread())

        self.assertIsNotNone(resolved, "the key should still authenticate")
        self.assertEqual(len(hashing_threads), 1, f"expected one hash, saw {len(hashing_threads)}")
        self.assertNotEqual(
            hashing_threads[0], shared, "the hash ran on the thread every socket's database work shares"
        )


class TheKeyStillAuthenticatesTests(_KeyCase):
    """The half that stops the thread assertion passing against auth that stopped working."""

    def test_a_valid_key_resolves_to_its_user(self) -> None:
        resolved = _run(self.middleware._resolve(self.raw_key))

        assert resolved is not None  # nosec B101
        user, credential = resolved
        self.assertEqual(user.pk, self.user.pk)
        self.assertIsInstance(credential, ApiKey)

    def test_a_wrong_secret_does_not_resolve(self) -> None:
        prefix = self.raw_key.rsplit("_", 1)[0]

        self.assertIsNone(_run(self.middleware._resolve(f"{prefix}_wrongsecretwrongsecret")))

    def test_an_unknown_key_does_not_resolve(self) -> None:
        self.assertIsNone(_run(self.middleware._resolve("ul_nosuchprefix_nosuchsecret")))

    def test_last_used_is_still_recorded(self) -> None:
        _run(self.middleware._resolve(self.raw_key))

        self.assertIsNotNone(ApiKey.objects.get(user=self.user).last_used_at)
