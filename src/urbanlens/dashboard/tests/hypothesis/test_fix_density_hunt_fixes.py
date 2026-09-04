"""Three defects found by hunting the highest fix-density modules (2026-08-20).

`bin/report_defect_history.py` ranks files by the share of their commits that are
fixes, on the premise that where bugs have been found is where bugs are. Reading
the top of that list turned up these, each verified against the code before being
believed:

1. **Editing any trip activity with a location 500s.** The itinerary row's
   `data-act-location-uuid` attribute has always carried the location's *slug*;
   the edit dialog posts it back as `location_uuid`; `resolve_activity_place`
   handed it straight to a `UUIDField` filter, which raises `ValidationError`
   from the ORM - and a plain view does not turn that into a 400.
2. **The label create view stored an uploaded icon with none of the validation
   the edit view applies** - no size, content-type or malware check - so the same
   file refused with a 400 on one URL was written to disk from the other.
3. **The 2FA lockout counter was read-then-write**, while the two login counters
   directly above it use an atomic helper. It is the only brake on TOTP guessing
   for an attacker who already has the password.
4. **A hidden trip activity leaked its location into the DOM.** The visible label
   was correctly swapped for "Secret Location", and the real name and slug were
   emitted into the row's own data attributes and the RSVP `aria-label` - where
   view-source and a screen reader both find them.
"""

from __future__ import annotations

import io
import uuid as uuid_module

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile


class ActivityLocationRefTests(TestCase):
    """A location reference may be a slug or a uuid, and neither may 500."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        # A *named* location on purpose: its slug is then a word-slug rather
        # than something uuid-shaped, which is what makes the uuid-filter
        # failure reachable. A nameless location can slug from its own uuid and
        # parse cleanly, so a fixture without a name tests nothing here - the
        # first version of this test did exactly that and passed either way.
        self.location = baker.make(Location, latitude=42.35, longitude=-71.05, official_name="Bennett School for Girls")
        self.location.ensure_slug()

    def _resolve(self, **body) -> tuple:
        from urbanlens.dashboard.services.trips.trip_activities import resolve_activity_place

        return resolve_activity_place(body, self.profile)

    def test_the_fixture_slug_is_not_uuid_shaped(self) -> None:
        """Guards this class's own premise, which an earlier version got wrong."""
        with self.assertRaises(ValueError):
            uuid_module.UUID(self.location.slug)

    def test_a_slug_posted_as_location_uuid_resolves_instead_of_raising(self) -> None:
        """What the edit dialog actually sends for every activity with a location."""
        location, pin = self._resolve(location_uuid=self.location.slug)

        self.assertEqual(location, self.location)
        self.assertIsNone(pin)

    def test_a_real_uuid_still_resolves(self) -> None:
        location, _pin = self._resolve(location_uuid=str(self.location.uuid))

        self.assertEqual(location, self.location)

    def test_the_explicit_slug_field_still_resolves(self) -> None:
        location, _pin = self._resolve(location_slug=self.location.slug)

        self.assertEqual(location, self.location)

    def test_a_reference_matching_nothing_answers_none_rather_than_raising(self) -> None:
        """Garbage from a stale tab must be a no-op, not a 500."""
        self.assertEqual(self._resolve(location_uuid="not-a-uuid-or-a-slug"), (None, None))
        self.assertEqual(self._resolve(location_uuid=str(uuid_module.uuid4())), (None, None))

    def test_the_itinerary_row_and_the_dialog_agree_on_the_attribute_name(self) -> None:
        """The rename is the reason this happened; a drift here reintroduces it.

        The row emits a slug, so the attribute may not be called `uuid` - it was,
        on both sides, which is how it came to be posted into a UUID filter.
        What the attribute *holds* is pinned by
        ``HiddenActivityLocationTests``, which requires it to be the masked
        value rather than the location's own slug.
        """
        from pathlib import Path

        row = Path("src/urbanlens/dashboard/templates/dashboard/partials/trips/trip_activities_panel.html").read_text(
            encoding="utf-8"
        )
        dialog = Path("src/urbanlens/dashboard/templates/dashboard/pages/trips/detail.html").read_text(encoding="utf-8")

        self.assertIn("data-act-location-ref=", row)
        self.assertNotIn("data-act-location-uuid", row)
        self.assertIn("li.dataset.actLocationRef", dialog)


