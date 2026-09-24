"""The ``ApiKey`` secret hash: its format, its upgrade path, its cost, and the writes around it.

P146. A key's secret is 32 bytes of ``secrets.token_urlsafe``, so a password KDF's work factor prices
nothing an attacker could guess, while PBKDF2 at Django's default iterations cost ~0.9s of CPU on
every authenticated request - and on every unauthenticated one naming a real (public) prefix.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from unittest import mock

from django.contrib.auth import hashers
from django.contrib.auth.hashers import check_password, get_hashers_by_algorithm
from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyUsageLog
from urbanlens.dashboard.services.auth import api_keys
from urbanlens.dashboard.services.auth.api_keys import (
    _HASH_SCHEME,
    _PREFIX_LENGTH,
    _SECRET_ENTROPY_BYTES,
    KEY_LABEL,
    LAST_USED_RESOLUTION,
    MIN_FAST_HASH_SECRET_LENGTH,
    _hash_secret,
    _upgrade_key_hash,
    authenticate_api_key,
    generate_api_key,
    verify_api_key_secret,
)

#: Enough verifications that a returning KDF cannot hide in timer noise; PBKDF2 would need minutes.
_CANARY_ITERATIONS = 200
_CANARY_CPU_BUDGET_SECONDS = 0.5

#: The production hasher list is Django's default, which puts PBKDF2 first; settings/test.py swaps in
#: MD5 for speed, so a legacy row has to be minted under the real one to be a legacy row at all.
_PRODUCTION_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.MD5PasswordHasher",
]


def _secret_of(raw_key: str) -> str:
    return raw_key[len(f"{KEY_LABEL}_") + _PREFIX_LENGTH :]


def _legacy_hash(secret: str) -> str:
    """What the pre-P146 issuer stored: ``make_password`` under PBKDF2, at test-speed iterations."""
    return hashers.PBKDF2PasswordHasher().encode(secret, hashers.PBKDF2PasswordHasher().salt(), iterations=1000)


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _PBKDF2Spy:
    """Counts calls to the PBKDF2 primitive every Django PBKDF2 hasher goes through."""

    def __init__(self) -> None:
        self.calls = 0
        self._original = hashers.pbkdf2

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._original(*args, **kwargs)

    def patch(self):
        return mock.patch.object(hashers, "pbkdf2", self)


class ApiKeyHashFormatTests(SimpleTestCase):
    """The stored format, and the one ``startswith`` that separates it from Django's."""

    def setUp(self) -> None:
        self.secret = secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)

    def test_the_hash_is_a_tagged_sha256_of_the_secret(self) -> None:
        assert _hash_secret(self.secret) == _HASH_SCHEME + hashlib.sha256(self.secret.encode()).hexdigest()
        assert _HASH_SCHEME == "ulk1$"

    def test_the_encoded_hash_fits_the_column(self) -> None:
        assert len(_hash_secret(self.secret)) <= ApiKey._meta.get_field("key_hash").max_length

    def test_no_installed_password_hasher_can_claim_the_scheme_name(self) -> None:
        for hasher_list in (None, _PRODUCTION_HASHERS):
            with override_settings(**({"PASSWORD_HASHERS": hasher_list} if hasher_list else {})):
                installed = get_hashers_by_algorithm()
                assert _HASH_SCHEME.rstrip("$") not in installed
                assert not any("$" in algorithm for algorithm in installed)

    def test_django_cannot_verify_a_current_format_hash(self) -> None:
        """Format confusion in the direction that would be a bypass: False, not True and not a raise."""
        assert check_password(self.secret, _hash_secret(self.secret)) is False


class ApiKeySecretEntropyInterlockTests(SimpleTestCase):
    """A bare digest is only safe for a high-entropy secret, so short ones are refused at both doors."""

    def test_a_short_secret_is_refused_rather_than_fast_hashed(self) -> None:
        with self.assertRaises(ValueError):
            _hash_secret("hunter2")

    def test_a_short_secret_never_verifies_even_against_its_own_digest(self) -> None:
        """A row written by any path that skipped ``_hash_secret`` still cannot admit a guessable secret."""
        forged = _HASH_SCHEME + hashlib.sha256(b"hunter2").hexdigest()
        assert verify_api_key_secret("hunter2", forged) == (False, False)

    def test_the_floor_is_below_what_the_issuer_mints(self) -> None:
        assert len(secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)) >= MIN_FAST_HASH_SECRET_LENGTH
        assert _SECRET_ENTROPY_BYTES >= 32


