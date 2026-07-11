from __future__ import annotations

import functools
import logging
import re

logger = logging.getLogger(__name__)

# Ordered so that more specific multi-word patterns (e.g. "Fire Tower") beat their
# single-word subsets (e.g. "Firehouse") - first match wins.
#
# Separator convention:
#   [\s-]+  between words where a gap is always expected  ("fire station", "fire-station")
#   [\s-]*  where the gap is sometimes absent             ("traincar" / "train car" / "train-car")
#
# All patterns are compiled with re.IGNORECASE | re.UNICODE.
CATEGORY_PATTERNS: dict[str, list[str]] = {
    "Airport": [
        r"\bair(port|field|strip)\b",
        r"\ba[ée]ro?[\s-]*(p(o|ue)rto?|dr?ome)\b",
        r"\bflughafen\b",
    ],
    "Amusement Park": [
        r"\b(?:amusement|theme)[\s-]*park\b",
        r"\b(fun|renaissance)[\s-]*fair\b",
        r"\bcarnival\b",
        r"\bwater[\s-]*park\b",
        r"\bparque[\s-]+de[\s-]+atracciones\b",
        r"\bparc[\s-]+d.attractions\b",
    ],
    "Asylum": [
        r"\basylum\b",
        r"\bpsychiatric\b",
        r"\bpsych[\s-]*ward\b",
        r"\bsan[ai]t[oa]rium\b",
        r"\blunatic\b",
        r"\bmadhouse\b",
        r"\bmanicomio\b",
        r"\bmental[\s-]+(?:health|hospital)\b",
        r"\binsan(e|ity)\b",
        r"\bretar[dt](ed|ation)?\b",
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
    ],
    "Bunker": [
        r"\bbunker\b",
        r"\b(?:bomb|fallout|air[\s-]*raid)[\s-]*shelter\b",
    ],
    "Cars": [
        r"\bjunkyard\b",
        r"\bscrapyard\b",
        r"\b(?:salvage|wrecking)[\s-]*yard\b",
        r"\b(?:auto|car)[\s-]+graveyard\b",
    ],
    "Castle": [
        r"\bcastle\b",
        r"\bcastillo\b",
        r"\bschloss\b",
        r"\bcitadel\b",
        r"\bbastille\b",
    ],
    "Church": [
        r"\bchurch\b",
        r"\bcathedral\b",
        r"\bchapel\b",
        r"\bbasilica\b",
        r"\b(?:monastery|convent|abbey|priory|friary|rectory)\b",
        r"\b(?:synagogue|mosque|minaret|temple|shrine|parish|sanctuary)\b",
        r"\biglesia\b",
        r"\b[eé]?glise\b",
        r"\bkirche\b",
        r"\bsantuario\b",
        r"\bErmita\b",
    ],
    "Fire Tower": [
        r"\bfire[\s-]*(?:tower|lookout)\b",
        r"\blookout[\s-]+tower\b",
    ],
    "Firehouse": [
        r"\bfire[\s-]*(station|hall|house)\b",
    ],
    "Funeral Home": [
        r"\bfuneral[\s-]*(?:home|parlou?r)\b",
        r"\bmortuary\b",
        r"\bcremator(?:y|ium|ie)\b",
        r"\bmorgue\b",
    ],
    "Graveyard": [
        r"\bgraveyard\b",
        r"\bcemeter(?:y|ies)\b",
        r"\bburial[\s-]+ground\b",
        r"\b(?:mausoleum|necropolis|churchyard|crypt|tomb)\b",
        r"\bcamposanto\b",
        r"\bcimeti[eè]re\b",
        r"\bfriedhof\b",
    ],
    "Hospital": [
        r"\bhospital\b",
        r"\binfirmary\b",
        r"\bmedical[\s-]+cent(?:er|re)\b",
        r"\bdispensary\b",
        r"\bh[oô]pital\b",
        r"\bkrankenhaus\b",
    ],
    "Hotel": [
        r"\b[hm][oô]tel\b",
        r"\binn\b",
        r"\blodge\b",
        r"\bhostel\b",
        r"\bposada\b",
    ],
    "House": [
        r"\bfarmhouse\b",
        r"\bcottage\b",
        r"\bbungalow\b",
    ],
    "Laboratory": [
        r"\blaborator(?:y|ies)\b",
        r"\bresearch[\s-]+facilit(?:y|ies)\b",
        r"\bscience[\s-]+cent(?:er|re)\b",
    ],
    "Library": [
        r"\blibrar(?:y|ies)\b",
        r"\bbiblioth[eè]que\b",
        r"\bbiblioteca\b",
        r"\bb[uü]cherei\b",
    ],
    "Lighthouse": [
        r"\blight[\s-]*(house|station)\b",
    ],
    "Mall": [
        r"\bshopping[\s-]*(?:mall|cent(?:er|re)|plaza|arcade)\b",
        r"\bgalleria\b",
    ],
    "Mansion": [
        r"\bmansion\b",
        r"\b(manor|grand)[\s-]*house\b",
    ],
    "Military Base": [
        r"\b(military|army|naval|air[\s-]*force|coast[\s-]*guard)\b",
        r"\bbarracks\b",
        r"\bcaserne\b",
    ],
    "Monument": [
        r"\bmonument\b",
        r"\b(?:war[\s-]+)?memorial\b",
        r"\bobelisk\b",
        r"\bstatue\b",
    ],
    "Police Station": [
        r"\bpolice[\s-]*(?:station|department|dept?)\b",
        r"\bcommissariat\b",
        r"\bconstabular(?:y|ies)\b",
        r"\bprecinct\b",
        r"\bsheriff(?:'s)?[\s-]+office\b",
        r"\bjailhouse\b",
    ],
    "Power Plant": [
        r"\bpower[\s-]*(?:plant|station)\b",
        r"\b(?:nuclear|coal|gas)[\s-]+(?:plant|station)\b",
        r"\b(?:generating|electrical)[\s-]+station\b",
        r"\bsub[\s-]*station\b",
    ],
    "Prison": [
        r"\bprison\b",
        r"\bpenitentiar(?:y|ies)\b",
        r"\bcorrectional[\s-]+facilit(?:y|ies)\b",
        r"\bdetention[\s-]+cent(?:er|re)\b",
        r"\breformator(?:y|ies)\b",
        r"\bpenal[\s-]+(?:colony|institution|farm)\b",
        r"\bjail\b",
        r"\bp[eé]nitencier\b",
    ],
    "Resort": [
        r"\bresort\b",
        r"\bretreat\b",
        r"\b(?:vacation|holid?ay)[\s-]+(?:camp|club|resort)\b",
    ],
    "Ruins": [
        r"\bruins?\b",
        r"\bdemolished\b",
    ],
    "School": [
        r"\b(?:high|elementary|middle)[\s-]*school\b",
        r"\bschool\b",
        r"\bacademy\b",
        r"\buniversity\b",
        r"\bcollege\b",
        r"\bseminar(?:y|ies)\b",
        r"\binstitute\b",
        r"\bdormi(?:tory|tories)\b",
        r"\b[eé]?cole\b",
        r"\bescuela\b",
        r"\bschule\b",
    ],
    "Stadium": [
        r"\bstadium\b",
        r"\barena\b",
        r"\bcolosseum\b",
        r"\bamphitheat(?:er|re)\b",
        r"\bballpark\b",
        r"\bsports[\s-]+complex\b",
        r"\bvelodrome\b",
    ],
    "Theater": [
        r"\btheat(?:er|re)\b",
        r"\bcinema\b",
        r"\b(?:movie|opera)[\s-]*house\b",
        r"\bplayhouse\b",
        r"\bauditorium\b",
        r"\bdrive[\s-]*in\b",
    ],
    "Traincar": [
        r"\b(?:train|rail)[\s-]*car\b",
        r"\bboxcar\b",
        r"\bcaboose\b",
        r"\blocomotiv(?:e|es)\b",
    ],
    "Train Station": [
        r"\b(?:train|rail(?:way|road)?)[\s-]*station\b",
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
        r"\b(?:foundr|brewer|distiller|canner|tanner|refiner)(?:y|ies)\b",
        r"\bmanufacturing\b",
        r"\busine\b",
        r"\bf[aá]brica\b",
    ],
}


@functools.lru_cache(maxsize=1)
def _get_compiled() -> dict[str, list[re.Pattern[str]]]:
    """Return the lazily-compiled pattern dict, building it on first access.

    ``lru_cache`` gives the same compile-once behaviour as a module-level
    global without mutable module state.
    """
    return {category: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns] for category, patterns in CATEGORY_PATTERNS.items()}


def categorize_by_keywords(text: str) -> str | None:
    """
    Attempt to categorize a location using regex keyword matching.

    Iterates through known urbex categories in priority order and returns the first
    category whose patterns match the supplied text.  Only name and place_name fields
    should be passed in - avoid addresses, which produce too many false positives
    (e.g. "Church Street" → Church).

    Args:
        text: Combined wiki name / description text to search.

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
