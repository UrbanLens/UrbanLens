"""Typed ``key:value`` search operators, and the tokenizer that finds them.

The search box accepts two registers at once. People type English
("photos in Poughkeepsie last March"), which
:mod:`~urbanlens.dashboard.services.global_search.parser` interprets
heuristically; and people type operators (``type:photo place:"Poughkeepsie,
NY" visited:2019-03``), which are exact. Operators are parsed first and win,
because they are unambiguous - the heuristics then work on whatever text is
left over.

Three rules shape everything here:

- **An unknown key is never an error.** ``foo:bar`` is searched as ordinary
  text and reported back as such. A search box that rejects input is a search
  box people stop using, and there is no way for someone to discover which
  keys exist by being refused.
- **Every operator declares itself** (:data:`OPERATORS`), so the vocabulary,
  the autocomplete list, and the help text are one source rather than three
  that drift.
- **An operator that cannot be answered says so.** Some fields are encrypted
  at rest and are not merely slow to search but silently unmatchable - an
  ``icontains`` against ciphertext returns nothing and raises nothing. Those
  carry :attr:`Operator.unsupported_reason` so the UI can explain the empty
  result rather than implying the user has none of the thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

#: Value kinds, which decide how a clause's value is interpreted downstream.
KIND_TEXT = "text"
KIND_DATE = "date"
KIND_ENUM = "enum"
KIND_USER = "user"
KIND_COUNT = "count"
KIND_PLACE = "place"


@dataclass(frozen=True, slots=True)
class Operator:
    """One recognized search operator.

    Attributes:
        key: Canonical keyword, as it appears in a query.
        kind: One of the ``KIND_*`` constants; decides value interpretation.
        summary: One-line description, shown in autocomplete and help.
        example: A complete example query using this operator.
        aliases: Other spellings that resolve to ``key``.
        choices: For ``KIND_ENUM``, the accepted values.
        unsupported_reason: Set when the operator is recognized but cannot
            currently be answered. Parsing still succeeds so the UI can
            explain *why* rather than returning a silently empty result.
    """

    key: str
    kind: str
    summary: str
    example: str
    aliases: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    unsupported_reason: str = ""


#: The operator vocabulary. Grouped by concern in source order; the grouping
#: is not load-bearing, but it is the order the help panel lists them in.
OPERATORS: tuple[Operator, ...] = (
    # -- What kind of thing -------------------------------------------------
    Operator("type", KIND_ENUM, "Limit to one kind of thing.", "type:photo waterfall", aliases=("is_a", "kind")),
    Operator(
        "has",
        KIND_ENUM,
        "Only things that have something attached.",
        "pins has:photos",
        choices=("photos", "comments", "visits", "notes", "labels", "links", "floorplan", "markup", "wiki", "coords", "checkins"),
    ),
    Operator(
        "is",
        KIND_ENUM,
        "Limit to a state.",
        "type:trip is:upcoming",
        choices=("visited", "unvisited", "upcoming", "past", "shared", "private", "public", "archived", "starred"),
    ),
    # -- When ---------------------------------------------------------------
    Operator("visited", KIND_DATE, "When you visited the place.", "visited:march", aliases=("visit",)),
    Operator("created", KIND_DATE, "When it was first added.", "created:this-week", aliases=("added",)),
    Operator("updated", KIND_DATE, "When it last changed.", "updated:2026", aliases=("edited", "modified")),
    Operator("viewed", KIND_DATE, "When you last opened it.", "viewed:today", aliases=("seen",)),
    Operator("taken", KIND_DATE, "When a photo was taken.", "type:photo taken:2019", aliases=("shot",)),
    Operator("starts", KIND_DATE, "When a trip begins.", "type:trip starts:>today", aliases=("start",)),
    Operator("ends", KIND_DATE, "When a trip finishes.", "type:trip ends:<today", aliases=("end",)),
    # -- Where --------------------------------------------------------------
    Operator("place", KIND_PLACE, "Inside a town, county, or country.", 'place:"Poughkeepsie, NY"', aliases=("city", "region")),
    Operator("near", KIND_TEXT, "Close to you, or to somewhere named.", "near:me", aliases=("around",)),
    # -- Attributes ---------------------------------------------------------
    Operator("label", KIND_TEXT, "Carries a label. Commas mean any of them.", "label:rooftop,tunnel", aliases=("tag", "labels")),
    Operator("status", KIND_ENUM, "A pin's status.", "status:demolished"),
    Operator("name", KIND_TEXT, "Match the name only, not the notes.", 'name:"boiler house"', aliases=("title",)),
    Operator("note", KIND_TEXT, "Match notes and descriptions only.", "note:asbestos", aliases=("notes", "description")),
    Operator("color", KIND_TEXT, "A photo's dominant colour.", "type:photo color:red", unsupported_reason="Photo colour is not analysed yet."),
    Operator(
        "contains",
        KIND_TEXT,
        "Something recognised in a photo.",
        "type:photo contains:person",
        unsupported_reason="Only photos processed by keyword detection can match, so coverage is partial.",
    ),
    # -- Who ----------------------------------------------------------------
    Operator("by", KIND_USER, "Who created it.", "type:wiki by:me", aliases=("author", "creator")),
    Operator("from", KIND_USER, "Who shared it with you.", "pins from:dana", aliases=("sharedby",)),
    Operator("with", KIND_USER, "Who came along.", "type:trip with:dana", aliases=("companion",)),
    # -- How much / ordering ------------------------------------------------
    Operator("visits", KIND_COUNT, "How many times you have been.", "pins visits:>5"),
    Operator(
        "sort",
        KIND_ENUM,
        "Order the results.",
        "pins sort:most-visited",
        choices=("relevance", "recent", "created", "updated", "visited", "most-visited", "nearest"),
    ),
)

#: Canonical key for every spelling, including aliases.
_BY_SPELLING: dict[str, Operator] = {}
for _operator in OPERATORS:
    _BY_SPELLING[_operator.key] = _operator
    for _alias in _operator.aliases:
        _BY_SPELLING[_alias] = _operator


def lookup(spelling: str) -> Operator | None:
    """The operator a spelling refers to, or None when it is not one.

    Args:
        spelling: A candidate key, as typed (case-insensitive).

    Returns:
        The matching :class:`Operator`, or None so the caller can fall back to
        treating the token as free text.
    """
    return _BY_SPELLING.get(spelling.strip().lower())


@dataclass(frozen=True, slots=True)
class Clause:
    """One ``key:value`` pair recovered from a query.

    Attributes:
        operator: The operator this clause invokes.
        values: The value split on commas. More than one means "any of these";
            comma-OR covers nearly all real disjunction without asking anyone
            to reason about boolean precedence.
        negated: Whether the clause was written with a leading ``-``.
        raw: The clause exactly as typed, for echoing back.
    """

    operator: Operator
    values: tuple[str, ...]
    negated: bool = False
    raw: str = ""

    @property
    def key(self) -> str:
        """The canonical operator key."""
        return self.operator.key

    @property
    def value(self) -> str:
        """The first value, for the common single-valued case."""
        return self.values[0] if self.values else ""


@dataclass(slots=True)
class OperatorScan:
    """The result of scanning a raw query for operators.

    Attributes:
        clauses: Recognized operator clauses, in the order they appeared.
        text: Everything that was not an operator, re-joined with single
            spaces, for the heuristic parser to work on.
        unknown_keys: Keys shaped like operators that are not in the
            vocabulary. Their text is left in ``text`` and also reported here,
            so the UI can say "``foo:`` isn't an operator - searched as text"
            instead of quietly doing something unexpected.
    """

    clauses: list[Clause] = field(default_factory=list)
    text: str = ""
    unknown_keys: list[str] = field(default_factory=list)

    def first(self, key: str) -> Clause | None:
        """The first non-negated clause for ``key``, or None."""
        for clause in self.clauses:
            if clause.key == key and not clause.negated:
                return clause
        return None

    def all_for(self, key: str) -> list[Clause]:
        """Every clause for ``key``, negated or not."""
        return [clause for clause in self.clauses if clause.key == key]


#: A ``key:value`` pair. The value is either a quoted string or an unquoted run
#: with no whitespace. A leading ``-`` negates.
_CLAUSE = re.compile(
    r"""
    (?P<negate>-)?
    (?P<key>[A-Za-z][A-Za-z0-9_-]*)
    :
    (?:
        "(?P<quoted>[^"]*)"
      | (?P<bare>[^\s,]+(?:,[^\s,]+)*)
    )
    """,
    re.VERBOSE,
)


def scan(raw: str) -> OperatorScan:
    """Pull every recognized operator out of a raw query string.

    Unrecognized ``key:value`` shapes are deliberately left in the free text
    rather than dropped: ``12:30`` and ``http://example.com`` are not operator
    syntax, and neither is a typo, and none of them should make a query fail.

    Args:
        raw: The query exactly as typed.

    Returns:
        An :class:`OperatorScan` holding the clauses, the leftover text, and
        any operator-shaped keys that were not recognized.
    """
    scan_result = OperatorScan()
    if not raw:
        return scan_result

    leftover: list[str] = []
    cursor = 0
    for match in _CLAUSE.finditer(raw):
        operator = lookup(match.group("key"))
        quoted = match.group("quoted")
        bare = match.group("bare")
        if operator is None:
            # Not a known operator: record the key (once) and leave the whole
            # span in the free text untouched.
            key = match.group("key").lower()
            if key not in scan_result.unknown_keys:
                scan_result.unknown_keys.append(key)
            continue
        leftover.append(raw[cursor : match.start()])
        cursor = match.end()

        rawvalue = quoted if quoted is not None else (bare or "")
        # A quoted value is one value even when it contains commas; an
        # unquoted one splits, which is what makes label:a,b mean "either".
        values = (rawvalue,) if quoted is not None else tuple(part for part in rawvalue.split(",") if part)
        scan_result.clauses.append(
            Clause(
                operator=operator,
                values=tuple(value.strip() for value in values if value.strip()),
                negated=bool(match.group("negate")),
                raw=match.group(0),
            ),
        )

    leftover.append(raw[cursor:])
    scan_result.text = " ".join(" ".join(leftover).split())
    # A clause whose value was empty (`label:`) carries no filter; keep it out
    # of the results rather than matching everything or nothing arbitrarily.
    scan_result.clauses = [clause for clause in scan_result.clauses if clause.values]
    return scan_result


def suggestions(prefix: str = "") -> list[Operator]:
    """Operators whose key or aliases start with ``prefix``.

    Args:
        prefix: What the user has typed so far, with or without a colon.

    Returns:
        Matching operators in vocabulary order, for autocomplete.
    """
    needle = prefix.strip().lower().rstrip(":")
    if not needle:
        return list(OPERATORS)
    return [operator for operator in OPERATORS if operator.key.startswith(needle) or any(alias.startswith(needle) for alias in operator.aliases)]
