"""Every spelling of a username or of one of an account's addresses names that account, everywhere one is typed.

Registration refuses any spelling of a name or address another account holds; login, invitations and user search
accept any spelling of the primary address, of each verified secondary, and of the username.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account import AccountKdf, EmailVerification
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.invitation import TripInvitation
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.visits.participant import ExternalVisitParticipant
from urbanlens.dashboard.services.auth.email_normalization import is_email_taken, normalize_email
from urbanlens.dashboard.services.auth.identity import canonical_identifier, find_user_by_identifier
from urbanlens.dashboard.services.auth.username import (
    find_user_by_username,
    normalize_username_key,
    username_is_taken,
    username_search_q,
)
from urbanlens.dashboard.services.security.e2ee import login_params_for_identifier
from urbanlens.dashboard.services.security.email_safety import hash_email
from urbanlens.dashboard.services.social.friendship import SelfInviteError, invite_by_email
from urbanlens.dashboard.services.trips.trip_invitations import bind_invitations_to_account, invite_to_trip_by_email
from urbanlens.dashboard.services.trips.trip_membership import add_member_by_username
from urbanlens.dashboard.services.visits.safety import invite_checkin_partner
from urbanlens.dashboard.services.visits.visit_invites import process_pending_visit_invites
from urbanlens.dashboard.tasks import (
    deliver_friend_invitation,
    deliver_trip_invitation,
    process_signup,
    send_password_reset,
)

_HIBP_PATCH = "urbanlens.dashboard.services.apis.security.hibp.HaveIBeenPwnedGateway.is_password_pwned"
PASSWORD = "Pty-3Chiwok-7Qvxzd-#9!"

USERNAME = "foobar"
USERNAME_SPELLINGS = ("foobar", "FooBar", "foo.bar", "foo_bar", "_f-o-o-b-a-r-", "foo-bar", "F.O.O_B-A-R", "f00bar")
# Registration only accepts letters, digits and underscores, so these are the spellings a second account could try.
REGISTRABLE_USERNAME_SPELLINGS = ("foobar", "FooBar", "foo_bar", "_foobar_", "f00bar", "FOO_BAR")

# The dotted, single-letter-initial shape of the request's example, on a stand-in name.
DOTTED = "sam.a.rivera@gmail.com"
DOTTED_SPELLINGS = (
    "sam.a.rivera@gmail.com",
    "samarivera@gmail.com",
    "sam.a.rivera+anything@gmail.com",
    "s.a.m.a.r.i.v.e.r.a+ul@gmail.com",
    "Sam.A.Rivera@Gmail.com",
    "samarivera@googlemail.com",
)
# Registered with the plus tag up front: every spelling of the untagged mailbox, and every other tag, is it.
PLUS = "user+urbanlens@gmail.com"
PLUS_SPELLINGS = (
    "user+urbanlens@gmail.com",
    "user@gmail.com",
    "u.s.e.r@gmail.com",
    "user+other@gmail.com",
    "u.ser+urbanlens.too@gmail.com",
    "USER+X@googlemail.com",
)
PLAIN = "plain@gmail.com"
PLAIN_SPELLINGS = (
    "plain@gmail.com",
    "p.lain@gmail.com",
    "plain+urbanlens@gmail.com",
    "p.l.a.i.n+ul@gmail.com",
    "PLAIN+anything@googlemail.com",
)
SECONDARY = "second.address@gmail.com"
SECONDARY_SPELLINGS = (
    "second.address@gmail.com",
    "secondaddress@gmail.com",
    "s.e.c.o.n.d.address+x@gmail.com",
    "SecondAddress@googlemail.com",
)


def _url(path: str) -> str:
    return f"https://urbanlens.test{path}"


def _verified_user(username: str, email: str, *, password: str | None = None) -> User:
    """An active account whose primary address is verified and which accepts friend requests from anyone."""
    user = baker.make(User, username=username, email=email, is_active=True)
    if password is not None:
        user.set_password(password)
        user.save()
    profile = user.profile
    profile.friend_request_visibility = VisibilityChoice.ANYONE
    profile.verified_primary_email = profile.primary_email_normalized
    profile.save(update_fields=["friend_request_visibility", "verified_primary_email"])
    return user


# -- Normalization properties -------------------------------------------------------------------------------------

_MAILBOX = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=6, max_size=24)
_TAG = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.-_+", max_size=12)


@st.composite
def _gmail_spelling(draw: st.DrawFn, mailbox: str) -> str:
    """Any address Gmail delivers to ``mailbox``: dots anywhere in it, any case, any ``+tag``, either domain."""
    chars = []
    for ch in mailbox:
        chars.append(ch.upper() if draw(st.booleans()) else ch)
        chars.append("." * draw(st.integers(min_value=0, max_value=2)))
    tag = draw(st.one_of(st.just(""), _TAG.map(lambda t: f"+{t}")))
    domain = draw(st.sampled_from(["gmail.com", "GMail.com", "googlemail.com", "GOOGLEMAIL.COM"]))
    return f"{''.join(chars)}{tag}@{domain}"


@st.composite
def _username_spelling(draw: st.DrawFn, name: str) -> str:
    """``name`` with any case per character and any run of separators before, between and after its characters."""
    separators = st.text(alphabet="._- ", max_size=3)
    parts = [draw(separators)]
    for ch in name:
        parts.append(ch.upper() if draw(st.booleans()) else ch)
        parts.append(draw(separators))
    return "".join(parts)


class NormalizationPropertyTests(SimpleTestCase):
    """Every spelling folds to the same canonical form, whichever spelling was registered."""

    @settings(max_examples=200, deadline=None)
    @given(data=st.data(), mailbox=_MAILBOX)
    def test_every_gmail_spelling_of_a_mailbox_normalizes_alike(self, data: st.DataObject, mailbox: str) -> None:
        spelling = data.draw(_gmail_spelling(mailbox))
        self.assertEqual(normalize_email(spelling), f"{mailbox}@gmail.com")

    @settings(max_examples=200, deadline=None)
    @given(data=st.data(), mailbox=_MAILBOX, tag=_TAG)
    def test_a_tagged_registration_and_a_plain_one_share_every_spelling(
        self, data: st.DataObject, mailbox: str, tag: str
    ) -> None:
        registered_tagged = normalize_email(f"{mailbox}+{tag}@gmail.com")
        registered_plain = normalize_email(f"{mailbox}@gmail.com")
        spelling = data.draw(_gmail_spelling(mailbox))
        self.assertEqual(registered_tagged, registered_plain)
        self.assertEqual(normalize_email(spelling), registered_tagged)

    @settings(max_examples=100, deadline=None)
    @given(
        local=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789.+-", min_size=1, max_size=20),
        domain=st.sampled_from(["example.org", "mailbox.org", "outlook.com"]),
    )
    def test_other_domains_keep_dots_and_tags(self, local: str, domain: str) -> None:
        self.assertEqual(normalize_email(f"{local.upper()}@{domain.upper()}"), f"{local}@{domain}")

    @settings(max_examples=200, deadline=None)
    @given(data=st.data(), name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=3, max_size=30))
    def test_every_separator_and_case_spelling_of_a_username_shares_its_key(
        self, data: st.DataObject, name: str
    ) -> None:
        spelling = data.draw(_username_spelling(name))
        self.assertEqual(normalize_username_key(spelling), normalize_username_key(name))

    @settings(max_examples=200, deadline=None)
    @given(value=st.text(max_size=40))
    def test_username_key_is_idempotent_for_any_text(self, value: str) -> None:
        key = normalize_username_key(value)
        self.assertEqual(normalize_username_key(key), key)

    def test_request_examples(self) -> None:
        for spelling in USERNAME_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_username_key(spelling), normalize_username_key(USERNAME))
        for registered, spellings in ((DOTTED, DOTTED_SPELLINGS), (PLUS, PLUS_SPELLINGS), (PLAIN, PLAIN_SPELLINGS)):
            for spelling in spellings:
                with self.subTest(registered=registered, spelling=spelling):
                    self.assertEqual(normalize_email(spelling), normalize_email(registered))

    def test_a_gmail_address_with_nothing_before_the_tag_is_left_alone(self) -> None:
        self.assertEqual(normalize_email("+tag@gmail.com"), "+tag@gmail.com")
        self.assertNotEqual(normalize_email("+a@gmail.com"), normalize_email("+b@gmail.com"))

    def test_canonical_identifier_folds_each_kind_by_its_own_rules(self) -> None:
        self.assertEqual(canonical_identifier(" _F-o-o.Bar "), canonical_identifier("foobar"))
        self.assertEqual(
            canonical_identifier("S.A.M.A.Rivera+x@gmail.com"), canonical_identifier("samarivera@gmail.com")
        )
        self.assertNotEqual(canonical_identifier("foobar"), canonical_identifier("foobar@gmail.com"))


# -- Registration and login -----------------------------------------------------------------------------------------


class _SignupTestCase(TestCase):
    """Drives the real signup, verification and login views."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        hibp = patch(_HIBP_PATCH, return_value=False)
        hibp.start()
        self.addCleanup(hibp.stop)

    def sign_up(self, username: str, email: str):
        cache.clear()  # the signup throttle counts every POST from the test client's address
        with tasks_run_inline(process_signup), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("signup"), {"username": username, "email": email, "password1": PASSWORD, "password2": PASSWORD}
            )
        self.client.logout()
        return response

    def register_and_verify(self, username: str, email: str) -> User:
        response = self.sign_up(username, email)
        form = response.context["form"] if response.context and "form" in response.context else None
        self.assertEqual(response.status_code, 302, form.errors if form is not None else response)
        user = User.objects.get(username=username)
        self.assertFalse(user.is_active)
        verification = EmailVerification.objects.get(user=user)
        self.client.get(reverse("verify_email", args=[str(verification.token)]))
        self.client.logout()
        user.refresh_from_db()
        self.assertTrue(user.is_active, "following the verification link did not activate the account")
        return user

    def logged_in_as(self, identifier: str) -> int | None:
        """Log in through the login form with ``identifier``; the id of the account the session holds, if any."""
        cache.clear()
        self.client.logout()
        self.client.post(reverse("login"), {"username": identifier, "password": PASSWORD})
        user_id = self.client.session.get("_auth_user_id")
        self.client.logout()
        return int(user_id) if user_id is not None else None


