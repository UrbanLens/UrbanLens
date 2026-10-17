#!/usr/bin/env python3
"""
Hold comments and docstrings to the repository's style, against a ratchet.

Rules:
    comment-block: more than ``MAX_BLOCK_LINES`` consecutive full-line comments.
    comment-density: a Python function with at least ``MIN_DENSE_COMMENTS``
        comment lines and more than one comment line per ``CODE_LINES_PER_COMMENT``
        code lines.
    history-reference: a comment or docstring that narrates the repository's
        past, cites a retired tracker id, or names who asked for something.
        The patterns are in ``HISTORY_PATTERNS``.
    docstring-restates-signature: an Args, Returns or Yields section in which
        every entry only repeats a parameter's name or a type the hints state.

A justified exception is marked where it is, on the reported line or the line
above it: ``hygiene: allow <rule>[, <rule>]`` (``history``, ``block``,
``density`` and ``restates`` stand for the full rule names), followed by the
reason, e.g. ``# hygiene: allow history - the county renamed the field then``.

Existing violations are recorded per rule and file in ``BASELINE``. A file may
not gain violations, and a file that loses some fails until the baseline is
lowered with ``--update-baseline``, which refuses to raise any rule's total, so
the totals only go down. A baseline entry for a file this checkout does not have is
ignored, so a tree that omits some files still passes.

Usage:
    bin/check_comment_hygiene.py                    whole tree (CI)
    bin/check_comment_hygiene.py FILE...            these files only
    bin/check_comment_hygiene.py --staged           staged files, or the whole tree if none are staged (pre-commit)
    bin/check_comment_hygiene.py --list [FILE...]   print every violation
    bin/check_comment_hygiene.py --update-baseline  record the current counts
"""

from __future__ import annotations

import argparse
import ast
import bisect
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import functools
import inspect
import io
import json
import operator
from pathlib import Path
import re
import subprocess
import sys
import tokenize
import tomllib

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "bin" / "comment_hygiene_baseline.json"
PLUGINS = "src/urbanlens/plugins/"
PLUGIN_BASELINE_NAME = "comment_hygiene_baseline.json"

PARALLEL_THRESHOLD = 64

MAX_BLOCK_LINES = 4
MIN_DENSE_COMMENTS = 4
CODE_LINES_PER_COMMENT = 3

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
RULES = ("comment-block", "comment-density", "history-reference", "docstring-restates-signature")
RULE_ALIASES = {"history": "history-reference", "block": "comment-block", "density": "comment-density", "restates": "docstring-restates-signature"}
ALLOW_MARKER = re.compile(r"hygiene:\s*allow\s+([a-z-]+(?:\s*,\s*[a-z-]+)*)")

HASH_SUFFIXES = (".py", ".sh", ".yml", ".yaml", ".toml", ".conf")
SLASH_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".scss")
EXCLUDED_PARTS = ("/docs/", "/migrations/", "/vendor/", "/node_modules/")

PRAGMA = re.compile(r"#\s*(noqa\b|type:\s*ignore|pragma:|pyright:|fmt:|nosec\b|pylint:|mypy:|ruff:|isort:|NOSONAR|codeql)", re.IGNORECASE)

# Each pattern says how the code came to be rather than what it does. Tracker ids and dates are
# checked separately, since each needs an exception list. Private patterns, which name people or tools,
# are loaded from PRIVATE_TERMS_PATH and checked first.
PRIVATE_TERMS_PATH = ROOT / "bin" / "comment_hygiene_private.toml"


@dataclass(frozen=True)
class HistoryPattern:
    """
    A label, the regex that flags it, and the path prefixes it does not apply to.
    """

    label: str
    pattern: re.Pattern[str]
    exempt_paths: tuple[str, ...] = ()


def load_private_terms(path: Path) -> tuple[tuple[HistoryPattern, ...], tuple[str, ...]]:
    """
    Private history patterns and dated-observation paths from the TOML file at *path*, or nothing when it is absent.

    Each ``[[history]]`` entry gives ``label`` and ``regex``, and may give ``flags`` (names from ``re``) and
    ``exempt_paths``. ``dated_observation_paths`` lists paths whose dates describe an external source.
    """
    if not path.is_file():
        return (), ()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    patterns = tuple(
        HistoryPattern(
            entry["label"],
            re.compile(entry["regex"], functools.reduce(operator.or_, (re.RegexFlag[name] for name in entry.get("flags", [])), re.NOFLAG)),
            tuple(entry.get("exempt_paths", [])),
        )
        for entry in data.get("history", [])
    )
    return patterns, tuple(data.get("dated_observation_paths", []))


