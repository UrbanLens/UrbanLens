"""A building's wiki drops a Wikipedia article seeded from the match at its point, which was its campus's (P262).

A wiki nested under another, holding no place of its own or one of a multi-building parcel's buildings, takes no
Wikipedia article from now on (``wiki_seed.takes_wikipedia_article``). One already seeded is removed only while it is
untouched: its first revision is the seed, every later one is ``localize_article_images`` changing nothing but image
addresses, no revision has an editor, and the article still holds the last revision's text. Anything a person wrote
stays. On the dev stack this is 11 of HRSH's child wikis, each holding the campus's article.
"""

import re

from django.db import migrations
from django.db.models import Exists, OuterRef, Q

_SEEDED = "Seeded from Wikipedia"
_IMAGES_LOCALIZED = "Images stored on this site"
_IMAGE_ADDRESS = re.compile(r"(!\[[^\]]*\])\([^)]*\)")
_CHUNK_SIZE = 200
_MULTI_BUILDING_THRESHOLD = 2


def _without_image_addresses(content):
    return _IMAGE_ADDRESS.sub(r"\1()", content)


def _untouched(content, revisions):
    if not revisions or revisions[0]["edit_summary"] != _SEEDED or revisions[-1]["content"] != content:
        return False
    if any(revision["editor_id"] is not None for revision in revisions):
        return False
    previous = revisions[0]["content"]
    for revision in revisions[1:]:
        if revision["edit_summary"] != _IMAGES_LOCALIZED or _without_image_addresses(revision["content"]) != _without_image_addresses(previous):
            return False
        previous = revision["content"]
    return True


def drop_building_wikipedia_seeds(apps, schema_editor):
    Article = apps.get_model("dashboard", "Article")
    ArticleRevision = apps.get_model("dashboard", "ArticleRevision")

    by_a_person = ArticleRevision.objects.filter(article=OuterRef("pk")).filter(Q(editor__isnull=False) | ~Q(edit_summary__in=[_SEEDED, _IMAGES_LOCALIZED]))
    candidates = list(
        Article.objects.filter(wiki__parent_wiki__isnull=False, last_edited_by__isnull=True)
        .filter(Q(wiki__place__isnull=True) | Q(wiki__place__kind="building", wiki__place__parent_relation="part_of", wiki__place__parent__building_child_count__gte=_MULTI_BUILDING_THRESHOLD))
        .filter(~Exists(by_a_person))
        .order_by("pk")
        .values_list("pk", flat=True),
    )
    doomed = []
    for start in range(0, len(candidates), _CHUNK_SIZE):
        chunk = candidates[start : start + _CHUNK_SIZE]
        # Locked before the revisions are read, as the article editor locks it: an edit is either read here or waits.
        articles = list(Article.objects.select_for_update().filter(pk__in=chunk, last_edited_by__isnull=True).values_list("pk", "content"))
        revisions = {}
        for revision in ArticleRevision.objects.filter(article_id__in=chunk).order_by("created", "pk").values("article_id", "editor_id", "edit_summary", "content"):
            revisions.setdefault(revision["article_id"], []).append(revision)
        doomed.extend(pk for pk, content in articles if _untouched(content, revisions.get(pk, [])))
    ArticleRevision.objects.filter(article_id__in=doomed).delete()
    Article.objects.filter(pk__in=doomed).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0053_notification_fold"),
    ]

    operations = [
        migrations.RunPython(drop_building_wikipedia_seeds, migrations.RunPython.noop),
    ]
