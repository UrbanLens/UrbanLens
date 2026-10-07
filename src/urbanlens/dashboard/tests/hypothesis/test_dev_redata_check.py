"""`dashboard.W003`: a development box pointed at somebody's real REData."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from unittest import mock

from django.conf import settings as django_settings
from django.test import TestCase, override_settings

from urbanlens.dashboard.checks import check_dev_is_not_pointed_at_a_real_redata

#: What this checkout's own .env actually held when the problem was found.
PRODUCTION_REDATA = "https://redata.urbanlens.org"


@contextlib.contextmanager
def _deployment(environment: str, url: str | None) -> Iterator[None]:
    """Pretend to be one deployment with one REData URL."""
    with (
        override_settings(ENVIRONMENT_NAME=environment),
        mock.patch.multiple("urbanlens.UrbanLens.settings.app.settings", redata_api_url=url),
    ):
        yield


def _ids(messages: list) -> list[str]:
    return [message.id for message in messages]


class TheSettingItReadsExistsTests(TestCase):
    """`override_settings` invents names happily; this one has to be real."""

    def test_environment_name_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(django_settings, "ENVIRONMENT_NAME"))


class ItFiresWhereItShouldTests(TestCase):
    def test_development_pointed_at_production_redata(self) -> None:
        with _deployment("development", PRODUCTION_REDATA):
            self.assertEqual(_ids(check_dev_is_not_pointed_at_a_real_redata()), ["dashboard.W003"])

    def test_local_pointed_at_production_redata(self) -> None:
        with _deployment("local", PRODUCTION_REDATA):
            self.assertEqual(_ids(check_dev_is_not_pointed_at_a_real_redata()), ["dashboard.W003"])

    def test_the_message_says_whose_budget_it_spends(self) -> None:
        """REData is allowed from every environment (D26), so the hint names the budget instead of a switch."""
        with _deployment("development", PRODUCTION_REDATA):
            hint = check_dev_is_not_pointed_at_a_real_redata()[0].hint
        self.assertIn("production's", hint)
        self.assertNotIn("UL_ALLOW_OUTBOUND_APIS", hint)


class ItStaysQuietWhereItShouldTests(TestCase):
    """A warning that cried wolf on every deployment would be turned off."""

    def test_production_is_not_its_business(self) -> None:
        with _deployment("production", PRODUCTION_REDATA):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_staging_is_not_its_business(self) -> None:
        with _deployment("staging", PRODUCTION_REDATA):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_no_redata_configured(self) -> None:
        with _deployment("development", None):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_a_container_alias_is_local(self) -> None:
        """A bare hostname is a Docker service name; it cannot leave the host."""
        with _deployment("development", "http://urbanlens_redata:8000"):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_localhost_is_local(self) -> None:
        with _deployment("development", "http://localhost:8001"):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_a_private_address_is_local(self) -> None:
        with _deployment("development", "http://10.2.0.244:8001"):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_every_rfc_1918_address_is_local(self) -> None:
        """The check stopped at 172.20., so 172.21-172.31 (all inside 172.16.0.0/12) warned as if public."""
        for host in (
            "10.0.0.1",
            "10.255.255.254",
            "172.16.0.1",
            "172.20.1.1",
            "172.21.0.1",
            "172.31.255.254",
            "192.168.0.1",
            "192.168.255.254",
        ):
            with self.subTest(host=host), _deployment("development", f"http://{host}:8001"):
                self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_a_public_address_next_to_a_private_range_is_not(self) -> None:
        for host in ("172.15.255.255", "172.32.0.1", "11.0.0.1", "192.169.0.1", "9.255.255.255", "8.8.8.8"):
            with self.subTest(host=host), _deployment("development", f"http://{host}:8001"):
                self.assertEqual(_ids(check_dev_is_not_pointed_at_a_real_redata()), ["dashboard.W003"])

    def test_a_hostname_that_merely_starts_like_a_private_range_is_not(self) -> None:
        """ "10.example.org" is a name somebody registered, not an address in 10.0.0.0/8."""
        with _deployment("development", "https://10.example.org"):
            self.assertEqual(_ids(check_dev_is_not_pointed_at_a_real_redata()), ["dashboard.W003"])

    def test_a_dev_environment_is_local(self) -> None:
        """`dev_env.py --own-redata` puts one behind a *.dev. hostname."""
        with _deployment("development", "https://a1b2c3-redata.dev.urbanlens.org"):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])

    def test_an_unparseable_url_says_nothing(self) -> None:
        """Nothing useful to warn about, and a check that raises blocks manage.py."""
        with _deployment("development", "not a url"):
            self.assertEqual(check_dev_is_not_pointed_at_a_real_redata(), [])


class ItIsRegisteredTests(TestCase):
    """A check that is never run warns nobody."""

    def test_django_will_actually_run_it(self) -> None:
        from django.core.checks import registry

        registered = {check.__name__ for check in registry.registry.get_checks()}
        self.assertIn("check_dev_is_not_pointed_at_a_real_redata", registered)
