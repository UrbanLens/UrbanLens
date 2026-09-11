"""One account must not be able to hold every WebSocket slot on the site.

Authorization on these sockets is thorough, and `InboundVolumeMixin` bounds how
fast an account may *send* on them - its docstring even says the shared tier "is
what stops one account opening fifty sockets", which is true of flooding from
fifty and not of holding them. An idle socket sends nothing, so it is charged
nothing, while still occupying one of nginx's `worker_connections` (1024 per
worker, shared with every HTTP request) and a slot in the single daphne behind
them (N21 H10).

Two properties matter more than the count itself:

* **it fails open.** A cap that cannot read its counter must allow, exactly as
  the request throttle does - a Valkey outage already degrades the site, and
  turning it into "nobody may open a socket" makes an outage worse rather than
  safer. This is the opposite of the single-flight guard, where proceeding blind
  starts a second copy of the most expensive work, and the difference is which
  way the failure hurts.
* **a crashed worker must not lock an account out.** A plain counter would be
  incremented and never decremented. The claims are a sorted set scored by time,
  so leftovers age out on their own.
"""

from __future__ import annotations

import pathlib
import time
from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from model_bakery import baker

from urbanlens.core.tests import socket_clients
from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.consumers import UserNotificationConsumer
from urbanlens.dashboard.services.security import socket_budget

SETTING_NAME = "WEBSOCKET_MAX_SOCKETS_PER_ACCOUNT"

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


class _BudgetCase(SimpleTestCase):
    """A budget backed by an in-memory store rather than a real socket."""

    def setUp(self) -> None:
        super().setUp()
        self.store = FakeRedis()
        patch = mock.patch.object(socket_budget, "_client", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)


class TheCeilingExistsTests(_BudgetCase):
    """An override_settings of a name nothing reads configures nothing."""

    def test_a_setting_names_the_maximum(self) -> None:
        from django.conf import settings

        self.assertTrue(hasattr(settings, SETTING_NAME))

    def test_the_name_is_one_production_reads(self) -> None:
        with override_settings(**{SETTING_NAME: 3}):
            self.assertEqual(socket_budget.max_sockets_per_account(), 3)


@override_settings(**{SETTING_NAME: 3})
class TheAllowanceIsEnforcedTests(_BudgetCase):
    """The rule, from both sides - a cap nothing accepts is not a cap."""

    def test_connections_within_the_allowance_are_claimed(self) -> None:
        for index in range(3):
            self.assertTrue(socket_budget.claim("user:1", f"chan-{index}"), f"connection {index} was refused")

        self.assertEqual(socket_budget.open_count("user:1"), 3)

    def test_the_one_past_the_allowance_is_refused(self) -> None:
        for index in range(3):
            socket_budget.claim("user:1", f"chan-{index}")

        self.assertFalse(socket_budget.claim("user:1", "chan-3"))

    def test_a_refused_connection_does_not_occupy_the_allowance(self) -> None:
        """Or the refusal would cost the account the slot it was refused."""
        for index in range(3):
            socket_budget.claim("user:1", f"chan-{index}")
        socket_budget.claim("user:1", "chan-3")

        self.assertEqual(socket_budget.open_count("user:1"), 3)

    def test_releasing_one_makes_room_for_the_next(self) -> None:
        for index in range(3):
            socket_budget.claim("user:1", f"chan-{index}")

        socket_budget.release("user:1", "chan-1")

        self.assertTrue(socket_budget.claim("user:1", "chan-3"))

    def test_one_account_cannot_spend_another_s_allowance(self) -> None:
        """The whole point: the isolation is per account."""
        for index in range(3):
            socket_budget.claim("noisy:1", f"chan-{index}")

        self.assertTrue(socket_budget.claim("quiet:2", "chan-0"))

    def test_reclaiming_the_same_connection_is_not_a_second_one(self) -> None:
        """A reconnect that reuses a channel name must not count twice."""
        for _ in range(5):
            self.assertTrue(socket_budget.claim("user:1", "chan-0"))

        self.assertEqual(socket_budget.open_count("user:1"), 1)


