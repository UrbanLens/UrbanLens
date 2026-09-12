"""Read an nginx config as directives-in-blocks, rather than as text.

Every guard the repo had over these files was an `assertIn` against whole-file
text, which cannot see which block a directive landed in - so a `limit_conn_zone`
placed in `events{}` instead of `http{}` passed all three of them while nginx
refused to start (N22 H59).

`bin/check_nginx_config.sh` asks nginx itself, which is the complete answer;
this exists so the rules we know to look for are also enforced where Docker is
not available, which includes the test suite.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
NGINX_DIR = REPO_ROOT / "src" / "urbanlens" / "config" / "nginx"

# ngx_event_core_module plus ngx_event_openssl: the whole set nginx accepts in
# `events`. Anything else there belongs to `http` or `main`.
EVENTS_DIRECTIVES = frozenset(
    {
        "accept_mutex",
        "accept_mutex_delay",
        "debug_connection",
        "multi_accept",
        "ssl_engine",
        "use",
        "worker_aio_requests",
        "worker_connections",
    },
)

# Directives that declare a shared-memory zone or a named block. Each is
# `http`-only, and each is the kind of thing added late to solve a capacity
# problem - which is when it gets dropped into whichever block came first.
HTTP_ONLY_DIRECTIVES = frozenset(
    {"geo", "limit_conn_zone", "limit_req_zone", "log_format", "map", "proxy_cache_path", "split_clients", "upstream"}
)

_COMMENT = re.compile(r"#.*?$", re.MULTILINE)
_TOKEN = re.compile(r"[{};]|[^\s{};]+")


def directives_by_context(text: str) -> list[tuple[tuple[str, ...], str, int]]:
    """Parse an nginx config into the block each directive sits in.

    Args:
        text: The config file's contents.

    Returns:
        One ``(context, directive, line)`` triple per directive, where context
        is the chain of enclosing block names - ``()`` at the top level,
        ``("http", "server")`` inside a server block. A block's own name is
        reported in its parent's context, so ``http`` itself comes back as
        ``((), "http", n)``.
    """
    found: list[tuple[tuple[str, ...], str, int]] = []
    stack: list[str] = []
    pending: list[str] = []
    line = 1
    position = 0
    stripped = _COMMENT.sub("", text)
    for match in _TOKEN.finditer(stripped):
        line += stripped.count("\n", position, match.start())
        position = match.start()
        token = match.group()
        if token == "{":
            if pending:
                found.append((tuple(stack), pending[0], line))
                stack.append(pending[0])
            pending = []
        elif token == "}":
            if stack:
                stack.pop()
            pending = []
        elif token == ";":
            if pending:
                found.append((tuple(stack), pending[0], line))
            pending = []
        else:
            pending.append(token)
    return found


def misplaced_directives(text: str) -> list[tuple[str, tuple[str, ...], int]]:
    """Report directives nginx would refuse in the block they appear in.

    Only the contexts with a small closed directive set are judged, so this
    stays a statement about nginx rather than a re-implementation of it.

    Args:
        text: A whole nginx config file, one that opens its own ``http`` block.

    Returns:
        One ``(directive, context, line)`` triple per misplacement, empty when
        every directive sits somewhere nginx accepts.
    """
    wrong = []
    for context, name, line in directives_by_context(text):
        in_events = context == ("events",) and name not in EVENTS_DIRECTIVES
        stray_http_only = name in HTTP_ONLY_DIRECTIVES and context != ("http",)
        if in_events or stray_http_only:
            wrong.append((name, context, line))
    return wrong
