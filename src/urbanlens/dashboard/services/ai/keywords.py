"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    keywords.py                                                                                        *
*        - Path:    /dashboard/services/ai/keywords.py                                                                 *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2026-05-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                                 *
*        - Copyright (c) 2026 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2026-05-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Ordered so that more specific multi-word patterns (e.g. "Fire Tower") appear before their
# single-word subsets (e.g. "Firehouse") to ensure the first matching category wins correctly.
# Patterns are word-boundary anchored and case-insensitive.
CATEGORY_PATTERNS: dict[str, list[str]] = {
    "Airport": [
        r"\bairport\b",
        r"\bairfield\b",
        r"\bairstrip\b",
        r"\baerdrome\b",
        r"\baéroport\b",
        r"\baérodrome\b",
        r"\baeroporto\b",
        r"\baeropuerto\b",
        r"\bflughafen\b",
    ],
    "Amusement Park": [
        r"\bamusement\s+park\b",
        r"\btheme\s+park\b",
        r"\bcarnival\b",
        r"\bfunfair\b",
        r"\bfun\s+fair\b",
        r"\bwater\s*park\b",
        r"\bparque\s+de\s+atracciones\b",
        r"\bparc\s+d.attractions\b",
    ],
    "Asylum": [
        r"\basylum\b",
        r"\bpsychiatric\b",
        r"\bpsych\s+ward\b",
        r"\bsanatorium\b",
        r"\bsanitarium\b",
        r"\blunatic\b",
        r"\bmadhouse\b",
        r"\bmanicomio\b",
        r"\bmental\s+health\b",
        r"\bmental\s+hospital\b",
    ],
    "Bank": [
        r"\bbank\b",
        r"\bbanque\b",
        r"\bbanco\b",
    ],
    "Bridge": [
        r"\bbridge\b",
        r"\bviaduct\b",
        r"\btrestle\b",
        r"\boverpass\b",
        # French/Spanish — short words kept last to avoid partial matches
        r"\bpont\b",
        r"\bpuente\b",
    ],
    "Bunker": [
        r"\bbunker\b",
        r"\bbomb\s+shelter\b",
        r"\bfallout\s+shelter\b",
        r"\bair\s+raid\s+shelter\b",
    ],
    "Cars": [
        r"\bjunkyard\b",
        r"\bsalvage\s+yard\b",
        r"\bscrapyard\b",
        r"\bauto\s+graveyard\b",
        r"\bcar\s+graveyard\b",
        r"\bwrecking\s+yard\b",
    ],
    "Castle": [
        r"\bcastle\b",
        r"\bchâteau\b",
        r"\bchateau\b",
        r"\bcastillo\b",
        r"\bschloss\b",
        r"\bfortress\b",
        r"\bcitadel\b",
        r"\bkeep\b",
        r"\bbastille\b",
    ],
    "Church": [
        r"\bchurch\b",
        r"\bcathedral\b",
        r"\bchapel\b",
        r"\bmonastery\b",
        r"\bconvent\b",
        r"\babbey\b",
        r"\bbasilica\b",
        r"\bsynagogue\b",
        r"\bmosque\b",
        r"\bminaret\b",
        r"\btemple\b",
        r"\bshrine\b",
        r"\bparish\b",
        r"\bpriory\b",
        r"\bfriary\b",
        r"\brectory\b",
        r"\biglesia\b",
        r"\béglis[e]?\b",
        r"\beglise\b",
        r"\bkirche\b",
        r"\bsantuario\b",
        r"\bsanctuary\b",
    ],
    "Fire Tower": [
        r"\bfire\s+tower\b",
        r"\bfire\s+lookout\b",
        r"\blookout\s+tower\b",
    ],
    "Firehouse": [
        r"\bfirehouse\b",
        r"\bfire\s+station\b",
        r"\bfire\s+hall\b",
        r"\bfire\s+house\b",
    ],
    "Funeral Home": [
        r"\bfuneral\s+home\b",
        r"\bfuneral\s+parlou?r\b",
        r"\bfuneral\s+chapel\b",
        r"\bchapel\s+of\s+rest\b",
        r"\bmortuary\b",
        r"\bcremator(?:y|ium)\b",
        r"\bmorgue\b",
    ],
    "Graveyard": [
        r"\bgraveyard\b",
        r"\bcemetery\b",
        r"\bcemeteries\b",
        r"\bburial\s+ground\b",
        r"\bmausoleum\b",
        r"\bnecropolis\b",
        r"\bchurchyard\b",
        r"\bcrypt\b",
        r"\btomb\b",
        r"\bcamposanto\b",
        r"\bcimetière\b",
        r"\bfriedhof\b",
    ],
    "Hospital": [
        r"\bhospital\b",
        r"\binfirmary\b",
        r"\bmedical\s+cent(?:er|re)\b",
        r"\bdispensary\b",
        r"\bhôpital\b",
        r"\bkrankenhaus\b",
    ],
    "Hotel": [
        r"\bhotel\b",
        r"\bmotel\b",
        r"\binn\b",
        r"\blodge\b",
        r"\bhostel\b",
        r"\bhôtel\b",
        r"\bposada\b",
    ],
    "House": [
        r"\bfarmhouse\b",
        r"\bcottage\b",
        r"\bbungalow\b",
    ],
    "Laboratory": [
        r"\blaborator(?:y|ies)\b",
        r"\bresearch\s+facilit(?:y|ies)\b",
        r"\bscience\s+cent(?:er|re)\b",
    ],
    "Library": [
        r"\blibrar(?:y|ies)\b",
        r"\bbibliothèque\b",
        r"\bbiblioteca\b",
        r"\bbücherei\b",
    ],
    "Lighthouse": [
        r"\blighthouse\b",
        r"\blight\s+station\b",
    ],
    "Mall": [
        r"\bshopping\s+(?:mall|cent(?:er|re)|plaza)\b",
        r"\bgalleria\b",
        r"\bshopping\s+arcade\b",
    ],
    "Mansion": [
        r"\bmansion\b",
        r"\bmanor\s+house\b",
        r"\bgrand\s+house\b",
    ],
    "Military Base": [
        r"\bmilitary\s+base\b",
        r"\barmy\s+base\b",
        r"\bnaval\s+base\b",
        r"\bair\s+force\s+base\b",
        r"\bmilitary\s+install",
        r"\bbarracks\b",
        r"\bcaserne\b",
    ],
    "Monument": [
        r"\bmonument\b",
        r"\bmemorial\b",
        r"\bobelisk\b",
        r"\bwar\s+memorial\b",
        r"\bstatue\b",
    ],
    "Police Station": [
        r"\bpolice\s+station\b",
        r"\bpolice\s+department\b",
        r"\bcommissariat\b",
        r"\bconstabular(?:y|ies)\b",
        r"\bprecinct\b",
        r"\bsheriff(?:'s)?\s+office\b",
        r"\bjailhouse\b",
    ],
    "Power Plant": [
        r"\bpower\s+plant\b",
        r"\bpower\s+station\b",
        r"\bgenerating\s+station\b",
        r"\bnuclear\s+plant\b",
        r"\bcoal\s+plant\b",
        r"\belectrical\s+station\b",
    ],
    "Prison": [
        r"\bprison\b",
        r"\bpenitentiar(?:y|ies)\b",
        r"\bcorrectional\s+facilit(?:y|ies)\b",
        r"\bdetention\s+cent(?:er|re)\b",
        r"\breformator(?:y|ies)\b",
        r"\bpenal\s+(?:colony|institution|farm)\b",
        r"\bjail\b",
        r"\bpénitencier\b",
    ],
    "Resort": [
        r"\bresort\b",
        r"\bretreat\b",
        r"\bvacation\s+(?:camp|club)\b",
        r"\bholid?ay\s+(?:camp|resort)\b",
    ],
    "Ruins": [
        r"\bruins?\b",
    ],
    "School": [
        r"\bhigh\s+school\b",
        r"\belementar(?:y|ies)\s+school\b",
        r"\bschool\b",
        r"\bacademy\b",
        r"\buniversity\b",
        r"\bcollege\b",
        r"\bseminar(?:y|ies)\b",
        r"\binstitute\b",
        r"\bdormi(?:tory|tories)\b",
        r"\bécole\b",
        r"\becole\b",
        r"\bescuela\b",
        r"\bschule\b",
    ],
    "Stadium": [
        r"\bstadium\b",
        r"\barena\b",
        r"\bcolosseum\b",
        r"\bamphitheat(?:er|re)\b",
        r"\bballpark\b",
        r"\bsports\s+complex\b",
        r"\bvelodrome\b",
    ],
    "Theater": [
        r"\btheat(?:er|re)\b",
        r"\bcinema\b",
        r"\bmovie\s+house\b",
        r"\bopera\s+house\b",
        r"\bplayhouse\b",
        r"\bauditorium\b",
        r"\bdrive-?in\b",
    ],
    "Traincar": [
        r"\btraincar\b",
        r"\btrain\s+car\b",
        r"\brailcar\b",
        r"\brail\s+car\b",
        r"\bboxcar\b",
        r"\bcaboose\b",
        r"\blocomotiv(?:e|es)\b",
    ],
    "Train Station": [
        r"\btrain\s+station\b",
        r"\brailway\s+station\b",
        r"\brailroad\s+station\b",
        r"\brail\s+station\b",
        r"\bterminus\b",
        r"\bdepot\b",
        r"\bestaci[oó]n\b",
        r"\bgare\b",
        r"\bbahnhof\b",
    ],
    "Tunnel": [
        r"\btunnel\b",
        r"\bunderpass\b",
    ],
    "Factory": [
        r"\bfactor(?:y|ies)\b",
        r"\bfoundr(?:y|ies)\b",
        r"\bbrewerk(?:y|ies)\b",
        r"\bdistiller(?:y|ies)\b",
        r"\bcanner(?:y|ies)\b",
        r"\btanner(?:y|ies)\b",
        r"\brefiner(?:y|ies)\b",
        r"\bmanufacturing\b",
        r"\busine\b",  # French for factory
        r"\bfábrica\b",
    ],
}

# Cache compiled patterns at module level to avoid recompilation on every call.
_COMPILED: dict[str, list[re.Pattern[str]]] | None = None


def _get_compiled() -> dict[str, list[re.Pattern[str]]]:
    """Return the lazily-compiled pattern dict, building it on first access."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = {
            category: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]
            for category, patterns in CATEGORY_PATTERNS.items()
        }
    return _COMPILED


def categorize_by_keywords(text: str) -> str | None:
    """
    Attempt to categorize a location using regex keyword matching.

    Iterates through known urbex categories in priority order and returns the first
    category whose patterns match the supplied text.  Only name and place_name fields
    should be passed in — avoid addresses, which produce too many false positives
    (e.g. "Church Street" → Church).

    Args:
        text: Combined location name / description text to search.

    Returns:
        The matched category name, or None if no pattern matched.
    """
    if not text:
        return None

    compiled = _get_compiled()
    for category, patterns in compiled.items():
        for pattern in patterns:
            if pattern.search(text):
                logger.debug("Keyword match for '%s' via pattern r'%s'", category, pattern.pattern)
                return category

    return None