PRIVATE_HISTORY, DATED_OBSERVATIONS = load_private_terms(PRIVATE_TERMS_PATH)
HISTORY_PATTERNS: tuple[HistoryPattern, ...] = (
    *PRIVATE_HISTORY,
    HistoryPattern(
        "cites a pull request or commit",
        re.compile(r"\b(?:PR|MR) ?#?\d+\b|\b(?:pull|merge) request #?\d+|\b(?:issue|ticket|bug) #\d+|(?<![\w/&])#\d{2,5}'s\b|\bcommit [0-9a-f]{7,40}\b"),
    ),
    HistoryPattern(
        "narrates a change",
        re.compile(
            r"\b(?:it|this|that|we|they|which|there) used to\b|\bused to be\b"
            r"|\b(?:before|until|since) (?:the|this|that) (?:fix|change|PR|commit|rewrite|refactor|regression)\b"
            r"|\b(?:the )?(?:old|previous) (?:code|implementation|behaviou?r|approach)\b"
            r"|\b(?:was|were) (?:fixed|reverted|introduced) (?:in|on|by)\b"
            r"|\bwe (?:found|discovered|noticed|learned|realised|realized|decided|tried|changed|fixed|removed|added|saw)\b"
            r"|\b(?:war stor(?:y|ies)|post-?mortem|historically|at the time of writing|as of this writing)\b",
            re.IGNORECASE,
        ),
    ),
)

# P-numbers and G-numbers are retired tracker ids; the open ones are listed in the problems table read by open_tracker_ids().
# P0 is a severity, and P256/P384/P521 are curves.
TRACKER_ID = re.compile(r"\b(P\d{1,4}|G\d{1,2}-\d{1,3})\b")
NOT_TRACKER_IDS = frozenset({"P0", "P256", "P384", "P521"})
OPEN_IDS_TABLE = re.compile(r"^\|\s*([PT]\d+)\s*\|", re.MULTILINE)

# The repository's first commit is from 2023, so an earlier date cannot be its history. A date beside
# one of these words, or in the place fixtures, dates an external source, which the code needs.
ISO_DATE = re.compile(r"(?<![`\"'\w])(20\d\d-[01]\d-[0-3]\d)(?!\w|\.\d)")
FIRST_HISTORY_DATE = "2023-01-01"
DATE_CONTEXT = re.compile(r"\b(?:version|release|api|aerial|imagery|survey|snapshot|dataset|vintage|retrieved|accessed|published|dated)\b[^.;]{0,24}$", re.IGNORECASE)

SECTION = re.compile(r"^\s*(Args|Arguments|Parameters|Params|Keyword Args|Keyword Arguments|Other Parameters|Returns|Return|Yields|Yield):\s*$", re.MULTILINE)
ARG_ENTRY = re.compile(r"^\*{0,2}(\w+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")
_FILLER_WORDS = """
    a an the of for to in on at by with from and or is are be being its it this that these those given
    value values object objects instance instances whether if flag true false none null optional list
    lists dict dicts dictionary dictionaries mapping set sets tuple tuples sequence iterable iterator
    str string strings int integer integers float number numbers bool boolean bytes any
"""
FILLER = frozenset(_FILLER_WORDS.split())
STAR_FILLER = frozenset(["additional", "extra", "keyword", "positional", "argument", "arguments", "args", "kwargs", "passed", "through", "forwarded"])


@dataclass(frozen=True)
class Violation:
    """
    One finding, at a 1-based line.
    """

    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


@dataclass
class Comment:
    """
    A comment's text without its marker, and whether it has a line to itself.
    """

    line: int
    text: str
    full_line: bool


# --------------------------------------------------------------------------
# collecting comments
# --------------------------------------------------------------------------