class VerificationPropertyTests(SimpleTestCase):
    @given(
        secret=st.text(min_size=MIN_FAST_HASH_SECRET_LENGTH, max_size=80),
        other=st.text(min_size=0, max_size=80),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_a_digest_admits_its_own_secret_and_nothing_else(self, secret: str, other: str) -> None:
        encoded = _hash_secret(secret)
        assert verify_api_key_secret(secret, encoded) == (True, False)
        assert verify_api_key_secret(other, encoded) == (other == secret, False)


class TimingSafeComparisonTests(SimpleTestCase):
    def test_the_digest_is_compared_with_compare_digest(self) -> None:
        secret = secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)
        with mock.patch.object(api_keys.secrets, "compare_digest", wraps=secrets.compare_digest) as spy:
            assert verify_api_key_secret(secret, _hash_secret(secret)) == (True, False)
            assert verify_api_key_secret(secret + "x", _hash_secret(secret)) == (False, False)
        assert spy.call_count == 2


class NewKeysAreStoredInTheCurrentFormatTests(TestCase):
    def test_a_generated_key_is_stored_as_ulk1(self) -> None:
        api_key, raw_key = generate_api_key(baker.make(User), "Zapier")
        stored = ApiKey.objects.get(pk=api_key.pk).key_hash
        assert stored == _hash_secret(_secret_of(raw_key))


class VerificationCostTests(TestCase):
    """The regression guard: nothing on the current-format path may stretch."""

    def setUp(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Zapier")

    @override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
    def test_a_current_format_key_never_reaches_pbkdf2(self) -> None:
        spy = _PBKDF2Spy()
        with spy.patch():
            assert authenticate_api_key(self.raw_key) is not None
            response = self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        assert response.status_code == 200
        assert spy.calls == 0

    @override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
    def test_the_spy_does_see_a_legacy_key(self) -> None:
        """The control that stops the assertion above passing against a spy that sees nothing."""
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash=_legacy_hash(_secret_of(self.raw_key)))
        spy = _PBKDF2Spy()
        with spy.patch():
            assert authenticate_api_key(self.raw_key) is not None
        assert spy.calls == 1

    @override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
    def test_a_wrong_secret_on_a_valid_prefix_never_reaches_pbkdf2(self) -> None:
        """The DoS amplifier: the prefix is public, so this path's rate is the attacker's to choose."""
        spy = _PBKDF2Spy()
        with spy.patch():
            response = self.client.get(
                reverse("external_api:whoami"), **_bearer(f"{KEY_LABEL}_{self.api_key.prefix}{'x' * 43}")
            )
        assert response.status_code == 401
        assert spy.calls == 0

    def test_verification_does_not_stretch(self) -> None:
        """CPU time, not wall clock, so a loaded host cannot flake it."""
        secret = _secret_of(self.raw_key)
        encoded = ApiKey.objects.get(pk=self.api_key.pk).key_hash
        started = time.process_time()
        for _ in range(_CANARY_ITERATIONS):
            verify_api_key_secret(secret, encoded)
        elapsed = time.process_time() - started
        assert elapsed < _CANARY_CPU_BUDGET_SECONDS, f"{_CANARY_ITERATIONS} verifications cost {elapsed:.2f}s of CPU"

    def test_an_unknown_prefix_costs_no_kdf(self) -> None:
        spy = _PBKDF2Spy()
        with spy.patch(), override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS):
            for _ in range(20):
                assert authenticate_api_key(f"{KEY_LABEL}_" + "z" * 53) is None
        assert spy.calls == 0


