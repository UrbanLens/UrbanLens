"""Report, and on request delete, the files under ``pin_images/`` that no Image row names (P14)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Walk ``pin_images/`` against every Image row's file names."""

    help = "Find files under pin_images/ that no Image row names. Reports only, unless --delete is given."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--delete", action="store_true", help="Delete what it finds instead of only reporting it.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the sweep and print what it found.

        Args:
            *args: Unused.
            **options: Parsed command options.
        """
        from urbanlens.dashboard.services.media.stored_field import sweep_unnamed_image_files

        report = sweep_unnamed_image_files(delete=options["delete"])
        verb = "Deleted" if options["delete"] else "Found"
        noun = "file" if report.files == 1 else "files"
        self.stdout.write(f"{verb} {report.files} unnamed {noun} under pin_images/ ({report.bytes / 1_048_576:.1f} MiB).")