def in_scope(path: str) -> bool:
    """
    Whether this checker reads *path*, given relative to the repository root.
    """
    if any(part in f"/{path}" for part in EXCLUDED_PARTS):
        return False
    name = path.rsplit("/", 1)[-1]
    return path.endswith(HASH_SUFFIXES + SLASH_SUFFIXES) or name.startswith("Dockerfile")


def _outside_quotes(line: str, index: int) -> bool:
    before = line[:index]
    return before.count('"') % 2 == 0 and before.count("'") % 2 == 0 and before.count("`") % 2 == 0


def line_comments(source: str, marker: str) -> tuple[list[Comment], list[Comment]]:
    """
    Comments in a non-Python file, and the lines of its ``/* */`` blocks.

    A marker counts when it starts the line or follows whitespace outside a
    quoted string, so ``https://`` and ``"#fff"`` are not comments.
    """
    comments: list[Comment] = []
    block_lines: list[Comment] = []
    in_block = False
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if marker == "//" and (in_block or stripped.startswith("/*")):
            block_lines.append(Comment(number, stripped.lstrip("/*").rstrip("*/").strip(), full_line=True))
            in_block = not stripped.endswith("*/")
            continue
        if stripped.startswith(marker):
            if not (number == 1 and stripped.startswith("#!")):
                comments.append(Comment(number, stripped[len(marker) :].strip(), full_line=True))
            continue
        for match in re.finditer(re.escape(marker), line):
            index = match.start()
            if index > 0 and line[index - 1] in " \t" and _outside_quotes(line, index):
                comments.append(Comment(number, line[index + len(marker) :].strip(), full_line=False))
                break
    return comments, block_lines


def python_comments(tokens: list[tokenize.TokenInfo], lines: list[str]) -> list[Comment]:
    """
    Every ``#`` comment in a tokenized Python file except a shebang or coding line.
    """
    found: list[Comment] = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        number, column = token.start
        if number <= 2 and (token.string.startswith("#!") or "coding" in token.string):
            continue
        found.append(Comment(number, token.string[1:].strip(), full_line=lines[number - 1][:column].strip() == ""))
    return found


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def long_blocks(path: str, comments: list[Comment], *, header_line: int | None) -> list[Violation]:
    """
    Runs of full-line comments longer than ``MAX_BLOCK_LINES``.

    A pragma line ends a run. A run starting at *header_line* describes the
    file, the role a module docstring plays in Python, and is exempt.
    """
    runs: list[list[Comment]] = []
    for comment in comments:
        if not comment.full_line or PRAGMA.match(f"#{comment.text}"):
            runs.append([])
            continue
        if runs and runs[-1] and runs[-1][-1].line == comment.line - 1:
            runs[-1].append(comment)
        else:
            runs.append([comment])
    found = []
    for run in runs:
        if len(run) <= MAX_BLOCK_LINES:
            continue
        if run[0].line == header_line:
            continue
        found.append(Violation(path, run[0].line, "comment-block", f"{len(run)} comment lines (max {MAX_BLOCK_LINES})"))
    return found


def _span_lines(node: ast.AST) -> set[int]:
    return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))


def _nested_scopes(node: ast.AST) -> list[ast.AST]:
    """
    The functions and classes directly inside *node*, not descending into them.
    """
    found, stack = [], list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, SCOPES):
            found.append(child)
        else:
            stack.extend(ast.iter_child_nodes(child))
    return found


def _docstring_node(node: ast.AST) -> ast.Constant | None:
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return body[0].value
    return None


def dense_functions(path: str, functions: list[ast.FunctionDef | ast.AsyncFunctionDef], tokens: list[tokenize.TokenInfo], comments: list[Comment]) -> list[Violation]:
    """
    Functions whose own lines carry too many comments for the code they hold.

    A nested function or class is measured on its own, and a docstring is
    neither comment nor code.
    """
    code_lines: set[int] = set()
    for token in tokens:
        if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}:
            code_lines.update(range(token.start[0], token.end[0] + 1))
    comment_lines = sorted(c.line for c in comments if not PRAGMA.match(f"#{c.text}"))
    found = []
    for node in functions:
        start, end = bisect.bisect_left(comment_lines, node.lineno), bisect.bisect_right(comment_lines, node.end_lineno or node.lineno)
        if end - start < MIN_DENSE_COMMENTS:
            continue
        own = _span_lines(node)
        for child in _nested_scopes(node):
            own -= _span_lines(child)
        docstring = _docstring_node(node)
        if docstring is not None:
            own -= _span_lines(docstring)
        n_comments = len(own.intersection(comment_lines[start:end]))
        n_code = len(own & code_lines)
        if n_comments >= MIN_DENSE_COMMENTS and n_comments * CODE_LINES_PER_COMMENT > n_code:
            found.append(Violation(path, node.lineno, "comment-density", f"{node.name}: {n_comments} comment lines to {n_code} code lines"))
    return found


