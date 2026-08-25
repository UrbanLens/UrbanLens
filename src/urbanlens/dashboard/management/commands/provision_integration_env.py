r"""Provision (or remove) the accounts the on-demand integration suite runs as.

Run this on the deployment under test, then point the suite at what it prints::

    python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json
    UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json \
        bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org

and afterwards, if the instance is shared::

    python src/urbanlens/manage.py provision_integration_env --purge --execute

Idempotent: re-running refreshes the same accounts (new password, new keys)
rather than accumulating a new pair each time. See
``services.integration_testing.accounts`` for what "refresh" has to mean for a
headless browser to be able to sign in.

**The output contains plaintext credentials.** ``--out`` writes them to a file
so they do not end up in a terminal scrollback or a CI log by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.integration_testing import INTEGRATION_OVERRIDE_ENV_VAR
from urbanlens.dashboard.services.integration_testing.accounts import DEFAULT_ROLES, integration_users, provision, purge
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes
from urbanlens.UrbanLens.settings.app import settings as app_settings


class Command(BaseCommand):
    """Create, refresh, or delete the integration suite's disposable accounts."""

    help = "Provision disposable accounts for the integration test suite, or purge them with --purge."

    def add_arguments(self, parser) -> None:
        """Register CLI arguments."""
        parser.add_argument(
            "--roles",
            default=",".join(DEFAULT_ROLES),
            help=f"Comma-separated role names to provision (default: {','.join(DEFAULT_ROLES)}).",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Password to set on every account. A strong one is generated when omitted, which is the better option.",
        )
        parser.add_argument("--no-api-keys", action="store_true", help="Skip minting external-API keys.")
        parser.add_argument(
            "--external-apis",
            action="store_true",
            help="Leave outbound providers and AI enabled on these accounts. Off by default: every provider outside REData bills per call.",
        )
        parser.add_argument("--out", default=None, help="Write the JSON manifest here instead of to stdout.")
        parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format (default: json).")
        parser.add_argument("--purge", action="store_true", help="Delete the provisioned accounts instead of creating them.")
        parser.add_argument("--execute", action="store_true", help="Required by --purge. Without it, --purge only reports.")
        parser.add_argument(
            "--force",
            action="store_true",
            help=f"Permit running against a production environment. Also requires {INTEGRATION_OVERRIDE_ENV_VAR}=true.",
        )

    def handle(self, *args, **options) -> None:
        """Provision or purge, after checking this is somewhere it may run.

        Raises:
            CommandError: The environment is production and both locks were not
                opened, or ``--purge`` was given without ``--execute``.
        """
        self._check_environment(force=options["force"])

        if options["purge"]:
            self._purge(execute=options["execute"])
            return

        roles = [role.strip() for role in options["roles"].split(",") if role.strip()]
        if not roles:
            raise CommandError("--roles resolved to an empty list.")

        result = provision(
            roles,
            password=options["password"],
            with_api_keys=not options["no_api_keys"],
            external_apis=options["external_apis"],
        )
        manifest = result.manifest(site_url=django_settings.SITE_URL, environment=str(app_settings.environment_name))

        if options["format"] == "text":
            self._write_text(manifest)
            return

        payload = json.dumps(manifest, indent=2)
        destination = options["out"]
        if destination is None:
            self.stdout.write(payload)
            return

        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        # Owner-only. The manifest holds live passwords and API keys, and this
        # is frequently written to a shared /tmp on a shared staging box.
        try:
            path.chmod(0o600)
        except OSError:
            # Windows and some mounted filesystems do not implement this. Not
            # worth failing a provisioning run over, but worth saying out loud.
            self.stderr.write(self.style.WARNING(f"Could not restrict permissions on {path}; it holds plaintext credentials."))

        created = ", ".join(result.created_roles) or "none"
        refreshed = ", ".join(result.refreshed_roles) or "none"
        self.stdout.write(f"Wrote {len(result.accounts)} account(s) to {path}. Created: {created}. Refreshed: {refreshed}.")
        self.stdout.write(f"Point the suite at it with UL_E2E_ACCOUNTS_FILE={path}")

    def _check_environment(self, *, force: bool) -> None:
        """Refuse to run against production unless both locks are open.

        Two locks rather than one because each covers a different mistake:
        ``--force`` covers a command typed in the wrong terminal, and the
        environment variable covers a script that has always carried ``--force``
        being pointed somewhere new.

        Args:
            force: Whether ``--force`` was passed.

        Raises:
            CommandError: This is production and either lock is closed.
        """
        if str(app_settings.environment_name) != EnvironmentTypes.PRODUCTION:
            return
        override = os.environ.get(INTEGRATION_OVERRIDE_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}
        if force and override:
            self.stderr.write(self.style.WARNING("Running against a PRODUCTION environment because --force and the override variable are both set."))
            return
        raise CommandError(
            f"UL_ENVIRONMENT is production. These accounts are driven by a suite that creates and deletes data, so this refuses by default. Pass --force and set {INTEGRATION_OVERRIDE_ENV_VAR}=true if that is genuinely what you want.",
        )

    def _purge(self, *, execute: bool) -> None:
        """Delete the provisioned accounts, or report what would be deleted.

        Args:
            execute: Whether to actually delete. Without it this only reports,
                which is the default because the alternative is a command that
                destroys accounts on a typo.
        """
        candidates = list(integration_users())
        if not candidates:
            self.stdout.write("No integration accounts exist.")
            return

        if not execute:
            self.stdout.write(f"{len(candidates)} integration account(s) would be deleted. Re-run with --execute.")
            for user in candidates:
                self.stdout.write(f"  {user.username} <{user.email}>")
            return

        deleted = purge()
        self.stdout.write(f"Deleted {len(deleted)} integration account(s): {', '.join(deleted)}")

    def _write_text(self, manifest: dict) -> None:
        """Print the manifest as shell-pasteable environment variables.

        For the single-account case, where exporting three variables is less
        ceremony than writing a file and pointing at it.
        """
        self.stdout.write(f"# UrbanLens integration accounts on {manifest['site_url']} ({manifest['environment']})")
        for account in manifest["accounts"]:
            prefix = "UL_E2E_" if account["role"] == "primary" else f"UL_E2E_{account['role'].upper()}_"
            self.stdout.write(f"export {prefix}USERNAME={account['username']}")
            self.stdout.write(f"export {prefix}PASSWORD={account['password']}")
            if account["api_key"]:
                self.stdout.write(f"export {prefix}API_KEY={account['api_key']}")
                self.stdout.write(f"export {prefix}SCOPES={','.join(account['scopes'])}")