@override_settings(**{SETTING_NAME: 3})
class ACrashedWorkerDoesNotLockAnAccountOutTests(_BudgetCase):
    """The failure mode a plain counter would have, and the reason for the shape."""

    def test_stale_claims_age_out(self) -> None:
        stale = time.time() - socket_budget.STALE_AFTER_SECONDS - 1
        self.store.zsets["ul_ws_open:user:1"] = {f"dead-{index}": stale for index in range(3)}

        self.assertTrue(socket_budget.claim("user:1", "fresh"), "an account was locked out by a worker that went away")

    def test_a_live_claim_is_not_swept(self) -> None:
        """The sweep must not be a cap that quietly stops counting."""
        for index in range(3):
            socket_budget.claim("user:1", f"chan-{index}")

        self.assertFalse(socket_budget.claim("user:1", "chan-3"))


@override_settings(**{SETTING_NAME: 3})
class ALiveConnectionKeepsItsPlaceTests(_BudgetCase):
    """The other half of the sweep: it must expire the dead, not the living.

    These sockets live as long as their tab, which is hours - far past the sweep
    window. Without a renewal a long-lived connection would quietly stop
    counting, which is the lenient direction but would make the cap meaningless
    for exactly the connections it exists to bound.
    """

    def test_a_renewed_claim_survives_the_sweep(self) -> None:
        socket_budget.claim("user:1", "chan-0")
        self.store.zsets["ul_ws_open:user:1"]["chan-0"] = time.time() - socket_budget.STALE_AFTER_SECONDS + 1

        socket_budget.refresh("user:1", "chan-0")
        self.store.zsets["ul_ws_open:user:1"]["chan-1"] = time.time()

        self.assertEqual(socket_budget.open_count("user:1"), 2, "a renewed claim was swept as abandoned")

    def test_an_unrenewed_claim_does_not_survive(self) -> None:
        """The negative half - otherwise the test above would pass against a
        sweep that never removed anything."""
        socket_budget.claim("user:1", "chan-0")
        self.store.zsets["ul_ws_open:user:1"]["chan-0"] = time.time() - socket_budget.STALE_AFTER_SECONDS - 1

        self.assertEqual(socket_budget.open_count("user:1"), 0)

    def test_renewing_a_swept_claim_does_not_resurrect_it(self) -> None:
        """It would let a connection that lost its place take a new one without
        being counted against the allowance."""
        socket_budget.refresh("user:1", "never-claimed")

        self.assertEqual(socket_budget.open_count("user:1"), 0)

    def test_the_renewal_fits_inside_the_sweep_window(self) -> None:
        """A renewal interval at or past the window would renew nothing."""
        self.assertLess(socket_budget.REFRESH_INTERVAL_SECONDS * 2, socket_budget.STALE_AFTER_SECONDS)


class TheCapFailsOpenTests(SimpleTestCase):
    """A counter it cannot read must not become a site-wide refusal."""

    def test_a_store_that_raises_allows_the_connection(self) -> None:
        broken = mock.Mock()
        broken.pipeline.side_effect = ConnectionError("gone")

        with mock.patch.object(socket_budget, "_client", return_value=broken):
            self.assertTrue(socket_budget.claim("user:1", "chan-0"))

    def test_no_configured_store_allows_the_connection(self) -> None:
        with mock.patch.object(socket_budget, "_client", return_value=None):
            self.assertTrue(socket_budget.claim("user:1", "chan-0"))

    def test_a_release_that_raises_does_not_propagate(self) -> None:
        """Raising here would turn a store blip into a failed disconnect."""
        broken = mock.Mock()
        broken.zrem.side_effect = ConnectionError("gone")

        with mock.patch.object(socket_budget, "_client", return_value=broken):
            socket_budget.release("user:1", "chan-0")