def open_tracker_ids(root: Path) -> frozenset[str]:
    """
    Tracker ids that the problems table still maps to an open issue.
    """
    try:
        return frozenset(OPEN_IDS_TABLE.findall((root / "docs" / "internal" / "PROBLEMS.md").read_text(encoding="utf-8")))
    except OSError:
        return frozenset()


def history_in(path: str, line: int, text: str, open_ids: frozenset[str]) -> Violation | None:
    """
    The first history reference in one line of comment or docstring text.
    """
    for entry in HISTORY_PATTERNS:
        if path.startswith(entry.exempt_paths):
            continue
        match = entry.pattern.search(text)
        if match:
            return Violation(path, line, "history-reference", f"{entry.label}: {match.group(0)!r}")
    for match in TRACKER_ID.finditer(text):
        if match.group(1) not in NOT_TRACKER_IDS and match.group(1) not in open_ids:
            return Violation(path, line, "history-reference", f"cites a retired tracker id: {match.group(1)!r}")
    for match in () if path.startswith(DATED_OBSERVATIONS) else ISO_DATE.finditer(text):
        if match.group(1) >= FIRST_HISTORY_DATE and not DATE_CONTEXT.search(text[: match.start()]):
            return Violation(path, line, "history-reference", f"dates an event: {match.group(1)!r}")
    return None


def _fold(word: str) -> str:
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    return word.removesuffix("s") if len(word) > 3 else word


def _words(text: str) -> set[str]:
    """
    Words in *text*, lower-cased, ``camelCase`` and ``snake_case`` split, plurals folded.
    """
    return {_fold(word) for word in re.findall(r"[A-Za-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text))}


def _identifier_words(text: str) -> set[str]:
    """
    ``_words``, plus each identifier whole, so ``QuerySet`` also covers "queryset".
    """
    return _words(text) | {_fold(name.replace("_", "")) for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)}


def _sections(docstring: str) -> list[tuple[str, int, list[str]]]:
    """
    Google-style sections as (header, line offset in the docstring, body lines).
    """
    raw = docstring.split("\n")
    first_offset = next((i for i, line in enumerate(raw) if line.strip()), 0)
    lines = inspect.cleandoc(docstring).splitlines()
    found = []
    index = 0
    while index < len(lines):
        match = SECTION.match(lines[index])
        if not match:
            index += 1
            continue
        start = index
        body: list[str] = []
        index += 1
        while index < len(lines) and (not lines[index].strip() or lines[index].startswith((" ", "\t"))):
            body.append(lines[index])
            index += 1
        while body and not body[-1].strip():
            body.pop()
        found.append((match.group(1), start + first_offset, body))
    return found


def _entries(header: str, body: list[str]) -> list[tuple[str, str]]:
    """
    (name, description) per entry; a Returns or Yields section is one entry with no name.
    """
    if not body:
        return []
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    if header in {"Returns", "Return", "Yields", "Yield"}:
        return [("", " ".join(line.strip() for line in body))]
    entries: list[tuple[str, str]] = []
    for line in body:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) == indent:
            match = ARG_ENTRY.match(line.strip())
            if not match:
                return []
            entries.append((match.group(1), f"{match.group(2) or ''} {match.group(3)}"))
        elif entries:
            entries[-1] = (entries[-1][0], f"{entries[-1][1]} {line.strip()}")
    return entries


def _annotation_words(annotation: ast.expr | None) -> set[str]:
    return set() if annotation is None else _identifier_words(ast.unparse(annotation))


def _restates(description: str, known: set[str], *, star: bool = False) -> bool:
    filler = FILLER | STAR_FILLER if star else FILLER
    return not (_words(description) - filler - known)


