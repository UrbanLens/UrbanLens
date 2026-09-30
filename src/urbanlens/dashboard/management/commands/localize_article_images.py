"""Point every remote image in a stored article at this site's copy (P165).

Articles saved from now on are rewritten as they are saved (``articles.localize_article_images``). This does the same
for articles saved before, each as a new revision, so the history keeps what the author wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Rewrite remote image addresses in stored articles."""

    help = "Point every remote image in a stored article at this site's copy, as a new revision."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the command's options.

        Args:
            parser: The command's argument parser.
        """
        parser.add_argument("--dry-run", action="store_true", help="Count the articles that would change without saving.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Rewrite each article whose source names a remote image.

        Args:
            *args: Unused.
            **options: ``dry_run``.
        """
        from urbanlens.dashboard.models.article.model import Article
        from urbanlens.dashboard.services.wiki.articles import localize_article_images, save_article

        changed = 0
        for article in Article.objects.filter(content__iregex=r"(!\[|<img)").select_related("pin", "wiki").iterator():
            if localize_article_images(article.content) == article.content:
                continue
            changed += 1
            if not options["dry_run"]:
                save_article(editor=None, content=article.content, edit_summary="Images stored on this site", pin=article.pin, wiki=article.wiki)
        verb = "would change" if options["dry_run"] else "changed"
        self.stdout.write(f"{changed} article(s) {verb}.")
