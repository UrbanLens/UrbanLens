"""``bin/check_canonical_creates.py`` flags hand-written Location/Label creates, and the tree is clean."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_canonical_creates.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_canonical_creates", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CanonicalCreatesCheckTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_a_raw_location_get_or_create_is_flagged(self) -> None:
        source = "location, _ = Location.objects.get_or_create(latitude=lat, longitude=lng)\n"

        self.assertEqual(len(self.checker.offences(source, "x.py")), 1)

    def test_every_creating_method_on_either_model_is_flagged(self) -> None:
        source = "\n".join(
            f"{model}.objects.{method}(name=n)"
            for model in ("Location", "Label")
            for method in ("create", "get_or_create", "update_or_create")
        )

        self.assertEqual(len(self.checker.offences(source, "x.py")), 6)

    def test_an_alias_get_or_create_is_flagged_but_a_plain_create_is_not(self) -> None:
        source = (
            "PinAlias.objects.get_or_create(pin=p, name__iexact=n)\n"
            "WikiAlias.objects.update_or_create(wiki=w, name=n)\n"
            "PinAlias.objects.create(pin=p, name=n)\n"
        )

        self.assertEqual([line.split(":")[1] for line in self.checker.offences(source, "x.py")], ["1", "2"])

    def test_a_create_on_a_queryset_chained_off_the_manager_is_flagged(self) -> None:
        source = "Label.objects.filter(kind='tag').exclude(pk=1).get_or_create(name=n)\n"

        self.assertEqual(len(self.checker.offences(source, "x.py")), 1)

    def test_an_aliased_import_is_flagged(self) -> None:
        source = "from urbanlens.dashboard.models.labels.model import Label as L\nL.objects.create(name=n)\n"

        self.assertEqual(len(self.checker.offences(source, "x.py")), 1)

    def test_the_helpers_and_other_models_pass(self) -> None:
        source = (
            "Location.objects.get_exact_or_create(lat, lng)\n"
            "Label.objects.resolve_or_create(profile, name, kind)\n"
            "Label.objects.bulk_create(rows, ignore_conflicts=True)\n"
            "Pin.objects.create(profile=p)\n"
        )

        self.assertEqual(self.checker.offences(source, "x.py"), [])

    def test_a_marked_line_is_exempt(self) -> None:
        source = "# canonical-create-ok: restoring a row verbatim\nLabel.objects.create(**fields)\n"

        self.assertEqual(self.checker.offences(source, "x.py"), [])

    def test_the_application_tree_is_clean(self) -> None:
        self.assertEqual(self.checker.main(), 0)
