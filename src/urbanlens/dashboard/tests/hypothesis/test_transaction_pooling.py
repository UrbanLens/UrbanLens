"""What the app needs to run behind a transaction-mode PgBouncer.

A transaction pool hands each transaction whichever server connection is free, so anything held by the session
outlives the transaction on a connection the next, unrelated client inherits. Two things here did that: a named
server-side cursor declared by ``.iterator()`` outside a transaction, and ``probe_scope``'s ``SET enable_seqscan``.
"""

from __future__ import annotations

import os
import re
from unittest import mock

from django.conf import settings
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from urbanlens.core.semijoin import probe_scope
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.UrbanLens.settings._env import env_bool

#: A statement that changes ``enable_seqscan`` for the rest of the session rather than the transaction.
SESSION_SET = re.compile(r"^\s*(SET(?!\s+LOCAL)\b|RESET\b)[^;]*enable_seqscan", re.IGNORECASE)


def _seqscan() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_seqscan")
        return cursor.fetchone()[0]


class ServerSideCursorsCanBeTurnedOffTests(SimpleTestCase):
    def test_the_setting_follows_the_environment(self) -> None:
        self.assertEqual(
            settings.DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"],
            env_bool("UL_DB_DISABLE_SERVER_SIDE_CURSORS", False),
        )

    def test_server_side_cursors_stay_on_unless_asked(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(env_bool("UL_DB_DISABLE_SERVER_SIDE_CURSORS", False))


class ProbeScopeHoldsNoSessionStateTests(TestCase):
    def test_sequential_scans_are_off_inside_the_scope(self) -> None:
        with probe_scope():
            self.assertEqual(_seqscan(), "off")

    def test_the_setting_is_scoped_to_the_transaction(self) -> None:
        with CaptureQueriesContext(connection) as captured, probe_scope():
            pass
        leaks = [query["sql"] for query in captured.captured_queries if SESSION_SET.search(query["sql"])]
        self.assertEqual(leaks, [], "a pooled server connection would keep this after the client let it go")

    def test_the_enclosing_transaction_gets_its_setting_back(self) -> None:
        with transaction.atomic():
            with probe_scope():
                pass
            self.assertEqual(_seqscan(), "on")

    def test_a_scope_that_raises_leaves_nothing_behind(self) -> None:
        with self.assertRaises(RuntimeError), probe_scope():
            raise RuntimeError
        self.assertEqual(_seqscan(), "on")


class ProbeScopeOutsideATransactionTests(TransactionTestCase):
    def test_the_probes_share_one_transaction(self) -> None:
        """Under autocommit each probe would otherwise be its own transaction, free to land on a server
        connection that never saw the setting."""
        with probe_scope():
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(_seqscan(), "off")
        self.assertEqual(_seqscan(), "on")