@override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
class LegacyApiKeyUpgradeTests(TestCase):
    """Keys issued before P146 keep working, and rewrite themselves on first use."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Legacy")
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash=_legacy_hash(_secret_of(self.raw_key)))

    def _stored(self) -> str:
        return ApiKey.objects.get(pk=self.api_key.pk).key_hash

    def test_a_pbkdf2_key_still_authenticates(self) -> None:
        assert self._stored().startswith("pbkdf2_sha256$")
        resolved = authenticate_api_key(self.raw_key)
        assert resolved is not None
        assert resolved.pk == self.api_key.pk

    def test_it_is_rewritten_in_the_current_format_on_first_use(self) -> None:
        authenticate_api_key(self.raw_key)
        assert self._stored() == _hash_secret(_secret_of(self.raw_key))

    def test_the_same_raw_key_still_works_after_the_rewrite(self) -> None:
        authenticate_api_key(self.raw_key)
        spy = _PBKDF2Spy()
        with spy.patch():
            assert authenticate_api_key(self.raw_key) is not None
        assert spy.calls == 0

    def test_a_wrong_secret_never_rewrites_the_stored_hash(self) -> None:
        tampered = self.raw_key[:-1] + ("a" if self.raw_key[-1] != "a" else "b")
        assert authenticate_api_key(tampered) is None
        assert self._stored().startswith("pbkdf2_sha256$")

    def test_a_revoked_legacy_key_is_rejected_and_not_rewritten(self) -> None:
        ApiKey.objects.filter(pk=self.api_key.pk).update(revoked_at=timezone.now())
        assert authenticate_api_key(self.raw_key) is None
        assert self._stored().startswith("pbkdf2_sha256$")

    def test_the_rewrite_is_a_compare_and_swap(self) -> None:
        """A racing request that already rewrote the row must not be clobbered by this one's stale read."""
        stale = ApiKey.objects.get(pk=self.api_key.pk)
        winner = _hash_secret(secrets.token_urlsafe(_SECRET_ENTROPY_BYTES))
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash=winner)
        assert _upgrade_key_hash(stale, _secret_of(self.raw_key)) is False
        assert self._stored() == winner

    def test_a_legacy_key_is_upgraded_through_the_http_path(self) -> None:
        response = self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        assert response.status_code == 200
        assert self._stored().startswith(_HASH_SCHEME)


@override_settings(PASSWORD_HASHERS=_PRODUCTION_HASHERS)
class CorruptApiKeyHashTests(TestCase):
    """Every malformed stored hash must deny. A 500 here is a worse bug than a 401."""

    CORRUPT = (
        f"{_HASH_SCHEME}not-hex-at-all",
        f"{_HASH_SCHEME}{'a' * 32}",
        _HASH_SCHEME + "é" * 64,
        f"{_HASH_SCHEME}{'a' * 122}",  # over-long, and still inside the column
        "",
        "!",
        "garbage",
        # An identifiable hasher with a malformed body: here check_password raises instead of returning False.
        "pbkdf2_sha256$",
        "pbkdf2_sha256$1200000",
        "pbkdf2_sha256$1200000$salt",
        "pbkdf2_sha256$abc$salt$hash",
        "argon2$nope",
    )

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Test Key")

    def test_malformed_rows_deny_rather_than_raising(self) -> None:
        for encoded in self.CORRUPT:
            with self.subTest(encoded=encoded[:40]):
                ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash=encoded)
                assert authenticate_api_key(self.raw_key) is None

    def test_a_truncated_legacy_row_is_a_401_not_a_500(self) -> None:
        ApiKey.objects.filter(pk=self.api_key.pk).update(key_hash="pbkdf2_sha256$1200000$salt")
        with self.assertRaises(ValueError):
            check_password(_secret_of(self.raw_key), "pbkdf2_sha256$1200000$salt")  # the premise: Django raises
        response = self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        assert response.status_code == 401

    def test_presenting_the_stored_hash_itself_as_the_secret_fails(self) -> None:
        stored = ApiKey.objects.get(pk=self.api_key.pk).key_hash
        assert authenticate_api_key(f"{KEY_LABEL}_{self.api_key.prefix}{stored}") is None


