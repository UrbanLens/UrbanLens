"""Each E2EE request the published schema documents is the request its view reads (P310).

A native client generates its E2EE calls from the schema. Rewrap and reset refuse a password-backed account without
``current_password``, and neither documented it; rewrap's KDF cost and reset's re-wrapped key lists were missing too,
and the two passkey-wrap operations documented no body. Reset also kept the bundle's old KDF cost beside a password
wrap made at another, so the next unwrap would derive the wrong key.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
from typing import TYPE_CHECKING

from django.test import Client
from django.urls import resolve, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
from urbanlens.dashboard.models.e2ee.key_bundle import DEFAULT_KDF_MEMLIMIT, DEFAULT_KDF_OPSLIMIT

if TYPE_CHECKING:
    from collections.abc import Callable

_WRITES = frozenset({"post", "put", "patch", "delete"})
#: Fills any path parameter, a UUID one included.
_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def _document() -> dict:
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


def _body_reads(function: Callable[..., object], seen: set[str] | None = None) -> set[str]:
    """The JSON body keys a view method reads, and those read by the module's helpers it hands the body to."""
    from urbanlens.dashboard.controllers import e2ee

    seen = seen if seen is not None else {function.__name__}
    source = inspect.getsource(function)
    keys = set(re.findall(r'data\.get\("([a-z_]+)"', source))
    keys |= set(re.findall(r'"([a-z_]+)" in data\b', source))
    keys |= set(re.findall(r'data\["([a-z_]+)"\]', source))
    for name in set(re.findall(r"\b(_[a-z_]+)\(", source)) - seen:
        helper = getattr(e2ee, name, None)
        if inspect.isfunction(helper) and "data" in inspect.signature(helper).parameters:
            seen.add(name)
            keys |= _body_reads(helper, seen)
    return keys


def _documented(document: dict, operation: dict) -> set[str]:
    schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return set(schema.get("properties", {}))


class TheDocumentedRequestIsTheReadRequestTests(SimpleTestCase):
    def test_every_field_an_e2ee_write_reads_is_documented(self) -> None:
        document = _document()
        checked = 0
        for path, item in document["paths"].items():
            if not path.startswith("/dashboard/e2ee/"):
                continue
            view = resolve(re.sub(r"\{[^}]+\}", _PLACEHOLDER, path)).func.view_class
            for method, operation in item.items():
                if method not in _WRITES:
                    continue
                checked += 1
                reads = _body_reads(getattr(view, method))
                if method == "delete":
                    # drf-spectacular documents no DELETE body, as OpenAPI 3.0 leaves one undefined; its description names it.
                    documented = {key for key in reads if f"`{key}`" in operation.get("description", "")}
                else:
                    documented = _documented(document, operation)
                with self.subTest(path=path, method=method):
                    self.assertEqual(reads - documented, set())
        self.assertGreaterEqual(checked, 7)

    def test_the_reads_are_found(self) -> None:
        """Anti-vacuity: the source scan sees what the views read, the password proof included."""
        from urbanlens.dashboard.controllers import e2ee

        self.assertEqual(
            _body_reads(e2ee.E2EEPasskeyWrapView.post),
            {"credential_id", "prf_input", "wrapped_secret", "current_password"},
        )
        self.assertLessEqual(
            {"kdf_opslimit", "kdf_memlimit", "current_password"}, _body_reads(e2ee.E2EERewrapView.post)
        )
        self.assertEqual(_body_reads(e2ee.E2EEPasskeyWrapItemView.delete), {"current_password"})


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class ResetStoresItsWrapsKdfCostTests(TestCase):
    """The KDF cost stored with a password wrap is the one it was made at, as rewrap already records."""

    def setUp(self) -> None:
        super().setUp()
        user = baker.make("auth.User")
        user.set_password("pw")
        user.save(update_fields=["password"])
        self.profile = user.profile
        self.bundle = MessagingKeyBundle.objects.create(
            profile=self.profile,
            public_key=_b64(os.urandom(32)),
            recovery_wrapped_secret=_b64(os.urandom(72)),
            kdf_opslimit=DEFAULT_KDF_OPSLIMIT + 2,
            kdf_memlimit=DEFAULT_KDF_MEMLIMIT * 2,
        )
        self.client = Client()
        self.client.force_login(user)

    def _reset(self, **extra: object):
        body = {
            "confirm": "RESET",
            "public_key": _b64(os.urandom(32)),
            "recovery_wrapped_secret": _b64(os.urandom(72)),
            "password_wrapped_secret": _b64(os.urandom(72)),
            "password_wrap_salt": _b64(os.urandom(16)),
            "current_password": "pw",
            **extra,
        }
        return self.client.post(reverse("e2ee.reset"), data=json.dumps(body), content_type="application/json")

    def test_a_wrap_naming_no_cost_is_stored_at_the_default(self) -> None:
        """The browser's reset wraps at the default cost and names none."""
        self.assertEqual(self._reset().status_code, 200)

        self.bundle.refresh_from_db()
        self.assertEqual(
            (self.bundle.kdf_opslimit, self.bundle.kdf_memlimit), (DEFAULT_KDF_OPSLIMIT, DEFAULT_KDF_MEMLIMIT)
        )

    def test_a_wrap_naming_its_cost_is_stored_at_it(self) -> None:
        cost = {"kdf_opslimit": DEFAULT_KDF_OPSLIMIT + 1, "kdf_memlimit": DEFAULT_KDF_MEMLIMIT * 4}

        self.assertEqual(self._reset(**cost).status_code, 200)

        self.bundle.refresh_from_db()
        self.assertEqual((self.bundle.kdf_opslimit, self.bundle.kdf_memlimit), tuple(cost.values()))

    def test_a_cost_below_the_floor_is_refused(self) -> None:
        response = self._reset(kdf_opslimit=1, kdf_memlimit=DEFAULT_KDF_MEMLIMIT)

        self.assertEqual(response.status_code, 400)
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.version, 1)
