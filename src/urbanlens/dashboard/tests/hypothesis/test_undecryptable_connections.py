"""An encrypted provider connection that this process cannot decrypt is absent to readers but never destroyed by them.

A process missing a key another process already writes with (a rolling deploy mid key-rotation) reads a
perfectly good row as undecryptable. Only an explicit disconnect or a reconnect may remove it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.db.models.signals import pre_delete
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.calendar_sync.model import GoogleCalendarAccount
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.services.apis.flickr import oauth as flickr_oauth
from urbanlens.dashboard.services.apis.immich.gateway import ImmichGateway

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

type _Account = ImmichAccount | FlickrAccount | GooglePhotosAccount | GoogleCalendarAccount


class _UndecryptableConnectionContract(TestCase):
    """The contract every provider connection keeps; a subclass supplies the provider's specifics."""

    __test__ = False

    model: ClassVar[type[_Account]]
    credential_column: ClassVar[str]
    controller_module: ClassVar[str]
    disconnect_url: ClassVar[str]

    user: User
    profile: Profile

    def make_account(self) -> _Account:
        raise NotImplementedError

    def reconnect(self, secret: str) -> None:
        """Run the provider's real connect flow for ``self.profile``, granting ``secret``."""
        raise NotImplementedError

    def credential(self, account: _Account | None) -> str:
        return getattr(account, self.credential_column)

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def corrupt(self, account: _Account) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self.model._meta.db_table} SET {self.credential_column} = %s WHERE id = %s",  # noqa: S608
                ["written-under-a-key-this-process-lacks", account.pk],
            )

    def rows(self) -> int:
        return self.model._default_manager.filter(profile=self.profile).count()

    def test_an_undecryptable_connection_is_reported_absent_but_kept(self) -> None:
        self.corrupt(self.make_account())
        self.assertIsNone(self.model.objects.get_for_profile(self.profile))
        self.assertEqual(self.rows(), 1)

    def test_delete_for_profile_removes_an_undecryptable_connection(self) -> None:
        self.corrupt(self.make_account())
        self.model.objects.delete_for_profile(self.profile)
        self.assertEqual(self.rows(), 0)

    def test_delete_for_profile_removes_an_undecryptable_connection_that_delete_signals_would_load(self) -> None:
        self.corrupt(self.make_account())
        receiver = mock.Mock()
        pre_delete.connect(receiver, sender=self.model, weak=False, dispatch_uid="p169-force-row-load")
        self.addCleanup(pre_delete.disconnect, sender=self.model, dispatch_uid="p169-force-row-load")
        self.model.objects.delete_for_profile(self.profile)
        self.assertEqual(self.rows(), 0)

    def disconnect(self, url: str) -> int:
        with mock.patch(f"urbanlens.dashboard.controllers.{self.controller_module}.revoke_token", create=True):
            return self.client.post(reverse(url)).status_code

    def test_disconnecting_an_undecryptable_connection_removes_it(self) -> None:
        self.corrupt(self.make_account())
        self.assertEqual(self.disconnect(self.disconnect_url), 200)
        self.assertEqual(self.rows(), 0)

    def test_reconnecting_over_an_undecryptable_connection_replaces_it(self) -> None:
        self.corrupt(self.make_account())
        self.reconnect("fresh-secret")
        account = self.model.objects.get_for_profile(self.profile)
        self.assertIsNotNone(account)
        self.assertEqual(self.credential(account), "fresh-secret")
        self.assertEqual(self.rows(), 1)

    def test_a_readable_connection_is_updated_in_place_on_reconnect(self) -> None:
        original = self.make_account()
        self.reconnect("fresh-secret")
        account = self.model.objects.get_for_profile(self.profile)
        assert account is not None
        self.assertEqual(account.pk, original.pk)
        self.assertEqual(account.created, original.created)
        self.assertEqual(self.credential(account), "fresh-secret")


class ImmichUndecryptableConnectionTests(_UndecryptableConnectionContract):
    __test__ = True
    model = ImmichAccount
    credential_column = "api_key"
    controller_module = "immich"
    disconnect_url = "settings.immich.disconnect"

    def make_account(self) -> ImmichAccount:
        return ImmichAccount.objects.create(
            profile=self.profile, server_url="https://photos.example.com", api_key="old"
        )

    def reconnect(self, secret: str) -> None:
        with (
            mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            mock.patch.object(ImmichGateway, "ping", return_value=True),
        ):
            response = self.client.post(
                reverse("settings.immich"), {"server_url": "https://photos.example.com", "api_key": secret}
            )
        self.assertEqual(response.status_code, 200)

    def test_a_readable_connection_keeps_its_connected_at_on_reconnect(self) -> None:
        original = self.make_account()
        self.reconnect("fresh-secret")
        self.assertEqual(ImmichAccount.objects.get(pk=original.pk).connected_at, original.connected_at)

    def test_a_library_scan_is_refused_over_an_undecryptable_connection(self) -> None:
        self.corrupt(self.make_account())
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            response = self.client.post(reverse("settings.immich.scan"))
        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()