def restating_sections(path: str, functions: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> list[Violation]:
    """
    Args, Returns and Yields sections that add nothing to the signature.

    An Args entry restates when its description uses no word beyond its own
    name, its annotation and ``FILLER``; a Returns or Yields section, when it
    uses none beyond the return annotation. A section is flagged only when every
    entry in it restates.
    """
    found = []
    for node in functions:
        docstring = _docstring_node(node)
        if docstring is None or not SECTION.search(docstring.value):
            continue
        arguments = node.args
        params = {a.arg: (a, False) for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
        for star in (arguments.vararg, arguments.kwarg):
            if star is not None:
                params[star.arg] = (star, True)
        for header, offset, body in _sections(docstring.value):
            entries = _entries(header, body)
            if not entries:
                continue
            if header in {"Returns", "Return", "Yields", "Yield"}:
                if node.returns is None:
                    continue
                redundant = _restates(entries[0][1], _annotation_words(node.returns))
            else:
                redundant = True
                for name, description in entries:
                    param, star = params.get(name, (None, False))
                    known = _identifier_words(name) | (_annotation_words(param.annotation) if param is not None else set())
                    if not _restates(description, known, star=star):
                        redundant = False
                        break
            if redundant:
                found.append(Violation(path, docstring.lineno + offset, "docstring-restates-signature", f"{node.name}: {header} only repeats the signature"))
    return found


# --------------------------------------------------------------------------
# per file
# --------------------------------------------------------------------------


def check_python(path: str, source: str, open_ids: frozenset[str]) -> list[Violation]:
    """
    Every rule, on one Python file. A file that does not parse is left to ruff.
    """
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError):
        return []
    lines = source.splitlines()
    comments = python_comments(tokens, lines)
    scopes = [tree, *(node for node in ast.walk(tree) if isinstance(node, SCOPES))]
    functions = [node for node in scopes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    found = long_blocks(path, comments, header_line=None)
    found += dense_functions(path, functions, tokens, comments)
    found += restating_sections(path, functions)
    for comment in comments:
        violation = history_in(path, comment.line, comment.text, open_ids)
        if violation:
            found.append(violation)
    for node in scopes:
        docstring = _docstring_node(node)
        if docstring is None:
            continue
        for offset, text in enumerate(docstring.value.split("\n")):
            violation = history_in(path, docstring.lineno + offset, text, open_ids)
            if violation:
                found.append(violation)
    return found


def allowed_rules(source: str) -> dict[int, set[str]]:
    """
    The rules an ``hygiene: allow`` marker exempts, by the 1-based line it is on.
    """
    allowed: dict[int, set[str]] = {}
    for number, line in enumerate(source.splitlines(), 1):
        match = ALLOW_MARKER.search(line)
        if match:
            names = {name.strip() for name in match.group(1).split(",")}
            allowed[number] = {RULE_ALIASES.get(name, name) for name in names}
    return allowed


def check_file(path: str, source: str, open_ids: frozenset[str]) -> list[Violation]:
    """
    Every rule that applies to *path*'s language, less what a marker allows on or just above its line.
    """
    allowed = allowed_rules(source)
    found = _check_language(path, source, open_ids)
    if not allowed:
        return found
    return [v for v in found if v.rule not in allowed.get(v.line, set()) | allowed.get(v.line - 1, set())]


def _check_language(path: str, source: str, open_ids: frozenset[str]) -> list[Violation]:
    """
    Every rule that applies to *path*'s language.
    """
    if path.endswith(".py"):
        return check_python(path, source, open_ids)
    marker = "//" if path.endswith(SLASH_SUFFIXES) else "#"
    comments, block_lines = line_comments(source, marker)
    found = long_blocks(path, comments, header_line=2 if source.startswith("#!") else 1)
    for comment in [*comments, *block_lines]:
        violation = history_in(path, comment.line, comment.text, open_ids)
        if violation:
            found.append(violation)
    return found


def tracked_paths(root: Path) -> list[str]:
    """
    Every committed file this checker reads.
    """
    listing = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True)
    return [path for path in listing.stdout.splitlines() if in_scope(path)]


def scan(root: Path, paths: list[str]) -> list[Violation]:
    """
    Violations in *paths*, relative to *root*. Unreadable files are skipped.
    """
    open_ids = open_tracker_ids(root)
    jobs = [(root, path, open_ids) for path in paths]
    if len(jobs) < PARALLEL_THRESHOLD:
        results = [_check_one(job) for job in jobs]
    else:
        with ProcessPoolExecutor() as pool:
            results = list(pool.map(_check_one, jobs, chunksize=32))
    return sorted((v for result in results for v in result), key=lambda v: (v.path, v.line, v.rule))


def _check_one(job: tuple[Path, str, frozenset[str]]) -> list[Violation]:
    root, path, open_ids = job
    try:
        source = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return check_file(path, source, open_ids)


# --------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------


def counts(violations: list[Violation]) -> dict[str, dict[str, int]]:
    """
    Violation counts as ``{rule: {path: n}}``, with every rule present.
    """
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for violation in violations:
        table[violation.rule][violation.path] += 1
    return {rule: dict(sorted(table[rule].items())) for rule in RULES}


def _is_partial_export(root: Path) -> bool:
    """
    Whether *root* is a public export, which lacks .forgejo/ and the private files the baseline still records.
    """
    return not (root / ".forgejo").is_dir()


def baseline_file(path: str) -> Path:
    """
    The baseline that records *path*: its plugin's own, so a plugin's directory can be left out of a tree.
    """
    if path.startswith(PLUGINS) and "/" in path.removeprefix(PLUGINS):
        return ROOT / PLUGINS / path.removeprefix(PLUGINS).split("/", 1)[0] / PLUGIN_BASELINE_NAME
    return BASELINE


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    """
    The recorded counts; a missing file records none.
    """
    if not path.is_file():
        return {rule: {} for rule in RULES}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {rule: data.get(rule, {}) for rule in RULES}


def load_all_baselines() -> dict[str, dict[str, int]]:
    """
    The core baseline merged with every plugin's.
    """
    merged = load_baseline(BASELINE)
    for plugin_file in sorted((ROOT / PLUGINS).glob(f"*/{PLUGIN_BASELINE_NAME}")):
        for rule, files in load_baseline(plugin_file).items():
            merged[rule].update(files)
    return merged


def measured_baseline(baseline: dict[str, dict[str, int]], root: Path, tracked: list[str]) -> dict[str, dict[str, int]]:
    """
    *baseline* without the files this checkout does not have.

    A file the tree omits (a private file on the public mirror) would otherwise read as having lost all its violations.
    """
    present = {path for path in tracked if (root / path).is_file()}
    return {rule: {path: n for path, n in files.items() if path in present} for rule, files in baseline.items()}


def compare(current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]], paths: list[str] | None) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    """
    (rule, path, now, recorded) for each file that gained, then each that lost, violations.

    With *paths*, only those files are compared.
    """
    gained, lost = [], []
    for rule in RULES:
        files = set(current[rule]) | set(baseline[rule]) if paths is None else set(paths)
        for path in sorted(files):
            now, recorded = current[rule].get(path, 0), baseline[rule].get(path, 0)
            if now > recorded:
                gained.append((rule, path, now, recorded))
            elif now < recorded:
                lost.append((rule, path, now, recorded))
    return gained, lost


