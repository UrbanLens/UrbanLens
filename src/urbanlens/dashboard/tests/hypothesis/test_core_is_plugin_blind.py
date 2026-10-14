"""
Core never names a feature that only ships as a private plugin.

Each entry below is one plugin the public tree does not carry, and the words that would betray it. Core source -
everything under ``src/urbanlens`` outside those plugins' own packages, tests included - must contain none of them,
so removing the plugins' directories leaves a tree that neither knows nor mentions them.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from django.test import SimpleTestCase

import urbanlens

PACKAGE_ROOT = Path(urbanlens.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent

#: Plugin package name -> what core must not say. Patterns are case-insensitive.
PRIVATE_PLUGINS: dict[str, tuple[str, ...]] = {
    "billing": (
        r"(?<![a-z])stripe(?![a-z])",
        r"pay[ -_]?what[ -_]?you[ -_]?want",
        r"(?<![a-z])pwyw(?![a-z])",
        r"(?<![a-z])pledg",
    ),
    # Dollar figures: what a call or a month costs. Call-count budgets ("billable", free tiers), vendor price levels and
    # property sale prices are not the site's money and stay.
    "costs": (
        r"cost_estimate",
        r"cost[ _]per[ _](?:call|user|supporter|thousand)",
        r"cost[ _]tracking",
        r"MODEL_COSTS",
        r"(?:Cost|Operating)Component|OperatingCost",
        r"public[ _]costs",
        r"(?<![a-z])costs? page",
        r"(?<![a-z])api[ _]spend",
        r"spend[ _]history",
        r"total_cost",
        r"running costs?",
        r"\$\d[\d,.]*(?: and \$\d[\d,.]*)? per [\d,]+",
    ),
    # Case-insensitive unless a pattern says otherwise: ``(?-i:...)`` for the word "AI" alone, which would otherwise
    # match inside "said" or "fair". Lookbehinds keep "llm" out of "hollmann" and "inference" out of "pin inference".
    "ai": (
        r"urbanlens[./]plugins[./](ai|assistant)(?![a-z])",
        r"urbanlens_ai",
        r"services[./]ai(?![a-z])",
        r"openai",
        r"anthropic",
        r"ollama",
        r"llava",
        r"tiktoken",
        r"(?<![a-z])llms?(?![a-z])",
        r"hosted[ _-]models?",
        r"(?<![a-z])(vision|language)[ _-]models?",
        r"machine[ -]learning",
        r"(?-i:\bML\b)",
        r"(?<![a-z])inference[ _-](service|server|tier|client|url|token|network)",
        r"prompt[ _-]?injection",
        r"link[ _-]?extraction",
        r"(?-i:\bAI\b)",
        r"(?-i:(?<![.A-Za-z0-9])AI_)",
        r"(?-i:(?<![A-Za-z0-9])ai[_-][a-z])",
        r"(?-i:\bAi[A-Z])",
        r"[\"']ai[\"']",
        r"SiteFeature\.AI",
    ),
    "assistant": (r"assistant",),
    "concealment": (r"conceal",),
    "abuse_prevention": (
        r"abuse[ _-]?prevention",
        r"reputation[ _-]?(ledger|events?|rules?|sweep|totals?)",
        r"(profile|score|recompute|sweep)_?reputation",
        r"MODERATED_REMOVAL",
        r"retract_events?",
        r"(?<![a-z])record_event(?![a-z])",
        r"weight_events_for_target",
    ),
    # "redata" is distinctive in any case (RedataGateway, UL_REDATA_*); a separated spelling needs a word boundary
    # before it, or "are data" and "store_data" would match.
    "redata": (r"redata", r"(?<![a-z])re[-_ ]data"),
}

#: Paths (relative to ``src/urbanlens``) that are not core source, or are generated.
NOT_CORE = (
    # The migration history predates the plugins; consolidating it is a separate step.
    "dashboard/migrations/",
    "dashboard/tests/hypothesis/test_core_is_plugin_blind.py",
    # ISO country and region codes ("AI" is Anguilla, "ML" is Mali).
    "dashboard/services/geo/data/",
)
#: Agent guidance is not part of the public tree either.
NOT_MIRRORED = ("AGENTS.md", "CLAUDE.md")
SUFFIXES = (".py", ".html", ".txt", ".ts", ".tsx", ".scss", ".json", ".toml", ".md")


def _core_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/urbanlens"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    owned = tuple(f"plugins/{name}/" for name in PRIVATE_PLUGINS)
    files = []
    for name in filter(None, tracked.split("\0")):
        relative = name.removeprefix("src/urbanlens/")
        if (
            relative.startswith(owned)
            or relative.startswith(NOT_CORE)
            or relative.endswith(NOT_MIRRORED)
            or not relative.endswith(SUFFIXES)
        ):
            continue
        path = REPO_ROOT / name
        if path.exists():
            files.append(path)
    return files


#: Repository files outside ``src/urbanlens`` that the public tree carries: deployment, tooling, the integration suite.
#: ``docs/`` is not among them, and nor are the private plugins' own deployment files or the private tooling and
#: Playwright specs.
INFRASTRUCTURE_NOT_PUBLIC = (
    "docs/",
    "src/",
    "tests/live_locations/",
    ".claude/",
    ".forgejo/",
    "docker-compose.ai.yml",
    ".env.ai-sample",
    "uv.lock",
    "bun.lock",
    "bin/forgejo_api.py",
    "bin/release.py",
    "bin/run_live_location_tests.sh",
    "bin/vendor_redata_schema.py",
    "tests/integration/lib/hrsh.ts",
    "tests/integration/specs/location/hrsh-",
)
#: Files that name a private plugin on purpose, and why. A new entry is a decision.
INFRASTRUCTURE_EXEMPT: dict[str, str] = {
    "bin/check_comment_hygiene.py": "its history rule lists the names of coding tools; it is tooling about comments, not a feature",
}
#: Files that name the inference service's package (``src/urbanlens_ai``, kept off the mirror). Each must still work
#: where the package is absent.
NAMES_THE_INFERENCE_PACKAGE = frozenset(
    {
        ".github/workflows/publish.yml",
        ".github/codeql/codeql-config.yml",
        ".github/codeql/codeql-config-local.yml",
    }
)
INFRASTRUCTURE_SUFFIXES = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".json",
    ".toml",
    ".md",
    ".yml",
    ".yaml",
    ".sh",
    ".env",
    "-sample",
    "Dockerfile",
)


def _infrastructure_files() -> list[Path]:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True).stdout.decode()
    files = []
    for name in filter(None, tracked.split("\0")):
        if (
            name.startswith(INFRASTRUCTURE_NOT_PUBLIC)
            or name in INFRASTRUCTURE_EXEMPT
            or name.endswith(NOT_MIRRORED)
            or not name.endswith(INFRASTRUCTURE_SUFFIXES)
        ):
            continue
        path = REPO_ROOT / name
        if path.exists():
            files.append(path)
    return files


class CoreIsPluginBlindTests(SimpleTestCase):
    def test_core_names_no_private_plugin(self) -> None:
        patterns = {plugin: re.compile("|".join(words), re.IGNORECASE) for plugin, words in PRIVATE_PLUGINS.items()}
        offenders: list[str] = []
        for path in _core_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for plugin, pattern in patterns.items():
                if match := pattern.search(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{line} names {plugin} ({match.group(0)!r})")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_deployment_and_tooling_name_no_private_plugin(self) -> None:
        """The compose file, Dockerfile, env samples, scripts and integration suite are public too."""
        patterns = {
            plugin: re.compile("|".join(word for word in words if word != r"urbanlens_ai"), re.IGNORECASE)
            for plugin, words in PRIVATE_PLUGINS.items()
        }
        offenders: list[str] = []
        for path in _infrastructure_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for plugin, pattern in patterns.items():
                if match := pattern.search(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line} names {plugin} ({match.group(0)!r})")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_only_known_files_name_the_inference_package(self) -> None:
        naming = {
            str(path.relative_to(REPO_ROOT))
            for path in _infrastructure_files()
            if "urbanlens_ai" in path.read_text(encoding="utf-8", errors="replace")
        }
        self.assertEqual(naming - NAMES_THE_INFERENCE_PACKAGE, set())
