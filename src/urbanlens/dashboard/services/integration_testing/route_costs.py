"""What each GET route costs, requested as a light account and as a heavy one.

A route whose query count stays flat can still fetch every row an account owns, and render all of it. Comparing rows,
bytes and wall time between the two accounts is what finds that.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
import statistics
import time
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, StreamingHttpResponse
from django.test import Client
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver

from urbanlens.dashboard.middleware import _SqlStats
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from django.http.response import HttpResponseBase

#: Namespaces whose routes are staff tooling or third-party flows, not pages a user loads.
SKIPPED_NAMESPACES = frozenset({"admin", "oauth2_provider", "djdt"})

#: Route prefixes skipped for the same reason.
SKIPPED_ROUTE_PREFIXES = ("admin/", "oauth/", "accounts/", "__debug__/", "health/", "media/")

#: A route named for an action rather than a page. A GET that does one of these is a bug of its own, not a cost.
SIDE_EFFECT_NAME = re.compile(
    r"(?:^|[:._/-])(?:logout|signout|delete|remove|revoke|disconnect|unlink|leave|cancel|block|unblock|unsubscribe|reset|destroy|purge|clear|dismiss|mark|accept|decline|reject|approve|verify|activate|deactivate|toggle|archive|restore|rotate|regenerate|impersonate)(?:$|[:._/-])",
    re.IGNORECASE,
)

#: How many times more the heavy account must cost, on any measure, for a route to be reported as growing.
GROWTH_FACTOR = 5

#: Floors below which growth is not worth reporting, whatever the ratio.
SLOW_MS = 250
MANY_ROWS = 1_000
LARGE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class RouteTarget:
    """A named route that answers GET."""

    name: str
    route: str
    params: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteCost:
    """One route, requested repeatedly as one account.

    Attributes:
        wall_ms: Every run's wall time, first run first.
        sql_n: Statements the first run issued.
        sql_rows: Rows the first run's statements returned or touched.
        sql_ms: Time the first run spent in the database.
        bytes: The first run's body size.
    """

    account: str
    name: str
    url: str
    status: int
    wall_ms: tuple[float, ...]
    sql_n: int
    sql_rows: int
    sql_ms: float
    bytes: int

    @property
    def median_ms(self) -> float:
        """The median wall time across runs."""
        return statistics.median(self.wall_ms)


@dataclass(slots=True)
class SweepResult:
    """Everything one sweep measured, and everything it could not."""

    runs: int
    accounts: dict[str, int] = field(default_factory=dict)
    costs: list[RouteCost] = field(default_factory=list)
    unfilled: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """The result as plain data."""
        return {"runs": self.runs, "accounts": self.accounts, "costs": [asdict(cost) for cost in self.costs], "unfilled": self.unfilled, "skipped": self.skipped}


def _walk(patterns: Iterable[URLPattern | URLResolver], route: str, params: tuple[str, ...], namespace: str, namespaces: tuple[str, ...]) -> Iterator[tuple[URLPattern, str, str, tuple[str, ...], tuple[str, ...]]]:
    for entry in patterns:
        regex = getattr(entry.pattern, "regex", None)
        own = tuple(regex.groupindex) if regex is not None else ()
        full_route = route + str(entry.pattern)
        if isinstance(entry, URLResolver):
            if entry.namespace:
                yield from _walk(entry.url_patterns, full_route, params + own, f"{namespace}{entry.namespace}:", (*namespaces, entry.namespace))
            else:
                yield from _walk(entry.url_patterns, full_route, params + own, namespace, namespaces)
        elif entry.name:
            yield entry, f"{namespace}{entry.name}", full_route, tuple(dict.fromkeys(params + own)), namespaces


def _answers_get(pattern: URLPattern) -> bool:
    callback = pattern.callback
    actions = getattr(callback, "actions", None)
    if isinstance(actions, dict):
        return "get" in actions
    view_class = getattr(callback, "view_class", None)
    if view_class is None:
        return True
    return "get" in getattr(view_class, "http_method_names", ()) and hasattr(view_class, "get")


def discover_routes(only: re.Pattern[str] | None = None, patterns: Iterable[URLPattern | URLResolver] | None = None) -> tuple[list[RouteTarget], list[RouteTarget]]:
    """Every named route that answers GET, split into those a sweep requests and those it must not.

    Args:
        only: Keep only routes whose name this matches.
        patterns: The URLconf to walk; the project's when omitted.

    Returns:
        The routes to request, and the routes skipped as staff tooling or as actions.
    """
    visited: list[RouteTarget] = []
    skipped: list[RouteTarget] = []
    seen: set[str] = set()
    for pattern, name, route, params, namespaces in _walk(get_resolver().url_patterns if patterns is None else patterns, "", (), "", ()):
        if name in seen or not _answers_get(pattern) or (only is not None and not only.search(name)):
            continue
        seen.add(name)
        target = RouteTarget(name=name, route=route, params=params)
        if SKIPPED_NAMESPACES.intersection(namespaces) or route.startswith(SKIPPED_ROUTE_PREFIXES) or SIDE_EFFECT_NAME.search(name):
            skipped.append(target)
        else:
            visited.append(target)
    return visited, skipped


def parameter_values(profile: Profile) -> dict[str, str | int]:
    """URL parameters filled from the account's own rows: its first pin, that pin's location, a friend, a tag.

    Args:
        profile: The account the sweep requests as.

    Returns:
        Values by URL parameter name. A parameter the account has nothing for is absent.
    """
    values: dict[str, str | int] = {"label_kind": "tag", "kind": "tag", "username": profile.user.username, "profile_id": profile.pk}
    pin = Pin.objects.filter(profile=profile).root_pins().select_related("location").order_by("pk").first()
    if pin is not None:
        values["pin_slug"] = pin.ensure_slug()
        pin.location.ensure_slug()
        values["location_slug"] = pin.location.slug
    friendship = Friendship.objects.filter(Q(from_profile=profile) | Q(to_profile=profile), status=FriendshipStatus.ACCEPTED).select_related("from_profile", "to_profile").order_by("pk").first()
    if friendship is None:
        values["profile_slug"] = profile.ensure_slug()
    else:
        friend = friendship.to_profile if friendship.from_profile_id == profile.pk else friendship.from_profile
        values["profile_slug"] = values["peer_slug"] = friend.ensure_slug()
    label = Label.objects.filter(profile=profile, kind="tag").order_by("pk").first()
    if label is not None:
        values["label_id"] = label.pk
    return values


def build_url(target: RouteTarget, values: Mapping[str, str | int]) -> str | None:
    """The route's URL, or None when a parameter has no value or the values do not fit its converters.

    Args:
        target: The route.
        values: Parameter values by name.

    Returns:
        A path, or None.
    """
    if any(param not in values for param in target.params):
        return None
    try:
        return reverse(target.name, kwargs={param: values[param] for param in target.params} or None)
    except NoReverseMatch:
        return None


def request_host() -> str:
    """A host this deployment accepts, for requests made in-process."""
    return next((host for host in settings.ALLOWED_HOSTS if host and "*" not in host and not host.startswith(".")), "localhost")


def signed_in_fetch(user: User, *, host: str) -> Callable[[str], HttpResponseBase]:
    """A GET as *user*, over the whole middleware stack, that answers a route's exception with a 500.

    Args:
        user: Who to sign in as.
        host: The Host header, one ``ALLOWED_HOSTS`` accepts.

    Returns:
        A function from path to response.
    """
    client = Client(raise_request_exception=False)
    client.force_login(user)

    def fetch(url: str) -> HttpResponseBase:
        return client.get(url, secure=True, headers={"host": host, "x-forwarded-proto": "https"})

    return fetch


def _body_bytes(response: HttpResponseBase) -> int:
    if isinstance(response, StreamingHttpResponse):
        return len(response.getvalue())
    if isinstance(response, HttpResponse):
        return len(response.content)
    return 0


def measure_route(fetch: Callable[[str], HttpResponseBase], url: str, *, account: str, name: str, runs: int) -> RouteCost:
    """Request *url* *runs* times, each inside a transaction that is rolled back.

    Args:
        fetch: Makes the request.
        url: The path.
        account: Label for the account *fetch* is signed in as.
        name: The route's name.
        runs: How many times to request it; at least one.

    Returns:
        The route's cost.

    Raises:
        ValueError: *runs* is less than one.
    """
    if runs < 1:
        raise ValueError(f"runs must be at least 1, not {runs}")
    stats, status, size, wall = _request_once(fetch, url)
    walls = [wall, *(_request_once(fetch, url)[3] for _ in range(runs - 1))]
    return RouteCost(account=account, name=name, url=url, status=status, wall_ms=tuple(walls), sql_n=stats.count, sql_rows=stats.rows, sql_ms=stats.ms, bytes=size)


def _request_once(fetch: Callable[[str], HttpResponseBase], url: str) -> tuple[_SqlStats, int, int, float]:
    stats = _SqlStats()
    with transaction.atomic():
        started = time.perf_counter()
        with connection.execute_wrapper(stats):
            response = fetch(url)
            size = _body_bytes(response)
        wall = (time.perf_counter() - started) * 1000
        transaction.set_rollback(True)
    return stats, response.status_code, size, wall


def population_extremes(username_prefix: str) -> tuple[User, User] | None:
    """The accounts under *username_prefix* holding the fewest and the most root pins.

    Args:
        username_prefix: Selects the accounts.

    Returns:
        ``(lightest, heaviest)``, or None when no such account holds a pin.
    """
    counts = list(Pin.objects.filter(profile__user__username__startswith=username_prefix).root_pins().values("profile__user").annotate(pins=Count("pk")).order_by("pins", "profile__user"))
    if not counts:
        return None
    return User.objects.get(pk=counts[0]["profile__user"]), User.objects.get(pk=counts[-1]["profile__user"])


def sweep(accounts: Sequence[tuple[str, User]], *, runs: int, host: str, only: re.Pattern[str] | None = None, progress: Callable[[str], object] | None = None) -> SweepResult:
    """Request every visitable route as each account.

    Args:
        accounts: ``(label, user)`` pairs.
        runs: Requests per route per account.
        host: The Host header.
        only: Keep only routes whose name this matches.
        progress: Receives a line per account.

    Returns:
        The measurements, with the routes skipped and those no URL could be built for.
    """
    visited, skipped = discover_routes(only)
    result = SweepResult(runs=runs, skipped=sorted(target.name for target in skipped))
    for label, user in accounts:
        profile = Profile.objects.get(user=user)
        result.accounts[label] = Pin.objects.filter(profile=profile).root_pins().count()
        values = parameter_values(profile)
        fetch = signed_in_fetch(user, host=host)
        unfilled: list[str] = []
        for target in visited:
            url = build_url(target, values)
            if url is None:
                unfilled.append(target.name)
                continue
            result.costs.append(measure_route(fetch, url, account=label, name=target.name, runs=runs))
        result.unfilled[label] = sorted(unfilled)
        if progress is not None:
            progress(f"{label}: {len(visited) - len(unfilled)} routes measured, {len(unfilled)} without parameter values")
    return result


def grows(light: RouteCost, heavy: RouteCost) -> bool:
    """Whether the heavy account costs a route far more than the light one, on rows, time or bytes."""
    return (
        (heavy.sql_rows >= MANY_ROWS and heavy.sql_rows > GROWTH_FACTOR * max(light.sql_rows, 1))
        or (heavy.median_ms >= SLOW_MS and heavy.median_ms > GROWTH_FACTOR * light.median_ms)
        or (heavy.bytes >= LARGE_BYTES and heavy.bytes > GROWTH_FACTOR * max(light.bytes, 1))
    )


def render(result: SweepResult, *, light: str, heavy: str) -> str:
    """The sweep as Markdown, slowest route for the heavy account first.

    Args:
        result: The sweep.
        light: The light account's label.
        heavy: The heavy account's label.

    Returns:
        The report.
    """
    by_account: dict[str, dict[str, RouteCost]] = {}
    for cost in result.costs:
        by_account.setdefault(cost.account, {})[cost.name] = cost
    lights, heavies = by_account.get(light, {}), by_account.get(heavy, {})
    lines = [
        "# Route costs",
        "",
        f"{light}: {result.accounts.get(light, 0):,} pins. {heavy}: {result.accounts.get(heavy, 0):,} pins. Each cell is {light}/{heavy}. Median over {result.runs} runs; SQL, rows and size from the first run.",
        "",
        "| route | status | median ms | first ms | SQL | rows | KB | |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, cost in sorted(heavies.items(), key=lambda item: item[1].median_ms, reverse=True):
        other = lights.get(name)
        flag = "**grows**" if other is not None and grows(other, cost) else ""

        def pair(measure: Callable[[RouteCost], float], cost: RouteCost = cost, other: RouteCost | None = other) -> str:
            return f"{'-' if other is None else f'{measure(other):,.0f}'}/{measure(cost):,.0f}"

        lines.append(
            f"| {name} | {pair(lambda c: c.status)} | {pair(lambda c: c.median_ms)} | {pair(lambda c: c.wall_ms[0])} | {pair(lambda c: c.sql_n)} | {pair(lambda c: c.sql_rows)} | {pair(lambda c: c.bytes / 1024)} | {flag} |",
        )
    lines += ["", "## Not measured", "", f"- skipped as staff tooling or actions ({len(result.skipped)}): {', '.join(result.skipped)}"]
    lines += [f"- {label}, no value for a parameter ({len(names)}): {', '.join(names)}" for label, names in result.unfilled.items()]
    return "\n".join(lines) + "\n"
