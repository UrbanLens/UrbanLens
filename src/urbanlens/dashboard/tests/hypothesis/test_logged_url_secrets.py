"""No credential sent in a URL reaches the log, whatever logs the URL (P203)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import io
import logging
import os
import string
from urllib.parse import quote, urlencode

from django.conf import settings
import requests

from hypothesis import assume, given, strategies as st
from urbanlens.core.tests.log_output import handler_output
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.security.redact import SENSITIVE_PARAM_NAMES, is_sensitive_param_name, redact_urls
from urbanlens.UrbanLens.logging_filters import SecretRedactionFilter

GOOGLE_KEY = "AIzaSyP203-fake-key-0123456789abcdefg"
LATITUDE = 41.718253
LONGITUDE = -73.934017
MAPS_LOGGER = "urbanlens.dashboard.services.apis.locations.google.maps"


class _RefusingAdapter(requests.adapters.BaseAdapter):
    """Answers every request the way Google answers a key its restrictions do not admit."""

    def send(self, request: requests.PreparedRequest, *args: object, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 403
        response.reason = "Forbidden"
        response.url = request.url or ""
        response.request = request
        response._content = b'{"error_message": "The provided API key is invalid."}'
        return response

    def close(self) -> None:
        pass


class _RefusingSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.mount("https://", _RefusingAdapter())


class StaticMapsRefusalTests(SimpleTestCase):
    """The production finding: a Static Maps 403 logged its URL, ``key=`` and all."""

    def setUp(self) -> None:
        super().setUp()
        self.gateway = GoogleMapsGateway(api_key=GOOGLE_KEY, session=_RefusingSession())

    def test_a_refused_key_is_not_in_the_log(self) -> None:
        with handler_output("urbanlens") as output:
            image = self.gateway.get_satellite_image_bytes(LATITUDE, LONGITUDE)

        logged = output.getvalue()
        self.assertIsNone(image)
        self.assertIn(MAPS_LOGGER, logged, "the warning did not reach the configured handlers")
        self.assertIn("staticmap", logged)
        self.assertNotIn(GOOGLE_KEY, logged)

    def test_the_url_still_names_where_the_coordinates_were_withheld(self) -> None:
        """The gateway redacts its own coordinate arguments; the URL in the exception carried them anyway."""
        with handler_output("urbanlens") as output:
            self.gateway.get_satellite_image_bytes(LATITUDE, LONGITUDE)

        logged = output.getvalue()
        self.assertIn(MAPS_LOGGER, logged)
        self.assertNotIn(str(LATITUDE), logged)
        self.assertNotIn(str(LONGITUDE), logged)

    def test_a_logged_traceback_does_not_carry_the_key(self) -> None:
        with handler_output("urbanlens") as output:
            try:
                self.gateway.get_directions("Albany, NY", "Hudson, NY")
            except requests.HTTPError:
                logging.getLogger(MAPS_LOGGER).warning("Directions failed", exc_info=True)

        logged = output.getvalue()
        self.assertIn("HTTPError", logged, "the traceback was not written")
        self.assertNotIn(GOOGLE_KEY, logged)

    def test_a_library_logger_writing_through_the_root_handlers_does_not_carry_the_key(self) -> None:
        """``requests``/``urllib3`` and Celery's own loggers have no handler of their own; they reach the root's."""
        with handler_output("") as output:
            try:
                self.gateway.get_directions("Albany, NY", "Hudson, NY")
            except requests.HTTPError as exc:
                logging.getLogger("urllib3.connectionpool").warning("Retrying after %r", exc)

        logged = output.getvalue()
        self.assertIn("urllib3.connectionpool", logged)
        self.assertNotIn(GOOGLE_KEY, logged)


class NothingElseIsRedactedTests(SimpleTestCase):
    def test_a_url_without_secrets_is_logged_as_it_was(self) -> None:
        url = "https://tiles.example.org/v1/tiles.json?format=json&page=2&zoom=14&size=640x400"

        with handler_output("urbanlens") as output:
            logging.getLogger(MAPS_LOGGER).warning("Fetched %s", url)

        self.assertIn(f"Fetched {url}", output.getvalue())

    def test_prose_that_merely_names_a_key_is_left_alone(self) -> None:
        """Only a query string is read as one; ``location=%s`` in a message is a primary key."""
        with handler_output("urbanlens") as output:
            logging.getLogger(MAPS_LOGGER).warning("Street View unavailable for location=%s key=%s", 17, "cache-entry")

        self.assertIn("location=17 key=cache-entry", output.getvalue())


class RedactionIsWiredTests(SimpleTestCase):
    """Every handler writes through the filter, so a logger added later needs no wiring of its own."""

    def test_every_handler_in_logging_carries_the_filter(self) -> None:
        declared = {
            name
            for name, spec in settings.LOGGING["filters"].items()
            if spec.get("()", "").endswith(".SecretRedactionFilter")
        }

        self.assertTrue(declared, "LOGGING declares no SecretRedactionFilter")
        for name, handler in settings.LOGGING["handlers"].items():
            with self.subTest(handler=name):
                self.assertTrue(declared & set(handler.get("filters", ())))

    def test_every_handler_a_configured_logger_holds_carries_the_filter(self) -> None:
        names = ["", *settings.LOGGING["loggers"]]
        loggers = [logging.getLogger(name) for name in names]
        handlers = {
            handler
            for logger in loggers
            for handler in logger.handlers
            if not type(handler).__module__.startswith("_pytest")
        }

        self.assertTrue(handlers)
        for handler in handlers:
            with self.subTest(handler=handler):
                self.assertTrue(any(isinstance(existing, SecretRedactionFilter) for existing in handler.filters))


@contextmanager
def celery_worker_logging(stream: io.StringIO) -> Iterator[None]:
    """Celery's own worker logging setup, which replaces the root logger's handlers, undone afterwards.

    Yields:
        Nothing; the worker's handlers write to ``stream`` meanwhile.
    """
    import billiard.util
    from celery.app.log import Logging
    import kombu.utils.encoding

    from urbanlens.UrbanLens.celery import app

    def every_logger() -> list[logging.Logger]:
        return [
            logging.getLogger(),
            *(logger for logger in logging.Logger.manager.loggerDict.values() if isinstance(logger, logging.Logger)),
        ]

    saved = {logger: (list(logger.handlers), logger.level, logger.propagate) for logger in every_logger()}
    saved_filters = {handler: list(handler.filters) for logger in saved for handler in logger.handlers}
    saved_environ = {
        name: os.environ.get(name) for name in ("_MP_FORK_LOGLEVEL_", "_MP_FORK_LOGFILE_", "_MP_FORK_LOGFORMAT_")
    }
    saved_setup = Logging._setup
    saved_encoding_file = kombu.utils.encoding.get_default_encoding_file()
    saved_multiprocessing_logger = getattr(billiard.util, "_logger", None)
    Logging._setup = False
    try:
        app.log.setup_logging_subsystem(loglevel=logging.WARNING, logfile=stream, colorize=False)
        yield
    finally:
        for logger in every_logger():
            handlers, level, propagate = saved.get(logger, ([], logging.NOTSET, True))
            logger.handlers, logger.propagate = handlers, propagate
            logger.setLevel(level)
        for handler, filters in saved_filters.items():
            handler.filters = filters
        for name, value in saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        Logging._setup = saved_setup
        kombu.utils.encoding.set_default_encoding_file(saved_encoding_file)
        billiard.util._logger = saved_multiprocessing_logger


class CeleryWorkerLoggingTests(SimpleTestCase):
    """A worker replaces the root logger's handlers with its own; a task that raises is logged through them."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.UrbanLens.celery import app

        name = "urbanlens.dashboard.tests.p203_refused_directions"

        @app.task(name=name)
        def refused_directions() -> None:
            GoogleMapsGateway(api_key=GOOGLE_KEY, session=_RefusingSession()).get_directions("Albany, NY", "Hudson, NY")

        self.task = refused_directions
        self.addCleanup(app.tasks.pop, name, None)

    def test_a_task_failing_on_the_refused_key_does_not_log_it(self) -> None:
        worker_output = io.StringIO()
        with celery_worker_logging(worker_output), handler_output("urbanlens") as app_output:
            result = self.task.apply(throw=False)

        self.assertTrue(result.failed())
        logged = worker_output.getvalue()
        self.assertIn("raised unexpected", logged, "Celery's failure line did not reach the worker's handler")
        self.assertIn("HTTPError", logged)
        self.assertNotIn(GOOGLE_KEY, logged)
        self.assertIn("Celery task failed", app_output.getvalue())
        self.assertNotIn(GOOGLE_KEY, app_output.getvalue())

    def test_the_worker_handlers_are_redacted_whoever_logs_through_them(self) -> None:
        worker_output = io.StringIO()
        with celery_worker_logging(worker_output):
            logging.getLogger("celery.redirected").warning(
                "https://maps.googleapis.com/maps/api/staticmap?key=%s", GOOGLE_KEY
            )

        self.assertIn("staticmap", worker_output.getvalue())
        self.assertNotIn(GOOGLE_KEY, worker_output.getvalue())


