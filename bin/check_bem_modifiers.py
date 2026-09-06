#!/usr/bin/env python3
"""Fail when a template applies a BEM modifier that no CSS rule styles.

`class="card card--secondary"` where `.card` is styled and `.card--secondary` is
not renders as a plain card. Nothing errors, nothing logs, and the review reads
correctly - the modifier is spelled right and the base class it modifies really
does exist. The distinction the author wrote it for simply never appears.

Fifty of these were live when this check was written (see P36). The count had
been recorded as 45 against a table of 46 rows, measured by hand three weeks
earlier, and had drifted in both directions - one fixed, eight added, three that
were never this - which is the argument for measuring it here instead of
transcribing it into a document.

Three things this gets right that a whitespace tokenizer over `class="..."` does
not, each of which hid real findings from the hand count:

* **A class written flush against a tag.** `class="page-footer{% if x %}
  page-footer--map{% endif %}"` yields the token `page-footer{%` to a naive
  split. Tags that emit nothing are removed as separators; tags and `{{ }}` that
  *can* emit text poison the token they touch, so `btn--{{ variant }}` is
  skipped rather than reported as `btn--`.
* **A modifier used as a JavaScript hook.** `slide.querySelector(".sv-img--
  fallback")` is a selector, not a visual state, and needs no rule. Referenced
  from TypeScript or a template's own `<script>`, a modifier is exempt.
* **A stale compiled stylesheet.** The hand count measured `style.css`, which is
  a build artifact and was five days behind the `.scss` sources when this was
  written. This compiles the sources, and refuses to fall back to a stale
  artifact silently.

`_KNOWN_UNSTYLED` is the accepted set. Anything outside it fails, and an entry
that no longer reproduces fails too - so the list shrinks as they are fixed and
cannot quietly grow. Deleting the modifier from the template is as valid a
resolution as writing the rule; what is not valid is leaving a new one unrecorded.

Exits non-zero listing what changed. Safe to run by hand from the repo root.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

#: Where the stylesheets live, relative to the repo root.
_SASS_DIR = "src/urbanlens/dashboard/frontend/sass"

#: The stylesheet entry point, and the build artifact compiled from it.
_SASS_ENTRY = f"{_SASS_DIR}/style.scss"
_COMPILED_CSS = "src/urbanlens/dashboard/frontend/static/dashboard/style.css"

#: Where templates live, and where a modifier may be used as a selector rather
#: than a visual state - TypeScript, or a template's own `<script>` block.
_TEMPLATE_DIR = "src/urbanlens/dashboard/templates"
_TS_DIR = "src/urbanlens/dashboard/frontend/ts"

#: `{% %}` tags that render nothing. Removing one cannot join two class names or
#: split one, so it is replaced by a space. Every other tag - `{% include %}`,
#: `{% trans %}`, `{% cycle %}`, a custom one - may emit text into the attribute,
#: so it poisons the token it touches instead.
_SILENT_TAGS = frozenset(
    {
        "if",
        "elif",
        "else",
        "endif",
        "ifchanged",
        "endifchanged",
        "for",
        "empty",
        "endfor",
        "with",
        "endwith",
        "comment",
        "endcomment",
        "spaceless",
        "endspaceless",
        "block",
        "endblock",
        "load",
        "csrf_token",
        "verbatim",
        "endverbatim",
        "autoescape",
        "endautoescape",
    }
)

#: Marks a position where template output could appear, so the token containing
#: it is unknowable at rest and is skipped rather than guessed at.
_POISON = "\x00"

_CLASS_ATTR = re.compile(r"""\bclass\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""", re.DOTALL)
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
_DJ_TAG = re.compile(r"\{%\s*(\w+)[^%]*?%\}", re.DOTALL)
_DJ_VAR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_CLASS_NAME = re.compile(r"[A-Za-z_][\w-]*\Z")
_SELECTOR_CLASS = re.compile(r"\.(-?[_A-Za-z][\w-]*)")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: Where a selector list can end: the next `{` closes one, and `}`/`;` start one.
_DELIMITER = re.compile(r"[{};]")

#: Modifiers applied by a template that no rule styles, accepted for now. Each is
#: a visual state that does not render; see P36 for the triage. Sorted, so a
#: diff to this set reads as one line per change.
_KNOWN_UNSTYLED = frozenset(
    {
        "album-card--readonly",
        "assistant-msg--pending",
        "btn--sel",
        "btn--trigger",
        "btn-icon--primary",
        "card--primary",
        "card--secondary",
        "cf-value-input--reference",
        "cf-value-input--select",
        "cf-value-input--url",
        "comment-reply-btn--sm",
        "detail-item--abandoned",
        "detail-item--address",
        "detail-item--built",
        "detail-item--coordinates",
        "detail-item--last-active",
        "detail-item--official",
        "detail-item--place",
        "dm-composer-attachment-chip--map",
        "dm-composer-attachment-chip--share",
        "dm-conv-item--group",
        "dm-thread--group",
        "form-row--map",
        "form-row--maps",
        "form-row--message",
        "form-row--plan",
        "form-row--time",
        "form-row--title",
        "fp-cf-input--select",
        "fp-cf-input--text",
        "home-widget--stats",
        "inline-sub-form--pricing",
        "map-overlay-btn--cancel",
        "notif-item--friend-req",
        "notif-item__icon-wrap--pin_shared",
        "notif-item__icon-wrap--safety_ci_due",
        "notif-item__icon-wrap--visit_suggested",
        "org-bulk-btn--edit",
        "org-bulk-btn--merge",
        "page-footer--map",
        "page-onboarding--wiki",
        "trip-map-marker-num--ghost",
        "ul-game-hud__btn--focus",
        "ul-game-hud__group--lead",
        "visit-item--pending",
        "visit-list--pending",
        "visit-source--pending",
        "wiki-seed-list--aliases",
        "wiki-stat-row--composite",
        "wiki-stat-row--mine",
    }
)


def stylesheet(root: pathlib.Path) -> str:
    """Compile the Sass sources, or fall back to the artifact if it is current.

    Measuring the checked-in `style.css` is what the original hand count did, and
    it was five days stale at the time. A build artifact older than its sources
    cannot answer this question, so it is used only when it is at least as new.

    Args:
        root: The repository root.

    Returns:
        The compiled CSS text.

    Raises:
        RuntimeError: When Sass is unavailable and the artifact is stale, so
            there is no trustworthy stylesheet to measure.
    """
    sass = root / "node_modules" / "sass" / "sass.js"
    runner = next((exe for exe in ("bun", "node") if _which(exe)), None)
    if sass.exists() and runner:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "style.css"
            result = subprocess.run(
                [runner, str(sass), "--style=expanded", "--no-source-map", _SASS_ENTRY, str(out)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and out.exists():
                return out.read_text(encoding="utf-8")

    artifact = root / _COMPILED_CSS
    if not artifact.exists():
        raise RuntimeError(f"Sass could not run and {_COMPILED_CSS} does not exist; nothing to measure.")
    newest = max((path.stat().st_mtime for path in (root / _SASS_DIR).rglob("*.scss")), default=0.0)
    if artifact.stat().st_mtime < newest:
        raise RuntimeError(f"Sass could not run and {_COMPILED_CSS} is older than the .scss sources. Measuring it would report on a stylesheet nobody is serving - run `bun run sass` first.")
    return artifact.read_text(encoding="utf-8")


def _which(name: str) -> bool:
    """Whether an executable is on PATH.

    Args:
        name: The executable to look for.

    Returns:
        True when it can be run.
    """
    return subprocess.run(["which", name], capture_output=True, check=False).returncode == 0


def styled_classes(css: str) -> set[str]:
    """Collect every class name a rule in *css* selects.

    Whatever is between the previous `{`, `}` or `;` and the next `{` is a
    selector list, or an at-rule prelude if it starts with `@`. That holds
    regardless of formatting, which matters: the first version of this read
    lines and required each selector to end in `{`, so it worked on
    `--style=expanded` and found **zero** rules in `--style=compressed` - the
    style `bun run sass` actually writes. Every modifier would then have failed
    the "is the base styled" test, the check would have reported its entire
    accepted list as fixed, and following that instruction would have deleted
    the baseline and left it passing vacuously.

    Args:
        css: Compiled CSS, in any output style.

    Returns:
        Every class name appearing in a selector.
    """
    text = _BLOCK_COMMENT.sub(" ", css)
    styled: set[str] = set()
    start = 0
    for delimiter in _DELIMITER.finditer(text):
        if delimiter.group() == "{":
            selector = text[start : delimiter.start()].strip()
            if selector and not selector.startswith("@"):
                styled.update(_SELECTOR_CLASS.findall(selector))
        start = delimiter.end()
    return styled


def applied_classes(templates: dict[str, str]) -> dict[str, list[str]]:
    """Collect every class name a template applies, and where.

    Args:
        templates: Template path to contents.

    Returns:
        Class name to the paths applying it, in the order encountered.
    """
    applied: dict[str, list[str]] = {}
    for path in sorted(templates):
        for match in _CLASS_ATTR.finditer(templates[path]):
            value = _DJ_VAR.sub(_POISON, match.group("v"))
            value = _DJ_TAG.sub(lambda tag: " " if tag.group(1) in _SILENT_TAGS else _POISON, value)
            for token in value.split():
                if _POISON in token or not _CLASS_NAME.match(token):
                    continue
                paths = applied.setdefault(token, [])
                if path not in paths:
                    paths.append(path)
    return applied


def _selects(name: str, script: str) -> bool:
    """Whether *script* uses *name* to find an element rather than to render one.

    A plain substring test gets both directions wrong. It exempts `btn--sel`
    because `tag-dialog-btn--selected` contains it, and it exempts
    `trip-map-marker-num--ghost`, which appears in a script only inside a
    template literal that *builds* the markup - still a visual state, still
    unstyled. So a reference counts only in the two forms that actually look
    something up: as a `.class` in a selector string, or as a bare token passed
    to the classList/getElementsByClassName family.

    Args:
        name: The class name to look for.
        script: JavaScript or TypeScript source.

    Returns:
        True when the script selects on *name*.
    """
    escaped = re.escape(name)
    selector = re.compile(rf"\.{escaped}(?![\w-])")
    token = re.compile(rf"""(?:classList\s*\.\s*(?:add|remove|toggle|contains|replace)|getElementsByClassName)\s*\([^)]*['"`]{escaped}['"`]""")
    return bool(selector.search(script) or token.search(script))


