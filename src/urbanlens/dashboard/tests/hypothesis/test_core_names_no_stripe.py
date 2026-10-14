"""Core source names no payment provider. The billing plugin owns Stripe, and its files are the only ones that do."""

from __future__ import annotations

from pathlib import Path
import re

from django.test import SimpleTestCase

import urbanlens

PACKAGE_ROOT = Path(urbanlens.__file__).resolve().parent
STRIPE = re.compile(r"stripe", re.IGNORECASE)
# Files and directories the billing plugin owns, matched against the path relative to the package.
PLUGIN_OWNED = (
    "plugins/builtin/billing.py",
    "models/billing/",
    "services/billing/",
    "controllers/billing",
    "templates/dashboard/billing/",
    "_billing_section.html",
    "site_admin_subscriptions.html",
    "_subscription_grants_list.html",
)
NOT_CORE_DIRECTORIES = {"tests", "migrations", "__pycache__"}


def _core_sources(suffix: str) -> list[Path]:
    sources = []
    for path in PACKAGE_ROOT.rglob(f"*{suffix}"):
        parts = path.relative_to(PACKAGE_ROOT).parts
        relative = "/".join(parts)
        if NOT_CORE_DIRECTORIES.intersection(parts) or any(marker in relative for marker in PLUGIN_OWNED):
            continue
        sources.append(path)
    return sources


class CoreNamesNoStripeTests(SimpleTestCase):
    def test_no_core_python_module_mentions_stripe(self) -> None:
        offenders = [
            path.relative_to(PACKAGE_ROOT).as_posix()
            for path in _core_sources(".py")
            if STRIPE.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])

    def test_no_core_template_mentions_stripe(self) -> None:
        offenders = [
            path.relative_to(PACKAGE_ROOT).as_posix()
            for path in _core_sources(".html")
            if STRIPE.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])