def update_baseline(
    current: dict[str, dict[str, int]],
    path: Path = BASELINE,
    *,
    allow_increase: bool,
    tracked: list[str] | None = None,
    root: Path = ROOT,
) -> int:
    """
    Write *current* as the baseline unless that raises a rule's total.

    The tree's own baseline is split: a plugin's files go to its own baseline file. Any other *path* takes everything.
    With *tracked*, totals are compared without the files *root* does not have, as ``main`` does, so the budget of an
    omitted file cannot absorb an increase in a present one. An omitted file's recorded counts are written back as they were.
    """
    split = path == BASELINE
    old = load_all_baselines() if split else load_baseline(path)
    omitted: dict[str, dict[str, int]] = {rule: {} for rule in RULES}
    if tracked is not None:
        measured = measured_baseline(old, root, tracked)
        # Only a checkout without .forgejo/ is a partial export; in the whole repository a file that is gone was deleted
        # or renamed, and its entry should go with it.
        partial = _is_partial_export(root)
        if partial:
            omitted = {rule: {file: n for file, n in old[rule].items() if file not in measured[rule]} for rule in RULES}
        old = measured
    raised = [rule for rule in RULES if sum(current[rule].values()) > sum(old[rule].values())]
    if raised and not allow_increase and path.is_file():
        for rule in raised:
            print(f"refusing: {rule} would rise from {sum(old[rule].values())} to {sum(current[rule].values())}")
        print("Fix the new violations, or pass --allow-increase if the maintainer has accepted them.")
        return 1

    def home(file: str) -> Path:
        return baseline_file(file) if split else path

    targets: dict[Path, dict[str, dict[str, int]]] = {path: {rule: {} for rule in RULES}}
    for rule in RULES:
        for file in {**old[rule], **omitted[rule], **current[rule]}:
            targets.setdefault(home(file), {rule_: {} for rule_ in RULES})
        for file, count in {**omitted[rule], **current[rule]}.items():
            targets[home(file)][rule][file] = count
    for target, table in targets.items():
        if target != path and not any(table.values()):
            target.unlink(missing_ok=True)
            continue
        target.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for rule in RULES:
        print(f"{rule}: {sum(old[rule].values())} -> {sum(current[rule].values())}")
    return 0