def unstyled_modifiers(css: str, templates: dict[str, str], scripts: dict[str, str]) -> dict[str, list[str]]:
    """Find applied BEM modifiers whose block is styled and which are not.

    A modifier whose *base* has no rule either is not this check's business: that
    is a whole component with no styling, which is visible immediately.

    Args:
        css: Compiled CSS.
        templates: Template path to contents.
        scripts: Path to contents for anywhere a class may be used as a selector.

    Returns:
        Modifier name to the templates applying it.
    """
    styled = styled_classes(css)
    applied = applied_classes(templates)
    found: dict[str, list[str]] = {}
    for name, paths in applied.items():
        if "--" not in name or name in styled:
            continue
        if name.split("--", 1)[0] not in styled:
            continue
        if any(_selects(name, text) for text in scripts.values()):
            continue
        found[name] = paths
    return found


def main() -> int:
    """Report modifiers that render nothing, and drift in the accepted set."""
    root = pathlib.Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    tracked = [name for name in subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True, cwd=root).stdout.split("\0") if name]

    def read(names: list[str]) -> dict[str, str]:
        contents = {}
        for name in names:
            try:
                contents[name] = (root / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return contents

    templates = read([name for name in tracked if name.startswith(_TEMPLATE_DIR) and name.endswith(".html")])
    # Only a template's <script> content counts as script, not the whole file:
    # every modifier appears in the class attribute of the template applying it,
    # so reading these whole would exempt all of them from themselves.
    scripts = {path: "\n".join(_SCRIPT_BLOCK.findall(text)) for path, text in templates.items()}
    scripts.update(read([name for name in tracked if name.startswith(_TS_DIR) and name.endswith((".ts", ".tsx"))]))

    if not templates:
        print(f"No templates found under {_TEMPLATE_DIR}. This check would pass vacuously.")
        return 1

    try:
        css = stylesheet(root)
    except RuntimeError as error:
        print(error)
        return 1

    found = unstyled_modifiers(css, templates, scripts)
    added = sorted(set(found) - _KNOWN_UNSTYLED)
    gone = sorted(_KNOWN_UNSTYLED - set(found))
    if not added and not gone:
        return 0

    if added:
        print(f"BEM modifiers applied by a template that no rule styles ({len(added)} new):")
        for name in added:
            print(f"  {name}  <- {', '.join(path.removeprefix(_TEMPLATE_DIR + '/dashboard/') for path in found[name])}")
        print()
        print("The base class is styled, so each of these was written to create a visual distinction")
        print("that does not render. Write the rule, or delete the modifier from the template.")
    if gone:
        print(f"Accepted modifiers that no longer reproduce ({len(gone)}):")
        for name in gone:
            print(f"  {name}")
        print()
        print("Remove them from _KNOWN_UNSTYLED so the list keeps shrinking.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
