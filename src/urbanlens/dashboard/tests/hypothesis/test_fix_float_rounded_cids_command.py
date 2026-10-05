"""The ``fix_float_rounded_cids`` cleanup: dry run by default, and only CIDs a float64 visibly rounded."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure, PinImportFailureReason
from urbanlens.dashboard.services.apis.locations.cid_validation import float_rounded

TRUE_CID = 0xC989DB53CE5B1234
ROUNDED = float_rounded(TRUE_CID)
OTHER_TRUE_CID = 0xB9E9C74EC4CA3011
#: A real CID that happens to look float-rounded.
SHAPED_BUT_REAL = 6827058720975719000


def _url(cid: int) -> str:
    return f"https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x89c25a2b3c4d5e6f:0x{cid:x}"


class FixFloatRoundedCidsCommandTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.other = baker.make("auth.User").profile
        self.rounded_place = GooglePlace.objects.create(
            latitude=Decimal("42.1"), longitude=Decimal("-76.1"), cid=ROUNDED
        )
        self.real_place = GooglePlace.objects.create(
            latitude=Decimal("42.2"), longitude=Decimal("-76.2"), cid=OTHER_TRUE_CID
        )
        self.repairable = self._failure(self.profile, ROUNDED, _url(TRUE_CID))
        self.unrecoverable = self._failure(self.other, float_rounded(OTHER_TRUE_CID), "")
        self.corroborated = self._failure(self.profile, SHAPED_BUT_REAL, _url(SHAPED_BUT_REAL))
        self.ordinary = self._failure(self.other, 12345, "")

    def _failure(self, profile, cid: int, maps_url: str) -> PinImportFailure:
        return PinImportFailure.objects.create(
            profile=profile, cid=cid, maps_url=maps_url, name="X", reason=PinImportFailureReason.NO_LOCATION_FOUND
        )

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("fix_float_rounded_cids", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_reports_per_table_and_changes_nothing(self) -> None:
        output = self._run()

        self.assertIn("dashboard_google_places: 1 float-shaped cid(s) would be cleared", output)
        self.assertIn("1 would be repaired from maps_url, 1 would be deleted, 1 kept", output)
        self.rounded_place.refresh_from_db()
        self.assertEqual(int(self.rounded_place.cid), ROUNDED)
        self.assertEqual(PinImportFailure.objects.count(), 4)

    def test_execute_clears_repairs_and_deletes_only_rounded_cids(self) -> None:
        self._run("--execute")

        self.rounded_place.refresh_from_db()
        self.real_place.refresh_from_db()
        self.assertIsNone(self.rounded_place.cid)
        self.assertEqual(int(self.real_place.cid), OTHER_TRUE_CID)
        self.assertEqual(
            {(f.pk, int(f.cid)) for f in PinImportFailure.objects.all()},
            {(self.repairable.pk, TRUE_CID), (self.corroborated.pk, SHAPED_BUT_REAL), (self.ordinary.pk, 12345)},
        )

    def test_a_repair_onto_a_cid_the_owner_already_has_deletes_the_rounded_copy(self) -> None:
        existing = self._failure(self.profile, TRUE_CID, _url(TRUE_CID))

        self._run("--execute")

        self.assertFalse(PinImportFailure.objects.filter(pk=self.repairable.pk).exists())
        self.assertTrue(PinImportFailure.objects.filter(pk=existing.pk, cid=TRUE_CID).exists())
