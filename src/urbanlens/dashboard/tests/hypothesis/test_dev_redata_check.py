"""`dashboard.W003`: a development box pointed at somebody's real REData.

REData is this project's own service, which is why the demo's spend guard
exempts it - calling our own instance costs only our own capacity. That stops
being true one hop later, because REData reaches Google Places, which bills. A
development checkout carrying the production REData URL and a live key is one
flag away from spending a real budget on background work nobody is watching, and
a single pin import enqueues thousands of such calls (P109).

The check is a warning rather than an error and fires whether or not
`UL_ALLOW_OUTBOUND_APIS` is on, because the whole point is that turning it on -
which is exactly what working on an integration means - makes it live.
"""

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
def _deployment(environment: str, url: str | None, *, allow: bool = False) -> Iterator[None]:
    """Pretend to be one deployment with one REData URL."""
    with (
        override_settings(ENVIRONMENT_NAME=environment),
        mock.patch.multiple("urbanlens.UrbanLens.settings.app.settings", redata_api_url=url, allow_outbound_apis=allow),
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

    def test_it_fires_even_while_outbound_calls_are_refused(self) -> None:
        """The guard is what makes this harmless today, and flags get turned on."""
        with _deployment("development", PRODUCTION_REDATA, allow=False):
            self.assertEqual(_ids(check_dev_is_not_pointed_at_a_real_redata()), ["dashboard.W003"])

    def test_the_message_says_which_state_the_flag_is_in(self) -> None:
        with _deployment("development", PRODUCTION_REDATA, allow=True):
            self.assertIn("going out right now", check_dev_is_not_pointed_at_a_real_redata()[0].hint)
        with _deployment("development", PRODUCTION_REDATA, allow=False):
            self.assertIn("nothing is calling it yet", check_dev_is_not_pointed_at_a_real_redata()[0].hint)


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
