"""Guards on ``SafetyCheckinViewerScopedView``, the watcher-side lookup base.

A safety check-in reveals where a person physically is, right now. The upcoming
partner-facing endpoints - acknowledge, escalate, read the shared live position
- have to resolve a check-in for someone who is *not* its owner, and the rule
they resolve it with is the single riskiest predicate in the API:

* it must admit the owner, and a partner whose invitation is **ACCEPTED**;
* it must refuse a merely ``INVITED`` partner, and anyone whose partner row was
  deleted when they declined or were removed;
* it must be indistinguishable from "no such check-in" in every refusal, since
  a 403 on a slug confirms that a specific person is out somewhere.

The tempting shortcut - ``checkin.partners.filter(profile=...).exists()`` -
passes a happy-path test and quietly admits an invitee who never accepted, so
these tests exercise the INVITED case directly rather than trusting that the
base "uses the service". There is also a test asserting the base and the
owner-scoped sibling answer with byte-identical error bodies, because a client
that could tell the two apart would learn which of them rejected it.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.mixins_safety import SafetyCheckinViewerScopedView
from urbanlens.dashboard.external_api.views import SafetyCheckinScopedView
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.services.safety import create_checkin


class SafetyCheckinViewerScopeTests(TestCase):
    """Who ``get_viewable_checkin`` admits, and who it reports as missing."""

    def setUp(self) -> None:
        """Create an owner with one live check-in, plus three other profiles."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.owner = Profile.objects.get(user=baker.make(User, username="explorer"))
        self.accepted = Profile.objects.get(user=baker.make(User, username="accepted-partner"))
        self.invited = Profile.objects.get(user=baker.make(User, username="invited-partner"))
        self.stranger = Profile.objects.get(user=baker.make(User, username="stranger"))

        self.checkin = create_checkin(
            profile=self.owner,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )
        self.view = SafetyCheckinViewerScopedView()
        self.factory = RequestFactory()

    def _request(self, profile: Profile):
        """A bare request attributed to *profile*'s user.

        The view base reads only ``request.user``; building the request
        directly keeps these tests on the resolution rule itself rather than on
        whichever URL a later batch wires it to.

        Args:
            profile: The profile the request should appear to come from.

        Returns:
            A ``WSGIRequest`` with ``user`` set.
        """
        request = self.factory.get("/")
        request.user = profile.user
        return request

    def _partner(self, profile: Profile, status: str) -> SafetyCheckinPartner:
        """Attach *profile* to the fixture check-in with the given status.

        Args:
            profile: The profile being named as a partner.
            status: One of ``SafetyCheckinPartnerStatus``.

        Returns:
            The created partner row.
        """
        return SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=profile, invited_by=self.owner, status=status)

    def test_owner_resolves_their_own_checkin(self) -> None:
        """The owner is always a legitimate viewer of their own check-in."""
        resolved = self.view.get_viewable_checkin(self._request(self.owner), self.checkin.slug)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, self.checkin.pk)

    def test_accepted_partner_resolves_the_checkin(self) -> None:
        """An accepted partner is exactly who this base exists to serve."""
        self._partner(self.accepted, SafetyCheckinPartnerStatus.ACCEPTED)
        resolved = self.view.get_viewable_checkin(self._request(self.accepted), self.checkin.slug)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, self.checkin.pk)

    def test_merely_invited_partner_is_refused(self) -> None:
        """An unaccepted invite must not open a live location feed.

        This is the case a bare ``partners.filter(profile=...)`` would let
        through: the invite row exists from the moment it is sent, so a
        membership test that ignores ``status`` admits someone who has not
        taken on the responsibility - and may never have seen the invite.
        """
        self._partner(self.invited, SafetyCheckinPartnerStatus.INVITED)
        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.invited), self.checkin.slug))

    def test_removed_partner_loses_access(self) -> None:
        """Revocation is immediate - no cached or grandfathered view."""
        partner = self._partner(self.accepted, SafetyCheckinPartnerStatus.ACCEPTED)
        self.assertIsNotNone(self.view.get_viewable_checkin(self._request(self.accepted), self.checkin.slug))

        partner.delete()
        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.accepted), self.checkin.slug))

    def test_stranger_is_refused(self) -> None:
        """Someone with no relationship to the check-in resolves nothing."""
        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.stranger), self.checkin.slug))

    def test_partner_on_a_different_checkin_is_refused(self) -> None:
        """Being an accepted partner somewhere is not being one here.

        Guards against an entitlement test that forgets to scope by check-in -
        which would hand one accepted invitation to every check-in on the site.
        """
        other_checkin = create_checkin(
            profile=self.stranger,
            title="Different trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
            plan_details="Somewhere else",
            contact_message="Call me",
            contacts=[(None, "other@example.com", "Other")],
        )
        SafetyCheckinPartner.objects.create(checkin=other_checkin, profile=self.accepted, invited_by=self.stranger, status=SafetyCheckinPartnerStatus.ACCEPTED)

        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.accepted), self.checkin.slug))

    def test_unknown_slug_resolves_to_none(self) -> None:
        """A slug matching nothing is the same "nothing" as a refused one."""
        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.owner), "no-such-checkin"))

    def test_lookup_accepts_a_raw_uuid(self) -> None:
        """Directly-linked and older check-ins are addressed by uuid, not slug."""
        resolved = self.view.get_viewable_checkin(self._request(self.owner), str(self.checkin.uuid))
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, self.checkin.pk)

    def test_non_uuid_garbage_does_not_raise(self) -> None:
        """A malformed identifier is a miss, not a 500.

        Comparing a non-uuid string against a ``UUIDField`` raises rather than
        simply not matching, so the uuid fallback has to catch it - otherwise
        any client typo becomes a server error.
        """
        self.assertIsNone(self.view.get_viewable_checkin(self._request(self.owner), "not-a-uuid-@@@"))

    def test_viewer_profile_is_created_on_first_use(self) -> None:
        """A credential holder with no profile row yet still resolves to one.

        Profiles are created lazily, so refusing a user without one would make
        the API's behaviour depend on whether they had ever visited the site -
        an account created purely through OAuth2 would be locked out of a
        feature it is otherwise entitled to.
        """
        fresh_user = baker.make(User, username="never-logged-in")
        Profile.objects.filter(user=fresh_user).delete()
        self.assertFalse(Profile.objects.filter(user=fresh_user).exists())

        request = self.factory.get("/")
        request.user = fresh_user
        viewer = self.view.resolve_viewer(request)

        self.assertEqual(viewer.user_id, fresh_user.pk)
        self.assertEqual(Profile.objects.filter(user=fresh_user).count(), 1)

    def test_not_found_body_matches_the_owner_scoped_base(self) -> None:
        """Both safety surfaces refuse with the identical envelope.

        If the watcher-scoped and owner-scoped bases answered differently, a
        client probing both would learn which one rejected it - which is itself
        information about the check-in.
        """
        watcher_response = self.view.not_found()
        owner_response = SafetyCheckinScopedView()._not_found()
        self.assertEqual(watcher_response.status_code, 404)
        self.assertEqual(watcher_response.data, owner_response.data)

    def test_owner_scoped_base_still_refuses_accepted_partners(self) -> None:
        """The new base must not have widened the owner-only write surface.

        Every endpoint on ``SafetyCheckinScopedView`` is an owner action (edit,
        cancel, delete, invite). If admitting partners had been implemented by
        relaxing *that* base instead of adding this one, every one of those
        writes would silently become available to any accepted partner.
        """
        self._partner(self.accepted, SafetyCheckinPartnerStatus.ACCEPTED)
        owner_scoped = SafetyCheckinScopedView()
        self.assertIsNone(owner_scoped._get_checkin(self._request(self.accepted), self.checkin.slug))
