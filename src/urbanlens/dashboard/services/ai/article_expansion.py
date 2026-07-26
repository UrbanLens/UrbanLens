"""AI writing assistant that expands pin/wiki articles from a linked page.

Called as a post-step of link extraction: after structured fields are applied,
this module asks a writing model for **new plain-text paragraphs** grounded in
the page, strips all markup, runs the draft through
:mod:`article_safety`, and on approval appends the text to the pin article and
(when present) the location wiki article via :func:`save_article`.

Applies are append-only and non-destructive to existing article Markdown.
Failures at any writing/safety step are recorded as review-page result rows
and never raise out of :func:`expand_articles_from_page`.
"""

from __future__ import annotations

from html import unescape
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import nh3

from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.articles import get_article, save_article
from urbanlens.dashboard.services.rate_limiter import log_api_call
from urbanlens.dashboard.services.text_limits import MAX_ARTICLE_LENGTH

if TYPE_CHECKING:
    from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Match link-extraction's page-text budget so writing stays within token limits.
MAX_EXPANSION_PAGE_CHARS = 20_000
MAX_EXISTING_ARTICLE_CHARS = 12_000
MAX_NEW_TEXT_CHARS = 8_000
_RESULT_PREVIEW_CHARS = 200

EDIT_SUMMARY_EXPANDED_FROM_LINK = "Expanded from linked source"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MD_IMAGE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)]\([^)]*\)")
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
# Paired-marker emphasis stripping (keeps the enclosed text, drops only the
# markers) so stray, non-markdown uses of these characters - "~500 degrees",
# "Foo_Bar Mill", "5*3=15" - survive untouched. Order matters: longer/more
# specific markers first so "**bold**" isn't left as a mangled "*bold*".
_MD_STRIKETHROUGH = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_MD_ITALIC_UNDERSCORE = re.compile(r"(?<!\w)_(.+?)_(?!\w)", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LIST_MARKER = re.compile(r"(?m)^\s*[-*+]\s+")
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

_WRITING_INSTRUCTIONS = """You expand encyclopedia-style articles about physical places for an urban-exploration \
mapping community. You will be given a web page's visible text and any existing article text \
for the place. Write ONLY new plain-text paragraphs that add factual material from the page \
which is not already covered in the existing articles.

Rules:
- Output plain text only: no HTML, Markdown, bullet markers, headings, links, images, JSON, or code fences.
- Do not invent facts that are not stated on the page.
- Do not include entry methods, trespass advice, security-bypass details, or other operational access guidance.
- Do not include personal attacks, doxxing, or inappropriate content.
- If the page adds nothing useful beyond what the articles already say, respond with an empty ANSWER.

Wrap the entire plain-text draft (or nothing) in ANSWER tags:
<ANSWER>
paragraph one

paragraph two
</ANSWER>"""


def sanitize_article_plain_text(raw: str | None) -> str:
    """Force AI/writing output down to bounded plain text with no markup.

    Strips HTML (nh3 with an empty tag allowlist), code fences, Markdown
    link/image/heading/emphasis markers, and control characters. Paragraph
    breaks (blank lines) are preserved.

    Args:
        raw: Untrusted model output (may already have ANSWER tags stripped).

    Returns:
        Sanitized plain text, or ``""`` when nothing usable remains.
    """
    if not raw or not isinstance(raw, str):
        return ""

    text = raw.strip()
    if text.upper() in {"NONE", "NULL", "N/A", "EMPTY"}:
        return ""

    text = _CODE_FENCE.sub(" ", text)
    # Decode first, then sanitize HTML.  Doing this in the opposite order lets
    # entity-encoded markup (``&lt;script&gt;``) turn back into live markup
    # after the HTML sanitizer has already run.  A second pass is cheap
    # defense-in-depth for nested/double-encoded model output.
    text = nh3.clean(unescape(text), tags=set())
    text = unescape(text)
    text = nh3.clean(text, tags=set())
    text = _MD_IMAGE.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_LIST_MARKER.sub("", text)
    text = _MD_STRIKETHROUGH.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC_STAR.sub(r"\1", text)
    text = _MD_ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WHITESPACE_RUN.sub(" ", line).strip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n", text).strip()
    return text[:MAX_NEW_TEXT_CHARS]


def _slice_existing(content: str) -> str:
    """Bound existing article text handed to the writing model."""
    content = (content or "").strip()
    if len(content) <= MAX_EXISTING_ARTICLE_CHARS:
        return content
    return content[:MAX_EXISTING_ARTICLE_CHARS] + "\n…"


def _preview(text: str) -> str:
    """Short display string for the review-page result row."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= _RESULT_PREVIEW_CHARS:
        return collapsed
    return collapsed[: _RESULT_PREVIEW_CHARS - 1] + "…"


def _result_row(*, key: str, label: str, value: str, applied: bool, note: str) -> dict[str, Any]:
    """Build one LinkExtraction.results row for an article target."""
    return {"key": key, "label": label, "value": value, "applied": applied, "note": note}


def append_to_article(
    *,
    editor,
    existing: str,
    new_text: str,
    pin: Pin | None = None,
    wiki: Wiki | None = None,
    edit_summary: str = EDIT_SUMMARY_EXPANDED_FROM_LINK,
) -> tuple[bool, str]:
    """Append sanitized text to a host article when room remains.

    Shared by every caller that appends AI-drafted plain text to a pin/wiki
    article (this module's own link-based expansion, and
    ``services.trivia.wiki_incorporation``'s upvoted-question incorporation)
    so the dedupe/length-budget/persistence rules stay identical regardless
    of where the text came from.

    Args:
        editor: Profile credited on the revision.
        existing: Current Markdown source (may be empty).
        new_text: Sanitized plain text to append.
        pin: Pin host (mutually exclusive with ``wiki``).
        wiki: Wiki host.
        edit_summary: One-line revision summary.

    Returns:
        ``(applied, note)`` describing the outcome.
    """
    # Treat every boundary as untrusted.  The caller already sanitized before
    # moderation, but sanitizing again immediately before persistence prevents
    # a future caller from accidentally bypassing the plain-text guarantee.
    new_text = sanitize_article_plain_text(new_text)
    if not new_text:
        return False, "Nothing usable remained after sanitizing"

    if new_text in existing:
        return False, "Already present in the article"

    if existing.strip():
        combined = f"{existing.rstrip()}\n\n{new_text}"
    else:
        combined = new_text

    if len(combined) > MAX_ARTICLE_LENGTH:
        room = MAX_ARTICLE_LENGTH - len(existing.rstrip()) - 2  # for \n\n
        if room < 40:
            return False, "Article is at the length limit"
        combined = f"{existing.rstrip()}\n\n{new_text[:room].rstrip()}"

    _article, revision = save_article(
        editor=editor,
        content=combined,
        edit_summary=edit_summary,
        pin=pin,
        wiki=wiki,
    )
    if revision is None:
        return False, "No change after save"
    return True, "Appended to article"


def _known_facts_block(wiki: Wiki) -> str:
    """Render this wiki's trusted Facts as a short bullet list for the writing prompt.

    Handing the model our already-confirmed data (rather than only the raw
    page text) lets it defer to what we're confident about instead of
    re-guessing or restating a conflicting value from the linked source. See
    ``services.facts.consumption.get_trusted_facts``.
    """
    from urbanlens.dashboard.models.facts import registry
    from urbanlens.dashboard.services.facts.consumption import get_trusted_facts

    facts = get_trusted_facts(wiki=wiki)
    if not facts:
        return ""

    lines = []
    for fact in facts:
        definition = registry.get_definition(fact.key)
        label = definition.display_name if definition else fact.key
        lines.append(f"- {label}: {fact.get_value()}")
    return "\n".join(lines)


def _draft_new_paragraphs(
    *,
    place_name: str,
    page_text: str,
    pin_article: str,
    wiki_article: str | None,
    wiki: Wiki | None,
    profile,
) -> str | None:
    """Call the writing gateway; return raw answer text or None on failure."""
    from urbanlens.dashboard.services.ai.factory import get_gateway

    gateway = get_gateway(
        feature="article_expansion",
        profile=profile,
        instructions=_WRITING_INSTRUCTIONS,
    )
    if gateway is None:
        return None

    parts = [
        f"The place is known as: {place_name!r}.",
        "",
        "Existing pin article:",
        wrap_user_data(_slice_existing(pin_article) or "(empty)"),
        "",
    ]
    if wiki_article is not None:
        parts.extend(
            [
                "Existing wiki article:",
                wrap_user_data(_slice_existing(wiki_article) or "(empty)"),
                "",
            ]
        )
    if wiki is not None:
        facts_block = _known_facts_block(wiki)
        if facts_block:
            parts.extend(
                [
                    "Known facts (trust these over conflicting page content):",
                    wrap_user_data(facts_block),
                    "",
                ]
            )
    parts.extend(
        [
            "Page content:",
            wrap_user_data(page_text[:MAX_EXPANSION_PAGE_CHARS]),
        ]
    )
    prompt = "\n".join(parts)

    started = time.monotonic()
    try:
        answer = gateway.send_prompt(prompt)
    except Exception:
        logger.exception("Article expansion writing call failed")
        log_api_call("article_expansion", success=False)
        return None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log_api_call(
        "article_expansion",
        success=answer is not None,
        response_ms=elapsed_ms,
        endpoint=gateway.model,
        cost_estimate=gateway.cost,
    )
    return answer


def expand_articles_from_page(extraction: LinkExtraction, page_text: str) -> list[dict[str, Any]]:
    """Draft, sanitize, safety-check, and append article text from a page.

    Args:
        extraction: The in-flight link-extraction run (pin + profile loaded).
        page_text: Visible text already fetched for field extraction.

    Returns:
        Zero or more result rows for ``LinkExtraction.results``. Returns ``[]``
        when article expansion or safety features are disabled (caller should
        treat that as a silent skip). Never raises.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings
    from urbanlens.dashboard.services.ai.article_safety import classify_article_text
    from urbanlens.dashboard.services.ai.factory import get_gateway

    try:
        site = SiteSettings.get_current()
        if not site.ai_article_expansion_enabled or not site.ai_article_safety_enabled:
            return []

        # Probe both feature gates up front so a disabled safety toggle cannot
        # be bypassed by a writing-only path, and so we don't spend a write call
        # when safety is off.
        if get_gateway(feature="article_expansion", profile=extraction.profile) is None:
            return []
        if get_gateway(feature="article_safety", profile=extraction.profile) is None:
            return []

        pin = extraction.pin
        place_name = pin.effective_name or "Unknown place"
        pin_article_obj = get_article(pin=pin)
        pin_existing = pin_article_obj.content if pin_article_obj else ""

        wiki = None
        wiki_existing: str | None = None
        location = getattr(pin, "location", None)
        if location is not None:
            wiki = getattr(location, "wiki", None)
            if wiki is not None:
                wiki_article_obj = get_article(wiki=wiki)
                wiki_existing = wiki_article_obj.content if wiki_article_obj else ""

        raw = _draft_new_paragraphs(
            place_name=place_name,
            page_text=page_text or "",
            pin_article=pin_existing,
            wiki_article=wiki_existing,
            wiki=wiki,
            profile=extraction.profile,
        )
        if raw is None:
            return [
                _result_row(
                    key="article_pin",
                    label="Pin article",
                    value="",
                    applied=False,
                    note="Writing assistant unavailable",
                )
            ]

        new_text = sanitize_article_plain_text(raw)
        if not new_text:
            return [
                _result_row(
                    key="article_pin",
                    label="Pin article",
                    value="",
                    applied=False,
                    note="Nothing new to add from the page",
                )
            ]

        verdict = classify_article_text(new_text, place_name=place_name, profile=extraction.profile)
        preview = _preview(new_text)
        if not verdict.approved:
            reason = verdict.reason or "rejected"
            note = f"Safety review rejected ({reason})"
            rows = [
                _result_row(key="article_pin", label="Pin article", value=preview, applied=False, note=note),
            ]
            if wiki is not None:
                rows.append(
                    _result_row(key="article_wiki", label="Wiki article", value=preview, applied=False, note=note)
                )
            return rows

        rows = []
        applied, note = append_to_article(
            editor=extraction.profile,
            existing=pin_existing,
            new_text=new_text,
            pin=pin,
        )
        rows.append(_result_row(key="article_pin", label="Pin article", value=preview, applied=applied, note=note))

        if wiki is not None:
            applied_w, note_w = append_to_article(
                editor=extraction.profile,
                existing=wiki_existing or "",
                new_text=new_text,
                wiki=wiki,
            )
            rows.append(
                _result_row(key="article_wiki", label="Wiki article", value=preview, applied=applied_w, note=note_w)
            )
        return rows
    except Exception:
        logger.exception("Article expansion failed for extraction %s", getattr(extraction, "pk", None))
        return [
            _result_row(
                key="article_pin",
                label="Pin article",
                value="",
                applied=False,
                note="Article expansion failed unexpectedly",
            )
        ]