def _given_paths(files: list[str]) -> list[str]:
    paths = set()
    for name in files:
        resolved = Path(name).resolve()
        if resolved.is_file() and resolved.is_relative_to(ROOT) and in_scope(resolved.relative_to(ROOT).as_posix()):
            paths.add(resolved.relative_to(ROOT).as_posix())
    return sorted(paths)


def _staged_paths() -> list[str]:
    """
    Staged files that are added, copied, modified or renamed, relative to the repository root.
    """
    listing = subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--name-only", "--diff-filter=ACMR"], capture_output=True, text=True, check=True)
    return [name for name in listing.stdout.splitlines() if name]


def main(argv: list[str] | None = None) -> int:
    """
    Check the tree or the given files, list violations, or rewrite the baseline.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument("files", nargs="*", help="check only these files (default: every tracked file)")
    parser.add_argument("--staged", action="store_true", help="check the staged files, or every tracked file when none is staged")
    parser.add_argument("--list", action="store_true", help="print every violation, baselined or not")
    parser.add_argument("--update-baseline", action="store_true", help="record the current counts as the baseline")
    parser.add_argument("--allow-increase", action="store_true", help="with --update-baseline, accept a higher total")
    args = parser.parse_args(argv)

    files = args.files
    if args.staged:
        if args.files or args.update_baseline:
            print("--staged takes no file arguments and cannot be combined with --update-baseline.")
            return 2
        files = [str(ROOT / name) for name in _staged_paths()]
        if files and not _given_paths(files):
            return 0

    tracked = tracked_paths(ROOT)
    paths = _given_paths(files) if files else tracked
    violations = scan(ROOT, paths)
    current = counts(violations)

    if args.update_baseline:
        if files:
            print("--update-baseline reads the whole tree; drop the file arguments.")
            return 2
        return update_baseline(current, BASELINE, allow_increase=args.allow_increase, tracked=tracked)

    if args.list:
        for violation in violations:
            print(violation)
        for rule in RULES:
            print(f"{rule}: {sum(current[rule].values())}")
        return 0

    recorded = load_all_baselines()
    if _is_partial_export(ROOT):
        recorded = measured_baseline(recorded, ROOT, tracked)
    gained, lost = compare(current, recorded, paths if files else None)
    for rule, path, now, recorded in gained:
        print(f"{path}: {rule}: {now} (baseline {recorded})")
        for violation in violations:
            if violation.path == path and violation.rule == rule:
                print(f"  {violation}")
    if gained:
        print("\nNew comment or docstring violations: shorten or remove them.")
        print("If a file only moved, its count moves with it: run --update-baseline.")
    if lost:
        for rule, path, now, recorded in lost:
            print(f"{path}: {rule}: {now} (baseline {recorded})")
        changed = sorted({baseline_file(path).relative_to(ROOT).as_posix() for _, path, _, _ in lost})
        print("\nFewer violations than the baseline records. Lock that in:")
        print(f"  python3 bin/check_comment_hygiene.py --update-baseline && git add {' '.join(changed)}")
    return 1 if gained or lost else 0


if __name__ == "__main__":
    sys.exit(main())
