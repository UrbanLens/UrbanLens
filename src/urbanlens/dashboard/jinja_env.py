"""Environment for the Jinja2 template backend.

Templates stay on the Django engine until they are ported one at a time; this backend serves the ones that
have moved. A template lives in exactly one engine's directories, so the two never race to resolve a name.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.templatetags.static import static
from django.urls import reverse
from jinja2 import Environment
import jinjax

if TYPE_CHECKING:
    from jinja2 import BaseLoader


def environment(**options: Any) -> Environment:
    """Build the environment Django's Jinja2 backend renders through.

    Args:
        options: Backend options. Django supplies ``loader`` from the backend's ``DIRS``/``APP_DIRS``,
            plus ``autoescape``, ``auto_reload`` and ``undefined``.

    Returns:
        An Environment with ``static``/``url`` globals and a JinjaX catalog over each ``components``
        directory beside a template directory.
    """
    # Django's backend defaults this on; forcing it keeps escaping independent of who builds the environment.
    options["autoescape"] = True
    env = Environment(**options)  # noqa: S701 - autoescape forced on the line above
    env.globals.update(static=static, url=reverse)
    env.add_extension(jinjax.JinjaX)

    catalog = jinjax.Catalog(jinja_env=env)
    loader: BaseLoader = options["loader"]
    for directory in getattr(loader, "searchpath", []):
        components = Path(directory) / "components"
        if components.is_dir():
            catalog.add_folder(components)
    return env