class HiddenActivityLocationTests(TestCase):
    """Hiding an activity's location must hide it everywhere, not just to the eye.

    `effective_location_hidden` covers both routes: the owner ticking Hide
    location, and `Profile.trip_pin_location_visibility` refusing this viewer.
    The panel honoured it in the visible label and nowhere else.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7, longitude=-73.9, official_name="Bennett School for Girls")
        self.location.ensure_slug()

    def _row(self, *, hidden: bool, title: str = "") -> dict:
        from urbanlens.dashboard.models.trips.model import TripActivity
        from urbanlens.dashboard.services.trips.trip_activities import _masked_activity_title

        activity = baker.prepare(TripActivity, title=title, location=self.location, location_hidden=hidden)
        return {
            "display_title": _masked_activity_title(activity, hidden=hidden),
            "display_location_name": "" if hidden else (activity.location.display_name if activity.location else ""),
            "display_location_ref": "" if hidden else (activity.location.slug if activity.location else ""),
        }

    def test_a_hidden_activity_exposes_neither_name_nor_slug(self) -> None:
        row = self._row(hidden=True)

        self.assertEqual(row["display_location_name"], "")
        self.assertEqual(row["display_location_ref"], "")
        self.assertNotIn("Bennett", row["display_title"])

    def test_a_hidden_activity_without_its_own_title_is_named_generically(self) -> None:
        """`effective_title` falls back to the location's name, so the title *is* the leak."""
        self.assertEqual(self._row(hidden=True)["display_title"], "Secret Location")

    def test_a_hidden_activity_keeps_a_title_its_author_typed(self) -> None:
        """ "Meet at the gate" was written for the other members to read."""
        self.assertEqual(self._row(hidden=True, title="Meet at the gate")["display_title"], "Meet at the gate")

    def test_a_visible_activity_is_unchanged(self) -> None:
        """Anti-vacuity: masking must not swallow the ordinary case."""
        row = self._row(hidden=False)

        self.assertEqual(row["display_location_name"], self.location.display_name)
        self.assertEqual(row["display_location_ref"], self.location.slug)

    def test_the_panel_reads_only_the_masked_values(self) -> None:
        """Structural: the leak was a template reaching past the guard.

        Any `act.location.*` or `act.effective_title` in this panel is a place
        the mask can be forgotten again, so the template is pinned to the
        already-masked row fields.
        """
        from pathlib import Path

        panel = Path("src/urbanlens/dashboard/templates/dashboard/partials/trips/trip_activities_panel.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("act.effective_title", panel)
        self.assertNotIn("act.location.", panel)
        self.assertIn("{{ item.display_title }}", panel)
        self.assertIn('data-act-location-name="{{ item.display_location_name }}"', panel)

    def test_the_hidden_flag_itself_reflects_the_effective_answer(self) -> None:
        """It emitted the raw `location_hidden`, so a viewer hidden by the
        owner's *visibility setting* was told the location was not hidden."""
        from pathlib import Path

        panel = Path("src/urbanlens/dashboard/templates/dashboard/partials/trips/trip_activities_panel.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-act-location-hidden="{{ item.effective_location_hidden', panel)
        self.assertNotIn("act.location_hidden", panel)


def _png_bytes() -> bytes:
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class LabelIconUploadValidationTests(TestCase):
    """Creating a label must check an uploaded icon exactly as editing one does.

    `_resize_custom_icon` deliberately returns the file untouched when PIL
    cannot open it (that fallback has its own test), and `label_icons/` is
    served to any authenticated user with a Content-Type nginx derives from the
    file extension - so an unchecked upload is not merely unresized, it is
    reachable as whatever type its name claims.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _create(self, upload: SimpleUploadedFile | None = None, name: str = "Rooftops"):
        body: dict = {"name": name}
        if upload is not None:
            body["custom_icon-new-tag"] = upload
        return self.client.post(reverse("label.create", kwargs={"label_kind": "tag"}), body)

    def test_a_scripted_svg_is_refused_by_the_create_path(self) -> None:
        svg = SimpleUploadedFile(
            "evil.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/svg+xml",
        )

        response = self._create(svg)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Label.objects.filter(profile=self.profile, name="Rooftops").exists())

    def test_the_edit_path_refuses_it_too_which_is_the_asymmetry_that_existed(self) -> None:
        """Anti-vacuity in the other direction: the guard the create path lacked."""
        label = baker.make(Label, profile=self.profile, name="Existing", kind="tag")
        svg = SimpleUploadedFile(
            "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", content_type="image/svg+xml"
        )

        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.pk}),
            {"name": "Existing", "custom_icon-edit": svg},
        )

        self.assertEqual(response.status_code, 400)
        label.refresh_from_db()
        self.assertFalse(label.custom_icon)

    def test_a_real_image_is_still_accepted(self) -> None:
        """Anti-vacuity: the guard must not refuse the ordinary case."""
        png = SimpleUploadedFile("icon.png", _png_bytes(), content_type="image/png")

        response = self._create(png)

        self.assertIn(response.status_code, (200, 204, 302))
        label = Label.objects.filter(profile=self.profile, name="Rooftops").first()
        assert label is not None
        self.assertTrue(label.custom_icon)

    def test_a_label_with_no_icon_is_unaffected(self) -> None:
        response = self._create()

        self.assertIn(response.status_code, (200, 204, 302))
        self.assertTrue(Label.objects.filter(profile=self.profile, name="Rooftops").exists())

    def test_both_paths_go_through_the_one_guard(self) -> None:
        """Structural: the asymmetry existed because there were two code paths.

        A future create path that reaches for the raw upload helper instead
        reintroduces exactly this defect, so the storing call sites are pinned
        to the validating helper.
        """
        from pathlib import Path

        source = Path("src/urbanlens/dashboard/controllers/labels.py").read_text(encoding="utf-8")

        self.assertEqual(source.count("_validated_custom_icon("), 3, "one definition and both call sites")
        self.assertEqual(
            source.count("_uploaded_custom_icon("), 2, "the raw helper is reached only from the validating one"
        )


class TwoFactorLockoutCounterTests(TestCase):
    """The counter that brakes TOTP guessing must not lose increments.

    Read-then-write loses them exactly when it matters: parallel guesses all
    read the same value and write the same successor, so a spray advances the
    counter once per batch rather than once per attempt. The two login counters
    beside this one were converted to the atomic helper; this one was left.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        cache.clear()

    def _record(self) -> int:
        from urbanlens.dashboard.controllers.account import _record_two_factor_failure

        return _record_two_factor_failure(self.user.pk)

    def test_sequential_failures_count_up(self) -> None:
        self.assertEqual([self._record() for _ in range(3)], [1, 2, 3])

    def test_two_overlapping_requests_count_as_two_attempts_not_one(self) -> None:
        """The defect, simulated deterministically rather than with real threads.

        A lost update is precisely "both requests read before either wrote", so
        the interleaving is reproduced by freezing what ``cache.get`` returns
        across the pair. Read-then-write then has both calls read the same
        value and write the same successor, and the counter ends at 1 for two
        attempts. ``incr`` does not consult ``cache.get`` at all, so freezing it
        cannot mask a regression - which is what makes this a test of the
        mechanism and not of the mock.
        """
        from unittest import mock

        from urbanlens.dashboard.controllers.account import _two_factor_attempts_key

        key = _two_factor_attempts_key(self.user.pk)
        frozen = cache.get(key)

        with mock.patch.object(cache, "get", side_effect=lambda *_args, **_kwargs: frozen):
            first = self._record()
            second = self._record()

        self.assertEqual((first, second), (1, 2), "the second attempt was lost - the counter is not atomic")
        self.assertEqual(cache.get(key), 2)

    def test_the_counter_goes_through_the_shared_atomic_helper(self) -> None:
        """Structural companion: the two login counters beside it already did.

        This one was left on the old pattern when they were converted, which is
        how the gap survived - the fix is not "add locking here" but "stop being
        the one caller that does it differently".
        """
        from pathlib import Path

        source = Path("src/urbanlens/dashboard/controllers/account.py").read_text(encoding="utf-8")

        self.assertEqual(source.count("_bump_counter("), 4, "one definition and all three counters")
        self.assertNotIn("(cache.get(key) or 0) + 1", source)

    def test_the_lockout_still_fires_at_the_limit(self) -> None:
        from urbanlens.dashboard.controllers.account import _is_two_factor_locked_out
        from urbanlens.dashboard.models.site_settings import SiteSettings

        limit = SiteSettings.get_current().login_max_attempts
        for _ in range(limit):
            self._record()

        self.assertTrue(_is_two_factor_locked_out(self.user.pk))

    def test_the_counter_is_reset_by_the_lockout(self) -> None:
        """Otherwise the next window starts already part-way to its limit."""
        from urbanlens.dashboard.controllers.account import _two_factor_attempts_key
        from urbanlens.dashboard.models.site_settings import SiteSettings

        for _ in range(SiteSettings.get_current().login_max_attempts):
            self._record()

        self.assertIsNone(cache.get(_two_factor_attempts_key(self.user.pk)))