def _mixed_case(name: str, mask: int) -> str:
    return "".join(char.upper() if mask >> index & 1 else char.lower() for index, char in enumerate(name))


_CREDENTIAL_NAMES = st.builds(
    _mixed_case,
    st.sampled_from(
        sorted(
            {
                *SENSITIVE_PARAM_NAMES,
                "api_key",
                "access_token",
                "client_secret",
                "subscription-key",
                "X-Amz-Signature",
                "apiKey",
                "refresh_token",
                "oauth_token",
            }
        ),
    ),
    st.integers(min_value=0, max_value=2**32 - 1),
)
_SECRETS = st.text(alphabet=string.ascii_letters + string.digits + "-_.~/+=:@!$", min_size=12, max_size=48)
_OTHER_PARAMS = st.lists(
    st.tuples(
        st.sampled_from(["format", "zoom", "size", "page", "maptype", "language"]),
        st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=8),
    ),
    max_size=4,
)


def _raise(exc: Exception) -> None:
    raise exc


class AnyCredentialParameterPropertyTests(SimpleTestCase):
    @given(
        name=_CREDENTIAL_NAMES,
        secret=_SECRETS,
        before=_OTHER_PARAMS,
        after=_OTHER_PARAMS,
        shape=st.sampled_from(["argument", "exception", "traceback", "nested"]),
    )
    def test_the_value_never_reaches_the_log(
        self, name: str, secret: str, before: list[tuple[str, str]], after: list[tuple[str, str]], shape: str
    ) -> None:
        url = f"https://api.example.test/v1/lookup?{urlencode([*before, (name, secret), *after])}"
        if shape == "nested":
            url = f"https://www.example.test/media-copy/?src={quote(url, safe='')}"
        forms = {secret, quote(secret, safe=""), quote(quote(secret, safe=""), safe="")}
        assume(all(url.count(form) == 1 for form in forms if form in url))

        logger = logging.getLogger(MAPS_LOGGER)
        with handler_output("urbanlens") as output:
            if shape == "traceback":
                try:
                    _raise(requests.HTTPError(f"403 Client Error: Forbidden for url: {url}"))
                except requests.HTTPError:
                    logger.warning("Lookup failed", exc_info=True)
            elif shape == "exception":
                logger.warning("Lookup failed: %r", requests.HTTPError(f"403 Client Error: Forbidden for url: {url}"))
            else:
                logger.warning("Lookup failed for %s", url)

        logged = output.getvalue()
        self.assertIn("Lookup failed", logged)
        for form in forms:
            self.assertNotIn(form, logged)


