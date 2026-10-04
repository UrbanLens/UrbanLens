"""Work every process must do once, done at boot instead of on request one.

Both servers need this and neither can share the other's mechanism: gunicorn
warms from `post_worker_init` in `gunicorn.conf.py`, daphne from `asgi.py` at
import. Keeping the *what* here means only the *when* differs between them -
the previous arrangement had one copy per server and they drifted, with daphne
having no copy at all until N22 H62.
"""

from __future__ import annotations

import gc
import sys


def warm_urlconf() -> tuple[int, int]:
    """Import the URLconf and build its reverse table.

    Two separate pieces of lazy work, and warming only the first is what H63
    was. Reading ``url_patterns`` imports the URLconf module, which reaches
    every controller and through them GeoPandas/Shapely - 1.4-2.3s of CPU.
    Django then builds the *reverse* table separately, on the first ``reverse()``
    or ``{% url %}``, by normalising every pattern's regex: a further 0.364s,
    landing on whichever request first rendered a template. Since practically
    every template opens with a ``{% url %}``, that was every process's first
    request, forever.

    Both numbers are returned so the caller can log them. That is not
    decoration: a warm-up whose result is discarded cannot be told apart from
    one that silently did nothing, and the counts are the only proof in the boot
    log that each half ran.

    Returns:
        The number of root URL patterns, and the number of reversible names.
    """
    from django.urls import get_resolver

    resolver = get_resolver()
    patterns = resolver.url_patterns
    reversible = resolver.reverse_dict
    return len(patterns), len(reversible)


def freeze_warm_heap() -> int:
    """Keep the warmed heap out of every later full collection.

    A full collection walks every tracked object, and a warmed worker holds about 400,000 that live as long as the
    process: 175-207 ms of CPU per collection on chiron, holding the GIL for every thread (P207). Frozen, a collection
    walks only what requests left behind. A reference from a frozen object still keeps its target alive, and a cycle
    made after the freeze is still collected. What is lost is a cycle running through a frozen object that later
    becomes garbage: it is never collected, with everything it holds. Call it once per process, after a successful
    warm-up. A pytest process imports ``asgi.py`` in-process, so there it freezes nothing.

    Returns:
        How many objects were frozen, for the boot log.
    """
    if "pytest" in sys.modules:
        return 0
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()
