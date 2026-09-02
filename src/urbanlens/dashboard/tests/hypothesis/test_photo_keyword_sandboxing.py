"""Photo keywording never decodes an upload outside the sandbox tier.

``CLAUDE.md``: anything handing user-uploaded bytes to a parser must be
decorated ``@untrusted_parse`` and reached only from a task declaring
``queue=SANDBOX_QUEUE``.

Keywording broke that rule. ``generate_image_keywords`` declares no queue, so
it runs on the ordinary worker - the one holding REData, OAuth and database
credentials, on a network with full egress - and it called Pillow on the
stored upload to build the 512px copy providers get. A decoder
memory-corruption bug in an uploaded image would have landed exactly where
``media-worker`` exists to keep it out of.

The decode now happens once, in the sandbox, into ``Image.analysis_thumbnail``
(``services.media.images.write_image_analysis_thumbnail``); keywording reads
those bytes and parses nothing
(``services.photos.photo_keywords.analysis_jpeg_bytes``).

Four claims, each failing differently:

1. The keywording path imports no image parser and calls no decode
   (:class:`KeywordPathDoesNotDecodeTests`).
2. The writer is sandbox-gated, and the task that calls it declares the
   sandbox queue (:class:`AnalysisThumbnailIsSandboxedTests`).
3. A photo with no analysis copy is skipped, never decoded as a fallback -
   the failure mode that would quietly reintroduce the hole
   (:class:`MissingAnalysisCopyTests`).
4. The copy is a real image file in the same storage family as every other
   photo, so it is served from the media origin with the same rules
   (:class:`AnalysisCopyStorageTests`).
"""

from __future__ import annotations

import ast
import io
import pathlib
from unittest import mock

from django.core.files.base import ContentFile
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.photos import photo_keywords

_KEYWORDS_PATH = pathlib.Path(photo_keywords.__file__)


def _jpeg(width: int = 1200, height: int = 900) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 60, 30)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _photo(*, generate_photo_keywords: bool = True) -> Image:
    profile = _make_profile(generate_photo_keywords=generate_photo_keywords)
    image = baker.make(Image, media_type=MediaKind.PHOTO, profile=profile)
    image.image.save("original.jpg", ContentFile(_jpeg()), save=True)
    return image