class RedactUrlsTests(SimpleTestCase):
    def test_a_credential_name_is_recognised_in_any_spelling(self) -> None:
        for name in (
            "key",
            "KEY",
            "api_key",
            "apiKey",
            "X-Api-Key",
            "subscription-key",
            "access_token",
            "client_secret",
            "X-Amz-Signature",
            "appid",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_param_name(name))

    def test_an_ordinary_name_is_not_a_credential(self) -> None:
        for name in ("format", "zoom", "keyword", "token_type", "api-version", "postal_code"):
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_param_name(name))

    def test_a_password_in_a_url_is_withheld(self) -> None:
        redacted = redact_urls("cannot reach redis://:hunter2@dragonfly:6379/0 or amqp://worker:s3cret@rabbitmq//")

        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("s3cret", redacted)
        self.assertIn("dragonfly:6379/0", redacted)
        self.assertIn("amqp://worker:", redacted)

    def test_a_coordinate_pair_is_withheld_whatever_carries_it(self) -> None:
        redacted = redact_urls("https://nominatim.example.test/search?q=41.718253,-73.934017&format=json")

        self.assertNotIn("41.718253", redacted)
        self.assertIn("format=json", redacted)

    def test_redacting_twice_changes_nothing_more(self) -> None:
        once = redact_urls("https://a.test/?key=abc123&center=41.7%2C-73.9 and redis://:pw@cache/0")

        self.assertEqual(redact_urls(once), once)

    def test_the_same_secret_reads_the_same_in_every_line(self) -> None:
        """So an operator can still tell one key's failures from another's."""
        first = redact_urls(f"https://a.test/?key={GOOGLE_KEY}")
        second = redact_urls(f"https://b.test/?other=1&key={GOOGLE_KEY}")

        self.assertEqual(first.rsplit("key=", 1)[1], second.rsplit("key=", 1)[1])
