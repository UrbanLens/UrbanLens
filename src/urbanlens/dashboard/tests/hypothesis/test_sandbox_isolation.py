"""The sandbox boundary: untrusted parsing runs in media-worker and nowhere else.

Three separable claims, tested separately because each fails differently:

1. The guard refuses an out-of-sandbox parse (:class:`UntrustedParseGuardTests`).
2. The decorators are actually on the parsers, so the guard is reached at all
   (:class:`DecoratedParserTests`) - a guard nothing calls protects nothing.
3. Celery really routes a task declaring ``queue=`` to that queue
   (:class:`SandboxQueueRoutingTests`). This one exists because the whole design
   rests on ``@shared_task(queue=...)`` reaching ``apply_async``'s options, which
   is Celery behaviour rather than ours, and a silent change there would move
   every decode back onto the unrestricted worker with nothing to notice.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from urbanlens.dashboard.services.sandbox import (
    ProcessRole,
    Queue,
    UnsandboxedParseError,
    UntrustedParsePolicy,
    allow_untrusted_parse,
    check_untrusted_parse,
    current_policy,
    current_role,
    sandbox_queue,
    untrusted_parse,
)

#: Every task routed to the sandbox queue, as a set so adding or removing one is
#: a deliberate line in a diff rather than a silent change of blast radius. A
#: task belongs here when it reaches a parser over bytes a user supplied.
EXPECTED_SANDBOX_TASKS = {
    "urbanlens.dashboard.tasks.process_image_upload",
    "urbanlens.dashboard.tasks.generate_image_thumbnails",
    "urbanlens.dashboard.tasks.generate_image_marker_thumbnails",
    "urbanlens.dashboard.tasks.run_user_data_import",
    "urbanlens.dashboard.tasks.scan_comment_image",
    "urbanlens.dashboard.tasks.scan_trip_comment_image",
}


@override_settings(UL_UNTRUSTED_PARSE_POLICY="deny", UL_PROCESS_ROLE="web")
class UntrustedParseGuardTests(SimpleTestCase):
    """``check_untrusted_parse`` enforces the role/policy matrix."""

    def test_denies_outside_the_sandbox(self) -> None:
        with self.assertRaises(UnsandboxedParseError) as ctx:
            check_untrusted_parse("image.decode")
        # The message has to name both halves: which parse, and which container
        # got it wrong. A bare "not allowed" sends the reader to the wrong logs.
        self.assertIn("image.decode", str(ctx.exception))
        self.assertIn("web", str(ctx.exception))

    @override_settings(UL_PROCESS_ROLE="sandbox")
    def test_allows_inside_the_sandbox(self) -> None:
        check_untrusted_parse("image.decode")

    @override_settings(UL_UNTRUSTED_PARSE_POLICY="warn")
    def test_warn_logs_and_proceeds(self) -> None:
        with self.assertLogs("urbanlens.dashboard.services.sandbox.guard", level="WARNING") as logs:
            check_untrusted_parse("video.transcode")
        self.assertIn("video.transcode", "\n".join(logs.output))

    @override_settings(UL_UNTRUSTED_PARSE_POLICY="allow")
    def test_allow_does_not_check(self) -> None:
        check_untrusted_parse("video.transcode")

    def test_explicit_exemption_permits_the_call(self) -> None:
        with allow_untrusted_parse("our own re-encoded thumbnail"):
            check_untrusted_parse("image.decode")

    def test_exemption_does_not_outlive_its_block(self) -> None:
        with allow_untrusted_parse("our own re-encoded thumbnail"):
            check_untrusted_parse("image.decode")
        with self.assertRaises(UnsandboxedParseError):
            check_untrusted_parse("image.decode")

    def test_exemption_is_reset_even_when_the_body_raises(self) -> None:
        with self.assertRaises(ZeroDivisionError), allow_untrusted_parse("reason"):
            _ = 1 / 0
        with self.assertRaises(UnsandboxedParseError):
            check_untrusted_parse("image.decode")

    def test_decorator_preserves_the_wrapped_signature(self) -> None:
        @untrusted_parse("image.decode")
        def parse(value: int, *, double: bool = False) -> int:
            """Docstring survives."""
            return value * 2 if double else value

        self.assertEqual(parse.__name__, "parse")
        self.assertEqual(parse.__doc__, "Docstring survives.")
        with self.assertRaises(UnsandboxedParseError):
            parse(3)
        with allow_untrusted_parse("test"):
            self.assertEqual(parse(3, double=True), 6)

    @override_settings(UL_PROCESS_ROLE="a-role-this-version-has-never-heard-of")
    def test_unknown_role_is_not_the_sandbox(self) -> None:
        # Degrading an unrecognised role to "some other container" keeps a new
        # compose value from crashing the container; degrading it to *sandbox*
        # would silently disable the boundary, which is the failure that matters.
        self.assertIs(current_role(), ProcessRole.UNSPECIFIED)
        with self.assertRaises(UnsandboxedParseError):
            check_untrusted_parse("image.decode")

    @override_settings(UL_UNTRUSTED_PARSE_POLICY="nonsense")
    def test_unknown_policy_falls_back_to_warn(self) -> None:
        self.assertIs(current_policy(), UntrustedParsePolicy.WARN)
        with self.assertLogs("urbanlens.dashboard.services.sandbox.guard", level="WARNING"):
            check_untrusted_parse("image.decode")


@override_settings(UL_UNTRUSTED_PARSE_POLICY="deny", UL_PROCESS_ROLE="web")
class DecoratedParserTests(SimpleTestCase):
    """The parsers themselves refuse to run outside the sandbox.

    Calling through the real functions rather than asserting on a decorator
    attribute: the thing worth knowing is that a view calling
    ``extract_exif_data`` fails, not that a marker is present.
    """

    def test_image_metadata_extraction_is_guarded(self) -> None:
        from urbanlens.dashboard.services.media.images import extract_exif_data, extract_gps_coords, extract_taken_at

        for func in (extract_exif_data, extract_gps_coords, extract_taken_at):
            with self.subTest(func=func.__name__), self.assertRaises(UnsandboxedParseError):
                func(object())  # type: ignore[arg-type]  # guard fires before the argument is touched

    def test_image_decode_is_guarded(self) -> None:
        from urbanlens.dashboard.services.media.images import downscale_stored_image, write_image_marker_thumbnail, write_image_thumbnail

        with self.assertRaises(UnsandboxedParseError):
            downscale_stored_image(object(), None, False)  # type: ignore[arg-type]
        for func in (write_image_thumbnail, write_image_marker_thumbnail):
            with self.subTest(func=func.__name__), self.assertRaises(UnsandboxedParseError):
                func(object())  # type: ignore[arg-type]

    def test_video_and_document_parsers_are_guarded(self) -> None:
        from urbanlens.dashboard.services.media.documents import convert_to_pdf, extract_pdf_text
        from urbanlens.dashboard.services.media.videos import probe_video

        with self.assertRaises(UnsandboxedParseError):
            probe_video("/nonexistent")
        for func in (convert_to_pdf, extract_pdf_text):
            with self.subTest(func=func.__name__), self.assertRaises(UnsandboxedParseError):
                func(object())  # type: ignore[arg-type]

    def test_byte_level_metadata_strip_is_not_guarded(self) -> None:
        # The deliberate exception, and the reason it exists: strip_metadata
        # walks container segments in pure Python and never decodes, which is
        # what lets an upload be scrubbed inside the request instead of sitting
        # in the media tree with its GPS intact until a worker gets to it.
        # Guarding it would push that scrub back onto the queue.
        from urbanlens.dashboard.services.media.metadata_strip import strip_metadata

        self.assertIsNone(strip_metadata(b"not an image"))


class SandboxQueueRoutingTests(SimpleTestCase):
    """Tasks declaring the sandbox queue are actually dispatched to it."""

    @override_settings(UL_SANDBOX_ENABLED=True)
    def test_queue_resolves_to_sandbox_when_a_worker_is_deployed(self) -> None:
        self.assertEqual(sandbox_queue(), Queue.SANDBOX)

    @override_settings(UL_SANDBOX_ENABLED=False)
    def test_queue_falls_back_to_default_without_a_sandbox_worker(self) -> None:
        # An install with no media-worker container must keep processing uploads
        # on the ordinary worker rather than filling a queue nothing drains.
        self.assertEqual(sandbox_queue(), Queue.DEFAULT)

    def test_declared_queue_reaches_apply_async(self) -> None:
        """``@shared_task(queue=...)`` puts the queue in the dispatched options.

        The load-bearing Celery behaviour. ``Task.apply_async`` merges
        ``_get_exec_options()`` (which reads ``Task.queue``) into the options it
        hands ``app.send_task``, so patching that boundary shows exactly what a
        real dispatch would carry - without needing a broker.
        """
        from celery import Celery, shared_task

        @shared_task(name="urbanlens.tests.sandbox_probe", queue=Queue.SANDBOX)
        def probe() -> None:
            """A task declaring the sandbox queue the same way the real ones do."""

        with patch.object(Celery, "send_task") as send_task:
            probe.apply_async()

        self.assertEqual(send_task.call_args.kwargs.get("queue"), Queue.SANDBOX)

    def test_every_untrusted_parse_task_declares_the_sandbox_queue(self) -> None:
        from urbanlens.dashboard import tasks

        routed = {task.name for task in (getattr(tasks, name) for name in dir(tasks)) if getattr(task, "queue", None) == tasks.SANDBOX_QUEUE and hasattr(task, "name")}
        self.assertEqual(routed, EXPECTED_SANDBOX_TASKS)

    def test_sandbox_queue_constant_is_resolved_once_at_import(self) -> None:
        # Under test settings UL_SANDBOX_ENABLED is False, so the constant is the
        # default queue - which is what keeps CELERY_TASK_ALWAYS_EAGER tests and
        # the memory:// broker working without a media-worker.
        from urbanlens.dashboard import tasks

        self.assertEqual(tasks.SANDBOX_QUEUE, Queue.DEFAULT)