class RegisteredAccountLogsInWithAnySpellingTests(_SignupTestCase):
    """An account registered through the signup form logs in with every spelling of its username and addresses."""

    def test_username_and_dotted_address(self) -> None:
        user = self.register_and_verify(USERNAME, DOTTED)
        for identifier in (*USERNAME_SPELLINGS, *DOTTED_SPELLINGS):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.logged_in_as(identifier), user.pk)

    def test_address_registered_with_a_plus_tag(self) -> None:
        user = self.register_and_verify("plususer", PLUS)
        self.assertEqual(user.email, PLUS)
        for identifier in PLUS_SPELLINGS:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.logged_in_as(identifier), user.pk)

    def test_address_registered_without_a_tag(self) -> None:
        user = self.register_and_verify("plainuser", PLAIN)
        for identifier in PLAIN_SPELLINGS:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.logged_in_as(identifier), user.pk)

    def test_verified_secondary_address_in_any_spelling(self) -> None:
        user = self.register_and_verify(USERNAME, DOTTED)
        ProfileEmail.objects.create(profile=user.profile, email=SECONDARY, is_verified=True)
        for identifier in SECONDARY_SPELLINGS:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.logged_in_as(identifier), user.pk)

    def test_unverified_secondary_address_logs_in_nobody(self) -> None:
        user = self.register_and_verify(USERNAME, DOTTED)
        ProfileEmail.objects.create(profile=user.profile, email=SECONDARY, is_verified=False)
        for identifier in SECONDARY_SPELLINGS:
            with self.subTest(identifier=identifier):
                self.assertIsNone(self.logged_in_as(identifier))

    def test_wrong_password_with_a_spelling_still_fails(self) -> None:
        self.register_and_verify(USERNAME, DOTTED)
        cache.clear()
        self.client.post(reverse("login"), {"username": "foo.bar", "password": "not the password"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_another_account_with_a_similar_name_is_not_reached(self) -> None:
        self.register_and_verify(USERNAME, DOTTED)
        self.assertIsNone(self.logged_in_as("foobarr"))
        self.assertIsNone(self.logged_in_as("fooba"))
        self.assertIsNone(self.logged_in_as("samrivera@gmail.com"))


class SecondRegistrationIsRefusedTests(_SignupTestCase):
    """No second account is created for any spelling of a name or address an account already holds."""

    def _assert_no_second_account(self, username: str, email: str) -> None:
        before = User.objects.count()
        self.sign_up(username, email)
        self.assertEqual(User.objects.count(), before, f"a second account was created for {username!r} / {email!r}")

    def test_any_spelling_of_the_address_creates_no_account(self) -> None:
        for registered, spellings in ((DOTTED, DOTTED_SPELLINGS), (PLUS, PLUS_SPELLINGS), (PLAIN, PLAIN_SPELLINGS)):
            owner = self.register_and_verify(f"owner{len(registered)}", registered)
            for index, spelling in enumerate(spellings):
                with self.subTest(registered=registered, spelling=spelling):
                    self.assertTrue(is_email_taken(spelling))
                    self._assert_no_second_account(f"newcomer{index}x{len(registered)}", spelling)
                    self.assertEqual(find_user_by_identifier(spelling), owner)

    def test_any_spelling_of_an_unverified_accounts_address_creates_no_account(self) -> None:
        self.sign_up("pending", PLAIN)
        self.assertTrue(User.objects.filter(username="pending", is_active=False).exists())
        for index, spelling in enumerate(PLAIN_SPELLINGS):
            with self.subTest(spelling=spelling):
                self._assert_no_second_account(f"latecomer{index}", spelling)

    def test_any_spelling_of_a_verified_secondary_creates_no_account(self) -> None:
        owner = self.register_and_verify(USERNAME, DOTTED)
        ProfileEmail.objects.create(profile=owner.profile, email=SECONDARY, is_verified=True)
        for index, spelling in enumerate(SECONDARY_SPELLINGS):
            with self.subTest(spelling=spelling):
                self._assert_no_second_account(f"latecomer{index}", spelling)

    def test_any_spelling_of_the_username_creates_no_account_and_says_so(self) -> None:
        self.register_and_verify(USERNAME, DOTTED)
        for index, spelling in enumerate(REGISTRABLE_USERNAME_SPELLINGS):
            with self.subTest(spelling=spelling):
                self.assertTrue(username_is_taken(spelling))
                cache.clear()
                response = self.client.post(
                    reverse("signup"),
                    {
                        "username": spelling,
                        "email": f"fresh{index}@mailbox.org",
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    },
                )
                self.assertFalse(User.objects.filter(email=f"fresh{index}@mailbox.org").exists())
                self.assertContains(response, "already exists")

    def test_separator_spellings_the_form_cannot_register_are_still_taken(self) -> None:
        self.register_and_verify(USERNAME, DOTTED)
        for spelling in ("foo.bar", "_f-o-o-b-a-r-", "foo-bar"):
            with self.subTest(spelling=spelling):
                self.assertTrue(username_is_taken(spelling))
                self._assert_no_second_account(spelling, f"{normalize_username_key(spelling)}x@mailbox.org")

    def test_a_distinct_address_and_name_still_register(self) -> None:
        self.register_and_verify(USERNAME, DOTTED)
        self.register_and_verify("someoneelse", "sam.b.rivera@gmail.com")


class LoginSupportTests(TestCase):
    """The pieces the login form relies on answer every spelling as the account."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.user = _verified_user(USERNAME, DOTTED, password=PASSWORD)
        ProfileEmail.objects.create(profile=self.user.profile, email=SECONDARY, is_verified=True)
        self.every_spelling = (*USERNAME_SPELLINGS, *DOTTED_SPELLINGS, *SECONDARY_SPELLINGS)

    def test_derived_credential_salt_is_the_same_for_every_spelling(self) -> None:
        AccountKdf.objects.set_auth_salt(self.user, "c2FsdHNhbHRzYWx0c2FsdA==")
        for identifier in self.every_spelling:
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    login_params_for_identifier(identifier),
                    {"mode": "derived", "auth_salt": "c2FsdHNhbHRzYWx0c2FsdA=="},
                )

    def test_decoy_salt_does_not_tell_spellings_of_a_missing_account_apart(self) -> None:
        for spellings in (
            ("nobody.here", "nobodyhere", "_N-o-b-o-d-y-here"),
            ("no.body+a@gmail.com", "nobody@gmail.com", "NoBody+b@googlemail.com"),
        ):
            with self.subTest(spellings=spellings):
                salts = {login_params_for_identifier(identifier)["auth_salt"] for identifier in spellings}
                self.assertEqual(len(salts), 1)

    def test_every_spelling_shares_the_accounts_lockout_counter(self) -> None:
        from urbanlens.dashboard.controllers.account import _lockout_key_for_identifier

        keys = {_lockout_key_for_identifier(identifier) for identifier in self.every_spelling}
        self.assertEqual(keys, {f"uid:{self.user.pk}"})

    def test_spellings_of_a_missing_username_share_one_counter(self) -> None:
        from urbanlens.dashboard.controllers.account import _lockout_key_for_identifier

        keys = {
            _lockout_key_for_identifier(identifier) for identifier in ("ghost.user", "ghost_user", "-G-h-o-s-t-user-")
        }
        self.assertEqual(len(keys), 1)

    def test_password_reset_finds_the_account_by_any_spelling_and_mails_its_primary(self) -> None:
        from django.core import mail

        for identifier in (*DOTTED_SPELLINGS, *SECONDARY_SPELLINGS):
            with self.subTest(identifier=identifier):
                mail.outbox.clear()
                cache.clear()
                with tasks_run_inline(send_password_reset), self.captureOnCommitCallbacks(execute=True):
                    self.client.post(reverse("password_reset"), {"email": identifier})
                self.assertEqual([message.to for message in mail.outbox], [[DOTTED]])

    def test_inactive_account_is_not_found_by_a_key_spelling(self) -> None:
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertIsNone(find_user_by_username("foo.bar"))
        self.assertEqual(find_user_by_username("foo.bar", active_only=False), self.user)

    def test_a_key_two_legacy_accounts_share_resolves_to_neither(self) -> None:
        twin = baker.make(User, username="legacy_twin", is_active=True)
        Profile.objects.filter(user=twin).update(username_key=normalize_username_key(USERNAME))
        self.assertIsNone(find_user_by_username("foo.bar"))
        self.assertEqual(find_user_by_username(USERNAME), self.user)
        self.assertEqual(find_user_by_username("legacy_twin"), twin)

    def test_names_that_fold_long_keep_distinct_keys(self) -> None:
        # NFKC-casefold turns each "ß" into "ss", so these keys exceed the 150 characters a username may have.
        longer, shorter = "ß" * 75 + "q", "ß" * 75
        self.assertNotEqual(normalize_username_key(longer), normalize_username_key(shorter))
        baker.make(User, username=shorter, is_active=True)
        self.assertFalse(username_is_taken(longer))

    def test_renaming_moves_the_key(self) -> None:
        self.user.username = "renamed_explorer"
        self.user.save(update_fields=["username"])
        self.assertIsNone(find_user_by_username("foo.bar"))
        self.assertEqual(find_user_by_username("renamed.explorer"), self.user)
        self.assertFalse(username_is_taken("foo.bar"))


# -- Invitations and search -----------------------------------------------------------------------------------------


class _InviteeTestCase(TestCase):
    """An inviter, and an invitee reachable at a dotted primary, a tagged secondary, and a username."""

    def setUp(self) -> None:
        super().setUp()
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(
            email_limit_per_hour=0, email_limit_per_day=0, email_limit_per_month=0
        )
        self.inviter_user = _verified_user("inviter", "inviter@mailbox.org")
        self.inviter = self.inviter_user.profile
        self.invitee_user = _verified_user(USERNAME, DOTTED)
        self.invitee = self.invitee_user.profile
        ProfileEmail.objects.create(profile=self.invitee, email="second.address+urbanlens@gmail.com", is_verified=True)
        self.every_address = (*DOTTED_SPELLINGS, *SECONDARY_SPELLINGS)


class FriendInvitationMatchesAnySpellingTests(_InviteeTestCase):
    def test_every_spelling_reaches_the_invitee(self) -> None:
        for address in self.every_address:
            with self.subTest(address=address):
                FriendInvitation.objects.all().delete()
                with tasks_run_inline(deliver_friend_invitation), self.captureOnCommitCallbacks(execute=True):
                    invitation = invite_by_email(self.inviter, address, url_builder=_url)
                invitation.refresh_from_db()
                self.assertEqual(invitation.invitee, self.invitee)

    def test_inviting_any_spelling_of_your_own_addresses_is_a_self_invite(self) -> None:
        for address in self.every_address:
            with self.subTest(address=address), self.assertRaises(SelfInviteError):
                invite_by_email(self.invitee, address, url_builder=_url)


class TripInvitationMatchesAnySpellingTests(_InviteeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trip = Trip.objects.create(name="Rust Belt Ramble", creator=self.inviter)
        TripMembership.objects.get_or_create(
            trip=self.trip, profile=self.inviter, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED}
        )

    def test_every_spelling_reaches_the_invitee(self) -> None:
        for address in self.every_address:
            with self.subTest(address=address):
                TripInvitation.objects.all().delete()
                with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
                    invitation = invite_to_trip_by_email(self.trip, self.inviter, address, invitation_url_builder=_url)
                invitation.refresh_from_db()
                self.assertEqual(invitation.invitee, self.invitee)

    def test_invitation_to_a_spelling_binds_when_the_address_is_verified_later(self) -> None:
        newcomer = baker.make(User, username="newcomer", email="new.comer+trips@gmail.com", is_active=True)
        invitation = invite_to_trip_by_email(
            self.trip, self.inviter, "NewComer@googlemail.com", invitation_url_builder=_url
        )
        self.assertEqual(bind_invitations_to_account(newcomer), 1)
        invitation.refresh_from_db()
        self.assertEqual(invitation.invitee, newcomer.profile)

    def test_adding_a_member_by_any_spelling_of_their_username(self) -> None:
        for spelling in USERNAME_SPELLINGS:
            with self.subTest(spelling=spelling):
                TripMembership.objects.filter(trip=self.trip, profile=self.invitee).delete()
                membership, created = add_member_by_username(self.trip, self.inviter, spelling)
                self.assertTrue(created)
                self.assertEqual(membership.profile, self.invitee)


class VisitTagMatchesAnySpellingTests(_InviteeTestCase):
    def test_a_tag_to_any_spelling_resolves_when_the_address_is_verified(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource

        location = baker.make(Location, latitude="42.200000", longitude="-73.800000", official_name="Grain Elevator")
        pin = Pin.objects.create(profile=self.inviter, location=location)
        visit = PinVisit.objects.create(
            pin=pin, visited_at=datetime.datetime(2026, 7, 1, 12, tzinfo=datetime.UTC), source=VisitSource.MANUAL
        )
        newcomer = baker.make(User, username="tagged", email="tag.ged+visits@gmail.com", is_active=True)
        for spelling in ("taggedd@gmail.com", "t.a.g.g.e.d@googlemail.com", "tagged+x@gmail.com"):
            ExternalVisitParticipant.objects.create(visit=visit, display_name=spelling, email_hash=hash_email(spelling))
        self.assertEqual(process_pending_visit_invites(newcomer), 2)


class UsernameInvitesAndSearchTests(_InviteeTestCase):
    def test_safety_partner_invite_by_any_spelling(self) -> None:
        for spelling in USERNAME_SPELLINGS:
            with self.subTest(spelling=spelling):
                checkin = baker.make(
                    "dashboard.SafetyCheckin",
                    profile=self.inviter,
                    title="Test hike",
                    checkin_by=timezone.now() + datetime.timedelta(hours=2),
                    grace_period=datetime.timedelta(hours=1),
                )
                partner = invite_checkin_partner(checkin, inviter=self.inviter, username=spelling)
                self.assertEqual(partner.profile, self.invitee)

    def test_user_search_matches_any_spelling(self) -> None:
        baker.make(User, username="unrelated")
        for query in ("foo.bar", "foo_b", "F-O-O", "o.b.a", "FooBar"):
            with self.subTest(query=query):
                self.assertEqual(list(Profile.objects.filter(username_search_q(query))), [self.invitee])

    def test_emergency_contact_by_any_spelling_of_a_connections_username(self) -> None:
        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
        from urbanlens.dashboard.services.visits.safety import resolve_contact_inputs

        Friendship.objects.create(from_profile=self.inviter, to_profile=self.invitee, status=FriendshipStatus.ACCEPTED)
        inputs, rejected = resolve_contact_inputs(
            self.inviter, [{"username": spelling} for spelling in USERNAME_SPELLINGS]
        )
        self.assertEqual(rejected, [])
        self.assertEqual({contact for contact, _email, _label in inputs}, {self.invitee})


class UsernameKeyBackfillTests(TestCase):
    def test_backfill_recomputes_every_stale_key(self) -> None:
        import importlib

        from django.apps import apps

        migration = importlib.import_module("urbanlens.dashboard.migrations.0062_backfill_username_key_and_gmail_alias")
        user = baker.make(User, username="Foo_Bar")
        Profile.objects.filter(user=user).update(username_key="")
        migration.backfill_username_keys(apps, None)
        self.assertEqual(Profile.objects.get(user=user).username_key, normalize_username_key("foobar"))

    def test_googlemail_fold_rewrites_stored_forms(self) -> None:
        import importlib

        from django.apps import apps

        migration = importlib.import_module("urbanlens.dashboard.migrations.0062_backfill_username_key_and_gmail_alias")
        user = baker.make(User, username="alias", email="Some.One@googlemail.com")
        Profile.objects.filter(user=user).update(
            primary_email_normalized="someone@googlemail.com", verified_primary_email="someone@googlemail.com"
        )
        secondary = ProfileEmail.objects.create(profile=user.profile, email="other@googlemail.com", is_verified=True)
        ProfileEmail.objects.filter(pk=secondary.pk).update(normalized_email="other@googlemail.com")
        migration.fold_googlemail_into_gmail(apps, None)
        profile = Profile.objects.get(user=user)
        self.assertEqual(
            (profile.primary_email_normalized, profile.verified_primary_email),
            ("someone@gmail.com", "someone@gmail.com"),
        )
        self.assertEqual(ProfileEmail.objects.get(pk=secondary.pk).normalized_email, "other@gmail.com")

    @staticmethod
    def _friend_invitation(inviter: User) -> FriendInvitation:
        invitation = FriendInvitation.objects.create(inviter=inviter.profile, email="Friend.Ly@googlemail.com")
        FriendInvitation.objects.filter(pk=invitation.pk).update(email_normalized="friendly@googlemail.com")
        return invitation

    def test_googlemail_fold_reverses_to_the_old_forms(self) -> None:
        import importlib

        from django.apps import apps

        migration = importlib.import_module("urbanlens.dashboard.migrations.0062_backfill_username_key_and_gmail_alias")
        user = baker.make(User, username="alias", email="Some.One@googlemail.com")
        Profile.objects.filter(user=user).update(verified_primary_email="someone@gmail.com")
        secondary = ProfileEmail.objects.create(profile=user.profile, email="other@googlemail.com", is_verified=True)
        invitation = self._friend_invitation(user)
        migration.fold_googlemail_into_gmail(apps, None)
        self.assertEqual(FriendInvitation.objects.get(pk=invitation.pk).email_normalized, "friendly@gmail.com")

        migration.unfold_googlemail(apps, None)
        profile = Profile.objects.get(user=user)
        self.assertEqual(
            (profile.primary_email_normalized, profile.verified_primary_email),
            ("someone@googlemail.com", "someone@googlemail.com"),
        )
        self.assertEqual(ProfileEmail.objects.get(pk=secondary.pk).normalized_email, "other@googlemail.com")
        self.assertEqual(FriendInvitation.objects.get(pk=invitation.pk).email_normalized, "friendly@googlemail.com")
