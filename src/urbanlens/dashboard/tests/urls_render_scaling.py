"""Two views whose per-row cost is known, for exercising the render-time mixin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpResponse
from django.template import Context, Template
from django.urls import path

from urbanlens.dashboard.models.achievements.model import Achievement

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Fixed per-page work, standing in for a real page's chrome. Sized so the
#: baseline render is long enough to divide by without amplifying noise.
CHROME_ELEMENTS = 2000

#: Per-row work in the expensive view - the size of this project's own icon
#: picker, which is what put a full 1,249-button grid inside every row of the
#: achievement admin.
ICONS_PER_ROW = 1249

_CHROME = "".join(f'<span class="chrome">{index}</span>' for index in range(CHROME_ELEMENTS))

_CHEAP = Template(
    '{{ chrome }}<ul>{% for row in rows %}<li class="row"><span>{{ row.name }}</span><span>{{ row.threshold }}</span></li>{% endfor %}</ul>'
)

_EXPENSIVE = Template(
    '{{ chrome }}<ul>{% for row in rows %}<li class="row"><span>{{ row.name }}</span>'
    '{% for icon in icons %}<button type="button" data-icon="{{ icon }}">{{ icon }}</button>{% endfor %}'
    "</li>{% endfor %}</ul>",
)


def _render(template: Template, **extra: object) -> HttpResponse:
    """Render one of the two templates over every achievement.

    Args:
        template: The template to render. **extra: Extra context, i.e. the expensive view's icon list.

    Returns:
        The rendered page."""
    rows = list(Achievement.objects.order_by("pk"))
    return HttpResponse(template.render(Context({"chrome": _CHROME, "rows": rows, **extra})))


def cheap(request: HttpRequest) -> HttpResponse:
    """A row costs two spans - what a well-behaved list page looks like.

    Args:
        request: The HTTP request.

    Returns:
        The rendered page."""
    return _render(_CHEAP)


def expensive(request: HttpRequest) -> HttpResponse:
    """A row costs a whole icon grid - the defect the mixin exists to catch.

    Args:
        request: The HTTP request.

    Returns:
        The rendered page."""
    return _render(_EXPENSIVE, icons=[f"icon_{index}" for index in range(ICONS_PER_ROW)])


urlpatterns = [
    path("cheap/", cheap, name="render_scaling.cheap"),
    path("expensive/", expensive, name="render_scaling.expensive"),
]