class TheConsumersHonourTheAllowanceTests(TransactionTestCase):
    """The service is only worth having if the sockets actually ask it.

    Through a real consumer rather than by reading the source: the claim has to
    happen on the connect path, before any group is joined, and a unit test of
    `socket_budget` alone would pass just as well against a consumer that never
    called it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.store = FakeRedis()
        patch = mock.patch.object(socket_budget, "_client", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)

    def _communicator(self) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(UserNotificationConsumer.as_asgi(), "/ws/notifications/")
        comm.scope["user"] = self.user
        return comm

    @override_settings(**{SETTING_NAME: 2})
    def test_a_third_socket_is_refused(self) -> None:
        async_to_sync(self._third_socket_is_refused)()

    async def _third_socket_is_refused(self) -> None:
        opened = []
        try:
            for index in range(2):
                comm = self._communicator()
                connected, _ = await comm.connect()
                self.assertTrue(connected, f"socket {index} was refused while under the allowance")
                opened.append(comm)

            extra = self._communicator()
            connected, _ = await extra.connect()
            self.assertFalse(connected, "the account opened more sockets than its allowance")
            await extra.disconnect()
        finally:
            for comm in opened:
                await comm.disconnect()

    @override_settings(**{SETTING_NAME: 1})
    def test_a_refused_socket_joined_no_group(self) -> None:
        """Channels fires disconnect() only for a connection that accepted, so a
        refusal after group_add would leak the membership permanently."""
        async_to_sync(self._refused_socket_joined_no_group)()

    async def _refused_socket_joined_no_group(self) -> None:
        first = self._communicator()
        connected, _ = await first.connect()
        self.assertTrue(connected)

        with mock.patch("channels.layers.InMemoryChannelLayer.group_add") as joined:
            refused = self._communicator()
            await refused.connect()
            await refused.disconnect()

        joined.assert_not_called()
        await first.disconnect()

    @override_settings(**{SETTING_NAME: 1})
    def test_a_connect_that_fails_after_claiming_gives_the_place_back(self) -> None:
        """The leak that would be permanent rather than temporary.

        Channels fires `disconnect()` only for a connection that reached
        `accept()`, so a failure between the claim and the accept would hold the
        place - and the task renewing its claim would keep it fresh for the life
        of the process, so it would never even age out.
        """
        async_to_sync(self._connect_that_fails_gives_the_place_back)()

    async def _connect_that_fails_gives_the_place_back(self) -> None:
        with mock.patch.object(UserNotificationConsumer, "accept", side_effect=RuntimeError("boom")):
            broken = self._communicator()
            await broken.connect()
            await broken.disconnect()

        self.assertEqual(socket_budget.open_count(f"user:{self.user.pk}"), 0, "a failed connect kept its place")

        after = self._communicator()
        connected, _ = await after.connect()
        self.assertTrue(connected, "the account could not reconnect after a failed connect")
        await after.disconnect()

    @override_settings(**{SETTING_NAME: 1})
    def test_disconnecting_gives_the_place_back(self) -> None:
        async_to_sync(self._disconnecting_gives_the_place_back)()

    async def _disconnecting_gives_the_place_back(self) -> None:
        first = self._communicator()
        connected, _ = await first.connect()
        self.assertTrue(connected)
        await first.disconnect()

        second = self._communicator()
        connected, _ = await second.connect()
        self.assertTrue(connected, "a closed socket went on occupying the allowance")
        await second.disconnect()


class TheEdgeBoundsWhatTheAppCannotTests(SimpleTestCase):
    """The per-account cap runs after authentication, so it cannot see the case
    that costs the least to mount: a handshake that never authenticates still
    occupies an nginx connection and a daphne slot before Django closes it.

    Read off the config rather than exercised, because what is being asserted is
    that the directive is present and in the right place - nginx itself is not
    under test here.
    """

    ZONE = "ws_conn"

    def test_the_zone_is_declared(self) -> None:
        text = (REPO_ROOT / "src" / "urbanlens" / "config" / "nginx" / "nginx.conf").read_text(encoding="utf-8")

        self.assertIn(f"limit_conn_zone $binary_remote_addr zone={self.ZONE}:", text)

    def test_the_socket_location_uses_it(self) -> None:
        text = (REPO_ROOT / "src" / "urbanlens" / "config" / "nginx" / "django.conf").read_text(encoding="utf-8")
        block = text.split("location /ws/ {", 1)[1].split("}", 1)[0]

        self.assertIn(f"limit_conn {self.ZONE} ", block, "the /ws/ location does not apply the connection zone")

    def test_the_real_address_is_established_before_the_limit(self) -> None:
        """Keyed on `$binary_remote_addr`, which is the front door's address
        unless real_ip has already rewritten it - in which case every visitor
        behind the tunnel would share one budget."""
        text = (REPO_ROOT / "src" / "urbanlens" / "config" / "nginx" / "django.conf").read_text(encoding="utf-8")

        self.assertLess(text.index("real_ip_header"), text.index("limit_conn ws_conn"))


class EverySocketClientBacksOffOnTheRefusalTests(SimpleTestCase):
    """A client that retries a capacity refusal eagerly is the cap's own load.

    Found by grepping rather than by reasoning about which page opens what: the
    fix for `live-socket.ts` covered the three game consumers and nothing else,
    because the notification, direct-message and safety-chat sockets are written
    by hand in their templates. The notification one - the socket every logged-in
    page opens - reset its backoff to a second on every tab focus, and escalated
    to HTTP polling after two failures, which would have answered a refused
    socket with a stream of requests.

    The rule is about who owns the close handler. A file that constructs its own
    `WebSocket` owns it and must handle the code; a file that calls
    `openLiveSocket` inherits the handling and must not have to repeat it.
    Discovered rather than listed, so a fifth hand-rolled socket fails here
    rather than in production.
    """

    #: Close code the consumers use for "this account already holds as many
    #: sockets as it may".
    CODE = "4429"

    def _hand_rolled_clients(self) -> list[pathlib.Path]:
        """Every file that constructs a WebSocket and so owns its own onclose."""
        return socket_clients.hand_rolled(
            REPO_ROOT / "src" / "urbanlens" / "dashboard" / "templates",
            REPO_ROOT / "src" / "urbanlens" / "dashboard" / "frontend" / "ts",
        )

    def test_the_scan_finds_the_clients_it_is_meant_to_guard(self) -> None:
        """An empty list would satisfy the assertion below."""
        names = {path.name for path in self._hand_rolled_clients()}

        self.assertIn("_notification_push.html", names)
        self.assertIn("live-socket.ts", names)
        self.assertGreaterEqual(len(names), 4, f"only found {sorted(names)}")

    def test_each_handles_the_capacity_refusal(self) -> None:
        for path in self._hand_rolled_clients():
            with self.subTest(path.name):
                source = socket_clients.executable_source(path.read_text(encoding="utf-8"))

                self.assertIn(
                    self.CODE,
                    source,
                    f"{path.relative_to(REPO_ROOT)} constructs a WebSocket but does not handle close {self.CODE} "
                    "anywhere outside a comment, so a refused connection would be retried on the ordinary backoff",
                )


class TheRefusalScanReadsCodeNotCommentsTests(SimpleTestCase):
    """The scan above is satisfied by any occurrence of the string.

    Every file it guards happens to explain the close code in a comment next to
    the branch that handles it, so a file that kept the comment and lost the
    branch - a refactor, a bad merge - would still pass. Strip comments before
    looking, and prove on synthetic text that the stripping is what makes the
    difference.
    """

    def test_a_comment_only_mention_does_not_count(self) -> None:
        text = """
        ws.onclose = function (ev) {
            // 4429 means the account is at its socket ceiling.
            setTimeout(connect, retryDelay);
        };
        """

        self.assertNotIn("4429", socket_clients.executable_source(text))

    def test_a_block_comment_only_mention_does_not_count(self) -> None:
        text = "/*\n * Close 4429 is the capacity refusal.\n */\nsetTimeout(connect, 1000);"

        self.assertNotIn("4429", socket_clients.executable_source(text))

    def test_a_template_comment_only_mention_does_not_count(self) -> None:
        text = "<!-- close 4429 is the capacity refusal -->\n<script>connect();</script>"

        self.assertNotIn("4429", socket_clients.executable_source(text))

    def test_a_real_branch_still_counts(self) -> None:
        """The anti-vacuity half: stripping that removed everything would pass above."""
        text = "if (ev.code === 4429) { retryDelay = maxDelay; }"

        self.assertIn("4429", socket_clients.executable_source(text))

    def test_a_protocol_scheme_is_not_a_comment(self) -> None:
        """`'wss://'` opens no comment, and a naive strip would eat the branch after it."""
        text = "var ws = new WebSocket('wss://' + host); if (ev.code === 4429) { stop(); }"

        self.assertIn("4429", socket_clients.executable_source(text))
