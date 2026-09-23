"""The project's ASGI HTTP handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import SyncToAsync, ThreadSensitiveContext
import django
from django.core.handlers.asgi import ASGIHandler

if TYPE_CHECKING:
    from types import TracebackType


class NonBlockingThreadSensitiveContext(ThreadSensitiveContext):
    """A ``ThreadSensitiveContext`` whose exit never joins the request's thread on the event loop.

    asgiref's exit waits for that thread. When the request was cancelled mid-flight the thread can be
    parked in ``async_to_sync`` waiting for this same loop, so the join never returns and the server
    stops answering every request. Shutting the executor down without waiting lets the thread finish
    its work item and exit on its own.
    """

    async def __aexit__(
        self,
        exc: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.token:
            return
        executor = SyncToAsync.context_to_thread_executor.pop(self, None)
        if executor:
            executor.shutdown(wait=False)
        SyncToAsync.thread_sensitive_context.reset(self.token)


class UrbanLensASGIHandler(ASGIHandler):
    """Django's ASGI handler, run inside a :class:`NonBlockingThreadSensitiveContext`."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            raise ValueError(f"Django can only handle ASGI/HTTP connections, not {scope['type']}.")
        async with NonBlockingThreadSensitiveContext():
            await self.handle(scope, receive, send)


def get_asgi_application() -> UrbanLensASGIHandler:
    """Set Django up and return the project's HTTP handler, as ``django.core.asgi`` does for its own.

    Returns:
        The ASGI callable for HTTP connections.
    """
    django.setup(set_prefix=False)
    return UrbanLensASGIHandler()