class KeywordPathDoesNotDecodeTests(SimpleTestCase):
    """The module keywording runs through holds no image parser at all."""

    def test_photo_keywords_imports_no_image_parser(self) -> None:
        tree = ast.parse(_KEYWORDS_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("PIL", imported, "photo_keywords imports Pillow - the decode belongs in the sandbox writer")

    def test_reading_the_analysis_copy_never_opens_a_parser(self) -> None:
        # The behavioural half of the assertion above: even if an import crept
        # back, analysis_jpeg_bytes must not be what uses it.
        image = mock.Mock()
        image.pk = 1
        image.analysis_thumbnail.__bool__ = mock.Mock(return_value=True)
        image.analysis_thumbnail.open.return_value.__enter__ = mock.Mock(return_value=io.BytesIO(b"raw-bytes"))
        image.analysis_thumbnail.open.return_value.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(PILImage, "open", side_effect=AssertionError("keywording must not decode")) as pil_open:
            self.assertEqual(photo_keywords.analysis_jpeg_bytes(image), b"raw-bytes")
        pil_open.assert_not_called()


class AnalysisThumbnailIsSandboxedTests(SimpleTestCase):
    """The writer is untrusted_parse-gated and only reached from sandbox-queued tasks."""

    def test_the_writer_is_decorated_untrusted_parse(self) -> None:
        from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail

        # untrusted_parse wraps the function; the marker it sets is what the
        # sandbox guard reads at call time.
        self.assertTrue(
            getattr(write_image_analysis_thumbnail, "__wrapped__", None) is not None or hasattr(write_image_analysis_thumbnail, "untrusted_parse_label"),
            "write_image_analysis_thumbnail is not wrapped by @untrusted_parse",
        )

    def test_every_task_reaching_the_writer_declares_the_sandbox_queue(self) -> None:
        """No task can reach the image decode without being on media-worker's queue.

        Transitive on purpose. The decode sits behind a plain helper
        (``_process_photo_upload``), so checking only functions that call it
        directly would pass while a default-queue task two hops away happily
        reached it - which is the shape the original defect had.
        """
        import urbanlens.dashboard.tasks as tasks_module

        tree = ast.parse(pathlib.Path(tasks_module.__file__).read_text(encoding="utf-8"))
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        calls = {
            node.name: {child.func.id for child in ast.walk(node) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)}
            for node in functions
        }

        reaches: set[str] = {"write_image_analysis_thumbnail"}
        changed = True
        while changed:
            changed = False
            for name, callees in calls.items():
                if name not in reaches and callees & reaches:
                    reaches.add(name)
                    changed = True

        offenders: list[str] = []
        for node in functions:
            if node.name not in reaches:
                continue
            decorators = [d for d in node.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "shared_task"]
            decorators += [d for d in node.decorator_list if isinstance(d, ast.Name) and d.id == "shared_task"]
            if not decorators:
                continue  # a plain helper - its own callers are what matter
            declared = [kw.value.id for d in decorators if isinstance(d, ast.Call) for kw in d.keywords if kw.arg == "queue" and isinstance(kw.value, ast.Name)]
            if "SANDBOX_QUEUE" not in declared:
                offenders.append(node.name)
        self.assertEqual(offenders, [], f"these tasks reach the image decode without queue=SANDBOX_QUEUE: {offenders}")

    def test_the_transitive_walk_actually_finds_the_helper(self) -> None:
        """Guards the test above: if the walk stopped being transitive it would pass vacuously."""
        import urbanlens.dashboard.tasks as tasks_module

        tree = ast.parse(pathlib.Path(tasks_module.__file__).read_text(encoding="utf-8"))
        calls = {
            node.name: {child.func.id for child in ast.walk(node) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)}
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        direct = {name for name, callees in calls.items() if "write_image_analysis_thumbnail" in callees}
        self.assertIn("_process_photo_upload", direct, "the helper this walk exists to see through is gone or renamed")

    def test_generate_image_keywords_does_not_call_the_writer(self) -> None:
        # It must not "helpfully" produce the copy itself - that would put the
        # decode back on the default queue, which is the original defect.
        import urbanlens.dashboard.tasks as tasks_module

        tree = ast.parse(pathlib.Path(tasks_module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "generate_image_keywords":
                names = {child.func.id for child in ast.walk(node) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)}
                self.assertNotIn("write_image_analysis_thumbnail", names)
                return
        self.fail("generate_image_keywords not found")


class MissingAnalysisCopyTests(TestCase):
    """No analysis copy means skip, never decode-on-demand."""

    def test_a_photo_without_an_analysis_copy_yields_no_bytes(self) -> None:
        image = _photo()
        self.assertFalse(image.analysis_thumbnail)
        self.assertIsNone(photo_keywords.analysis_jpeg_bytes(image))

    def test_the_ai_plugin_returns_nothing_rather_than_decoding(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import AiVisionKeywordProvider

        image = _photo()
        with mock.patch.object(PILImage, "open", side_effect=AssertionError("must not decode")):
            self.assertEqual(AiVisionKeywordProvider().generate(image), [])

    def test_the_classifier_plugin_returns_nothing_rather_than_decoding(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import ClassifierKeywordProvider

        image = _photo()
        with mock.patch.object(PILImage, "open", side_effect=AssertionError("must not decode")):
            self.assertEqual(ClassifierKeywordProvider().generate(image), [])

    def test_the_backfill_writes_the_copy_and_re_enqueues_keywording(self) -> None:
        # Without the re-enqueue, a photo whose analysis write failed during
        # upload would silently never get AI keywords.
        from urbanlens.dashboard.tasks import generate_image_analysis_thumbnails

        image = _photo(generate_photo_keywords=True)

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            written = generate_image_analysis_thumbnails([image.pk])

        self.assertEqual(written, 1)
        image.refresh_from_db()
        self.assertTrue(image.analysis_thumbnail)
        self.assertEqual(enqueue.call_args.args[1], image.pk)

    def test_the_backfill_skips_a_profile_that_opted_out(self) -> None:
        from urbanlens.dashboard.tasks import generate_image_analysis_thumbnails

        image = _photo(generate_photo_keywords=False)

        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            generate_image_analysis_thumbnails([image.pk])

        image.refresh_from_db()
        self.assertTrue(image.analysis_thumbnail, "the copy is still written; only keywording is gated")
        enqueue.assert_not_called()


class AnalysisCopyStorageTests(TestCase):
    """The copy lives with every other photo, so the media origin's rules apply to it."""

    def test_it_is_written_as_a_jpeg_at_the_declared_size(self) -> None:
        from urbanlens.dashboard.services.media.images import ANALYSIS_THUMBNAIL_MAX_DIMENSION, write_image_analysis_thumbnail

        image = _photo()
        self.assertTrue(write_image_analysis_thumbnail(image))
        image.save(update_fields=["analysis_thumbnail"])

        with image.analysis_thumbnail.open("rb") as stored:
            decoded = PILImage.open(stored)
            decoded.load()
        # JPEG, not the WebP the display thumbnails use: Cloudflare Workers AI
        # is the default vision provider and takes a bare byte array with no
        # format negotiation.
        self.assertEqual(decoded.format, "JPEG")
        self.assertLessEqual(max(decoded.size), ANALYSIS_THUMBNAIL_MAX_DIMENSION)

    def test_it_is_stored_in_the_same_media_family_as_the_original(self) -> None:
        # Same family => the media origin serves it under the same
        # authorization and caching rules as every other photo, which is the
        # whole reason it is a stored ImageField rather than transient bytes.
        from urbanlens.dashboard.models.images.model import pin_image_analysis_thumbnail_path, pin_image_upload_path
        from urbanlens.dashboard.services.media.access import MEDIA_FAMILY_ATTR

        self.assertEqual(
            getattr(pin_image_analysis_thumbnail_path, MEDIA_FAMILY_ATTR, None),
            getattr(pin_image_upload_path, MEDIA_FAMILY_ATTR, None),
        )

    def test_its_path_cannot_collide_with_the_original_or_the_thumbnails(self) -> None:
        from urbanlens.dashboard.models.images.model import pin_image_analysis_thumbnail_path

        path = pin_image_analysis_thumbnail_path(mock.Mock(), "abc-analysis.jpg")
        self.assertTrue(path.startswith("pin_images/analysis/"))

    def test_a_transparent_source_is_flattened_rather_than_failing(self) -> None:
        # JPEG has no alpha channel; RGBA -> RGB alone renders transparency as
        # black, and some modes raise outright.
        from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail

        buffer = io.BytesIO()
        PILImage.new("RGBA", (700, 500), (10, 200, 40, 0)).save(buffer, format="PNG")
        image = baker.make(Image, media_type=MediaKind.PHOTO, profile=_make_profile())
        image.image.save("original.png", ContentFile(buffer.getvalue()), save=True)

        self.assertTrue(write_image_analysis_thumbnail(image))
        image.save(update_fields=["analysis_thumbnail"])
        with image.analysis_thumbnail.open("rb") as stored:
            decoded = PILImage.open(stored)
            decoded.load()
        self.assertEqual(decoded.format, "JPEG")
        self.assertEqual(decoded.mode, "RGB")

    def test_a_second_write_is_a_no_op_without_force(self) -> None:
        from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail

        image = _photo()
        self.assertTrue(write_image_analysis_thumbnail(image))
        self.assertFalse(write_image_analysis_thumbnail(image))

    def test_a_non_photo_is_skipped(self) -> None:
        from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail

        image = baker.make(Image, media_type=MediaKind.DOCUMENT, profile=_make_profile())
        image.image.save("doc.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.assertFalse(write_image_analysis_thumbnail(image))
