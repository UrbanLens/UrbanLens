"""Request every GET route as the population's lightest and heaviest accounts, and report what each costs."""

from __future__ import annotations

import json
from pathlib import Path
import re

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.integration_testing import INTEGRATION_OVERRIDE_ENV_VAR, INTEGRATION_USERNAME_PREFIX
from urbanlens.dashboard.services.integration_testing.guards import is_production, production_unlocked
from urbanlens.dashboard.services.integration_testing.population import POPULATION_INFIX
from urbanlens.dashboard.services.integration_testing.route_costs import population_extremes, render, request_host, sweep


class Command(BaseCommand):
    help = "Request every GET route as the population's lightest and heaviest accounts, and report what each costs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--out", required=True, help="Where to write the measurements, as JSON.")
        parser.add_argument("--markdown", help="Where to write the report. Printed when omitted.")
        parser.add_argument("--runs", type=int, default=3, help="Requests per route per account.")
        parser.add_argument("--only", help="Measure only routes whose name matches this regular expression.")
        parser.add_argument("--force", action="store_true", help=f"Permit running against production. Also requires {INTEGRATION_OVERRIDE_ENV_VAR}=true.")

    def handle(self, *args, **options) -> None:
        """Measure both accounts, then write the JSON and the report.

        Raises:
            CommandError: This is production and a lock is closed, ``--runs`` is below one, ``--only`` does not
                compile, or no population account holds a pin.
        """
        if is_production():
            if not production_unlocked(force=options["force"]):
                raise CommandError(f"UL_ENVIRONMENT is production. The sweep requests every page as two accounts; pass --force and set {INTEGRATION_OVERRIDE_ENV_VAR}=true to run it anyway.")
            self.stderr.write(self.style.WARNING("Running against a PRODUCTION environment because --force and the override variable are both set."))
        if options["runs"] < 1:
            raise CommandError("--runs must be at least 1.")
        try:
            only = re.compile(options["only"]) if options["only"] else None
        except re.error as error:
            raise CommandError(f"--only is not a regular expression: {error}") from error
        extremes = population_extremes(f"{INTEGRATION_USERNAME_PREFIX}{POPULATION_INFIX}")
        if extremes is None:
            raise CommandError("No population account holds a pin. Run provision_integration_env --population first.")
        light, heavy = extremes

        result = sweep([("light", light), ("heavy", heavy)], runs=options["runs"], host=request_host(), only=only, progress=self.stderr.write)

        Path(options["out"]).write_text(json.dumps(result.as_json(), indent=1), encoding="utf-8")
        report = render(result, light="light", heavy="heavy")
        if options["markdown"]:
            Path(options["markdown"]).write_text(report, encoding="utf-8")
        else:
            self.stdout.write(report)
