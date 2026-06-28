"""
Prompt injection detection and sanitization for untrusted content passed to LLMs.

Two entry points:
    scan(text, source)     -> ScanResult with risk score, match list, and sanitized text
    sanitize(text, source) -> str (clean version, shortcut for scan().sanitized)
    wrap_user_data(text)   -> str wrapped in <USER_DATA> delimiters with escape attempts stripped

Integrate at two levels:
  - Gateway level: call scan() on every user prompt before it reaches the model.
  - Construction level: call wrap_user_data() on each user-supplied field before
    embedding it in the prompt, so the model knows to treat it as inert data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re

logger = logging.getLogger(__name__)

_HIGH = "high"
_MED = "medium"

# Each entry: (compiled regex, human label, confidence tier)
# High-confidence = almost certainly an attack; medium = suspicious but can be legitimate.
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # ── Instruction override family ───────────────────────────────────────────
    (re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|your)\s+instructions?"), "instruction override", _HIGH),
    (re.compile(r"(?i)(?:disregard|forget|bypass|override)\s+(?:all\s+)?(?:previous|prior|above|your)?\s*instructions?"), "instruction override", _HIGH),

    # ── New-instructions injection ────────────────────────────────────────────
    (re.compile(r"(?im)(?:^|(?<=\.\s)|(?<=\n))new\s+instructions?\s*:"), "instruction injection", _HIGH),
    (re.compile(r"(?i)(?:here\s+are|these\s+are)\s+(?:your\s+)?new\s+instructions?"), "instruction injection", _HIGH),
    (re.compile(r"(?i)\byour\s+new\s+(?:task|goal|role|purpose|instructions?)\s+(?:is|are)\b"), "role override", _HIGH),

    # ── Jailbreak keywords ────────────────────────────────────────────────────
    (re.compile(r"(?i)\b(?:jailbreak|dan\s+mode|developer\s+mode|god\s+mode|unrestricted\s+mode|uncensored\s+mode)\b"), "jailbreak attempt", _HIGH),

    # ── System-prompt probing ─────────────────────────────────────────────────
    (re.compile(r"(?i)\b(?:reveal|repeat|print|output|show|display)\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?)"), "system prompt probe", _HIGH),
    (re.compile(r"(?i)what\s+(?:are|is)\s+your\s+(?:system\s+)?(?:prompt|instructions?)"), "system prompt probe", _HIGH),

    # ── Delimiter injection ───────────────────────────────────────────────────
    (re.compile(r"(?i)</?system>"), "delimiter injection", _HIGH),
    (re.compile(r"(?i)</?instructions?>"), "delimiter injection", _HIGH),
    (re.compile(r"(?i)</?prompt>"), "delimiter injection", _HIGH),
    (re.compile(r"(?im)^###\s*(?:system|instructions?|prompt)\b"), "delimiter injection", _HIGH),
    (re.compile(r"(?i)\[system\]"), "delimiter injection", _HIGH),
    (re.compile(r"(?i)<</?SYS>>"), "delimiter injection", _HIGH),

    # ── Role override ─────────────────────────────────────────────────────────
    (re.compile(r"(?i)\bpretend\s+(?:you\s+are|to\s+be)\b"), "role override", _HIGH),
    (re.compile(r"(?i)\byou\s+(?:are|must)\s+now\s+(?:act|be|behave|ignore)\b"), "role override", _HIGH),
    (re.compile(r"(?i)\bact\s+as\s+(?:an?\s+)?(?:ai|gpt|claude|llm|chatbot|uncensored\s+ai)\b"), "role override", _HIGH),

    # ── Medium confidence ─────────────────────────────────────────────────────
    (re.compile(r"(?i)\bdo\s+not\s+(?:follow|obey|adhere\s+to|comply\s+with)\s+(?:your|these|any)\s+(?:instruction|rule|guideline|constraint)"), "constraint bypass", _MED),
    (re.compile(r"(?i)\bsystem\s+prompt\b"), "system prompt mention", _MED),
    (re.compile(r"(?i)\bprompt\s+injection\b"), "self-referential", _MED),
    (re.compile(r"(?im)^\s*role\s*:\s*(?:system|developer|admin)\b"), "role injection", _MED),
]

# A single high-confidence match scores 0.4 → above this threshold we sanitize.
_SANITIZE_THRESHOLD = 0.3
_REPLACEMENT = "[CONTENT FILTERED]"

# Strip these tags when they appear inside user-supplied text so the <USER_DATA>
# wrapper cannot be escaped via closing tags.
_ESCAPE_PATTERN = re.compile(r"(?i)</?USER_DATA>")


@dataclass
class InjectionMatch:
    """A single pattern match found during scanning."""
    description: str
    matched_text: str
    confidence: str


@dataclass
class ScanResult:
    """Full result of a prompt-injection scan."""
    original: str
    sanitized: str
    is_suspicious: bool
    risk_score: float
    matches: list[InjectionMatch] = field(default_factory=list)
    source: str = "unknown"


def scan(text: str, source: str = "unknown") -> ScanResult:
    """
    Scan untrusted text for prompt injection patterns.

    Args:
        text: Content to check.
        source: Origin label used in log messages ("user", "web", or "unknown").

    Returns:
        ScanResult with risk_score in [0, 1], match list, and pre-sanitized text.
        When risk_score >= 0.3, sanitized replaces high-confidence matches with
        [CONTENT FILTERED]; otherwise sanitized == original.
    """
    if not text or not text.strip():
        return ScanResult(original=text, sanitized=text, is_suspicious=False, risk_score=0.0, source=source)

    matches: list[InjectionMatch] = []
    for pattern, description, confidence in _PATTERNS:
        for m in pattern.finditer(text):
            matches.append(InjectionMatch(description=description, matched_text=m.group(), confidence=confidence))

    high_count = sum(1 for m in matches if m.confidence == _HIGH)
    med_count = sum(1 for m in matches if m.confidence == _MED)
    risk_score = min(1.0, high_count * 0.4 + med_count * 0.1)

    if matches:
        summary = ", ".join(f"{m.description}({m.confidence})" for m in matches[:5])
        logger.warning(
            "Prompt injection detected [source=%s, risk=%.2f, matches=%d]: %s",
            source,
            risk_score,
            len(matches),
            summary,
        )

    sanitized = _apply_sanitization(text) if risk_score >= _SANITIZE_THRESHOLD else text

    return ScanResult(
        original=text,
        sanitized=sanitized,
        is_suspicious=bool(matches),
        risk_score=risk_score,
        matches=matches,
        source=source,
    )


def sanitize(text: str, source: str = "unknown") -> str:
    """Scan text and return the sanitized version with injections neutralized.

    Args:
        text: Content to sanitize.
        source: Origin label for logging.

    Returns:
        Text with high-confidence injection patterns replaced by [CONTENT FILTERED].
    """
    return scan(text, source=source).sanitized


def wrap_user_data(text: str) -> str:
    """Wrap user-supplied text in <USER_DATA> delimiters for the LLM context boundary.

    Any pre-existing <USER_DATA> / </USER_DATA> tags in the input are stripped first
    so that an attacker cannot escape the sandbox by injecting a closing tag.

    Args:
        text: Raw user-supplied content (pin name, description, etc.).

    Returns:
        Empty string if text is blank, otherwise the content wrapped in
        <USER_DATA>...</USER_DATA> tags.
    """
    if not text or not text.strip():
        return ""
    neutralized = _ESCAPE_PATTERN.sub("", text).strip()
    if not neutralized:
        return ""
    return f"<USER_DATA>\n{neutralized}\n</USER_DATA>"


def _apply_sanitization(text: str) -> str:
    """Replace all high-confidence injection patterns with the placeholder string."""
    result = text
    for pattern, _, confidence in _PATTERNS:
        if confidence == _HIGH:
            result = pattern.sub(_REPLACEMENT, result)
    return result