class FlickrUndecryptableConnectionTests(_UndecryptableConnectionContract):
    __test__ = True
    model = FlickrAccount
    credential_column = "oauth_token"
    controller_module = "flickr"
    disconnect_url = "settings.flickr.disconnect"

    def make_account(self) -> FlickrAccount:
        return FlickrAccount.objects.create(
            profile=self.profile, oauth_token="old", oauth_token_secret="old-secret", flickr_user_id="1@N00"
        )

    def reconnect(self, secret: str) -> None:
        cache.set("ul_flickr_request_token_req-token", {"secret": "req-secret", "pid": self.profile.id}, 600)
        grant = flickr_oauth.FlickrAccessGrant(
            oauth_token=secret, oauth_token_secret="final-secret", user_nsid="1@N00", username="tester"
        )
        with mock.patch("urbanlens.dashboard.controllers.flickr.finish_authorization", return_value=grant):
            response = self.client.get(
                reverse("settings.flickr.callback"), {"oauth_token": "req-token", "oauth_verifier": "v"}
            )
        self.assertEqual(response.status_code, 302)

    def test_a_readable_connection_keeps_its_connected_at_on_reconnect(self) -> None:
        original = self.make_account()
        self.reconnect("fresh-secret")
        self.assertEqual(FlickrAccount.objects.get(pk=original.pk).connected_at, original.connected_at)

    def test_an_import_is_refused_over_an_undecryptable_connection(self) -> None:
        self.corrupt(self.make_account())
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        with mock.patch("urbanlens.dashboard.controllers.flickr.safely_enqueue_task") as enqueue:
            response = self.client.post(reverse("pin.flickr.import", args=[pin.slug]), {"photo_ids": ["p1"]})
        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()


class GooglePhotosUndecryptableConnectionTests(_UndecryptableConnectionContract):
    __test__ = True
    model = GooglePhotosAccount
    credential_column = "access_token"
    controller_module = "google_photos"
    disconnect_url = "settings.google_photos.disconnect"

    def make_account(self) -> GooglePhotosAccount:
        return GooglePhotosAccount.objects.create(profile=self.profile, access_token="old", refresh_token="old-refresh")

    def reconnect(self, secret: str) -> None:
        state = signing.dumps({"pid": self.profile.id}, salt="google-photos-connect")
        with mock.patch(
            "urbanlens.dashboard.controllers.google_photos.exchange_code_for_tokens",
            return_value={"access_token": secret, "refresh_token": "ref", "expires_in": 3600},
        ):
            response = self.client.get(reverse("settings.google_photos.callback"), {"state": state, "code": "abc"})
        self.assertEqual(response.status_code, 302)

    def test_an_import_is_refused_over_an_undecryptable_connection(self) -> None:
        self.corrupt(self.make_account())
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        with mock.patch("urbanlens.dashboard.controllers.google_photos.safely_enqueue_task") as enqueue:
            response = self.client.post(
                reverse("pin.google_photos.import", args=[pin.slug]), {"media_item_ids": ["m1"]}
            )
        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()


class GoogleCalendarUndecryptableConnectionTests(_UndecryptableConnectionContract):
    __test__ = True
    model = GoogleCalendarAccount
    credential_column = "access_token"
    controller_module = "calendar_sync"
    disconnect_url = "settings.google_calendar.disconnect"

    def make_account(self) -> GoogleCalendarAccount:
        return GoogleCalendarAccount.objects.create(
            profile=self.profile, access_token="old", refresh_token="old-refresh"
        )

    def reconnect(self, secret: str) -> None:
        state = signing.dumps({"pid": self.profile.id, "next": "settings.view"}, salt="google-calendar-connect")
        with mock.patch(
            "urbanlens.dashboard.controllers.calendar_sync.exchange_code_for_tokens",
            return_value={"access_token": secret, "refresh_token": "ref", "expires_in": 3600},
        ):
            response = self.client.get(reverse("trips.calendar.callback"), {"state": state, "code": "abc"})
        self.assertEqual(response.status_code, 302)

    def test_disconnecting_from_the_trips_page_removes_an_undecryptable_connection(self) -> None:
        self.corrupt(self.make_account())
        self.assertEqual(self.disconnect("trips.calendar.disconnect"), 200)
        self.assertEqual(self.rows(), 0)