class RevocationAndRejectionTests(TestCase):
    """Nothing about the decision is cached, so a revocation lands on the very next request."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Test Key")
        self.url = reverse("external_api:whoami")

    def test_revocation_takes_effect_on_the_very_next_request(self) -> None:
        assert self.client.get(self.url, **_bearer(self.raw_key)).status_code == 200
        assert self.client.get(self.url, **_bearer(self.raw_key)).status_code == 200  # inside the touch window
        ApiKey.objects.filter(pk=self.api_key.pk).update(revoked_at=timezone.now())
        assert self.client.get(self.url, **_bearer(self.raw_key)).status_code == 401

    def test_a_wrong_secret_with_a_valid_prefix_is_refused(self) -> None:
        assert self.client.get(self.url, **_bearer(self.raw_key)).status_code == 200
        wrong = f"{KEY_LABEL}_{self.api_key.prefix}{secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)}"
        assert self.client.get(self.url, **_bearer(wrong)).status_code == 401

    def test_a_one_character_change_is_refused(self) -> None:
        tampered = self.raw_key[:-1] + ("a" if self.raw_key[-1] != "a" else "b")
        assert authenticate_api_key(tampered) is None


class LastUsedCoarseningTests(TestCase):
    """``last_used_at`` is written once per key per window, not once per request."""

    def setUp(self) -> None:
        self.api_key, self.raw_key = generate_api_key(baker.make(User), "Test Key")

    def test_the_first_authentication_records_last_used(self) -> None:
        resolved = authenticate_api_key(self.raw_key)
        assert resolved is not None
        assert resolved.last_used_at is not None
        assert ApiKey.objects.get(pk=self.api_key.pk).last_used_at is not None

    def test_a_second_authentication_inside_the_window_writes_nothing(self) -> None:
        """One statement - the indexed prefix lookup - and no UPDATE on the shared row."""
        authenticate_api_key(self.raw_key)
        with self.assertNumQueries(1):
            authenticate_api_key(self.raw_key)

    def test_a_stale_last_used_is_refreshed(self) -> None:
        stale = timezone.now() - (LAST_USED_RESOLUTION * 2)
        ApiKey.objects.filter(pk=self.api_key.pk).update(last_used_at=stale)
        authenticate_api_key(self.raw_key)
        assert ApiKey.objects.get(pk=self.api_key.pk).last_used_at > stale

    def test_the_window_is_retested_in_sql(self) -> None:
        """A concurrent worker that read the same stale row must match nothing once another has written."""
        stale = timezone.now() - (LAST_USED_RESOLUTION * 2)
        ApiKey.objects.filter(pk=self.api_key.pk).update(last_used_at=stale)
        first_reader = ApiKey.objects.get(pk=self.api_key.pk)
        second_reader = ApiKey.objects.get(pk=self.api_key.pk)
        assert api_keys.touch_api_key(first_reader) is True
        assert api_keys.touch_api_key(second_reader) is False

    def test_the_returned_instance_always_reports_now(self) -> None:
        first = authenticate_api_key(self.raw_key)
        second = authenticate_api_key(self.raw_key)
        assert first is not None
        assert second is not None
        assert second.last_used_at >= first.last_used_at

    def test_only_the_writing_request_is_flagged_for_usage_logging(self) -> None:
        first = authenticate_api_key(self.raw_key)
        second = authenticate_api_key(self.raw_key)
        assert first is not None
        assert second is not None
        assert (first.usage_sample, second.usage_sample) == (True, False)


class UsageLogSamplingTests(TestCase):
    """Reads ride the ``last_used_at`` window; writes are always logged."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Zapier")

    def _entries(self) -> int:
        return ApiKeyUsageLog.objects.for_api_key(self.api_key).count()

    def test_repeated_reads_inside_the_window_log_once(self) -> None:
        for _ in range(5):
            assert self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key)).status_code == 200
        assert self._entries() == 1

    def test_a_write_inside_the_window_is_still_logged(self) -> None:
        self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        self.client.post(
            reverse("external_api:pins"),
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        assert self._entries() == 2
        assert ApiKeyUsageLog.objects.for_api_key(self.api_key).order_by("-created").first().endpoint == reverse(
            "external_api:pins"
        )

    def test_a_read_after_the_window_is_logged_again(self) -> None:
        self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        ApiKey.objects.filter(pk=self.api_key.pk).update(last_used_at=timezone.now() - LAST_USED_RESOLUTION * 2)
        self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        assert self._entries() == 2
