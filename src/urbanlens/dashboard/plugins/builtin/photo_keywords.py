"""Built-in photo keywording plugins.

Three independent strategies, each storing its own ``ImageKeyword`` rows so
they coexist and can be regenerated separately:

- **Embedded metadata** (:class:`PhotoMetadataKeywordsPlugin`): the established
  way - XMP ``dc:subject`` and IPTC keyword tags photographers embed via
  Lightroom/digiKam etc. Local, free, always on.
- **AI vision** (:class:`AiVisionKeywordsPlugin`): asks the site's AI provider
  to describe the photo. Costs real money per call, so it requires the
  ``AI_PHOTO_PROCESSING`` subscription feature (deliberately separate from the
  cheaper text-only ``AI`` feature) plus the user's AI toggles. Images are
  downscaled before being sent.
- **Content classifier** (:class:`ClassifierKeywordsPlugin`): Cloudflare
  Workers AI ResNet-50 image classification - near-free label+confidence
  pairs, no subscription needed, but still an external call so it respects the
  user's external-APIs toggle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.photo_keywords import KeywordResult, PhotoKeywordProvider, downscaled_jpeg_bytes
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

#: Classifier labels below this confidence are noise, not keywords.
CLASSIFIER_MIN_CONFIDENCE = 0.15


def _xmp_subjects(xmp: dict[str, Any]) -> list[str]:
    """Pull dc:subject keyword entries out of Pillow's parsed XMP dict.

    Args:
        xmp: The dict returned by ``PIL.Image.Image.getxmp()``.

    Returns:
        Keyword strings found in the XMP packet (may be empty).
    """
    keywords: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == "subject":
                    _collect(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    def _collect(value: Any) -> None:
        # subject is usually {"Bag": {"li": [...]}} but flat forms exist too.
        if isinstance(value, str):
            keywords.append(value)
        elif isinstance(value, list):
            for item in value:
                _collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                _collect(item)

    _walk(xmp)
    return keywords


class MetadataKeywordProvider(PhotoKeywordProvider):
    """Reads keywords the photographer embedded in the file (XMP/IPTC)."""

    slug = "photo_keywords_metadata"
    label = "Embedded metadata keywords"

    def generate(self, image: Image) -> list[KeywordResult]:
        """Extract XMP dc:subject and IPTC 2:25 keyword tags from the stored file.

        Args:
            image: The uploaded image.

        Returns:
            Embedded keywords; empty when the file carries none.
        """
        from PIL import Image as PILImage, IptcImagePlugin

        keywords: list[str] = []
        with image.image.open("rb") as stored_file:
            pil_image = PILImage.open(stored_file)

            try:
                xmp = pil_image.getxmp()
            except Exception:  # Pillow raises varied errors on malformed XMP
                xmp = {}
            if xmp:
                keywords.extend(_xmp_subjects(xmp))

            try:
                iptc = IptcImagePlugin.getiptcinfo(pil_image) or {}
            except (OSError, SyntaxError):
                iptc = {}
            raw_iptc = iptc.get((2, 25))
            if raw_iptc:
                entries = raw_iptc if isinstance(raw_iptc, list) else [raw_iptc]
                for entry in entries:
                    if isinstance(entry, bytes):
                        keywords.append(entry.decode("utf-8", errors="replace"))
                    elif isinstance(entry, str):
                        keywords.append(entry)

        return [KeywordResult(keyword=keyword) for keyword in keywords]


class PhotoMetadataKeywordsPlugin(UrbanLensPlugin):
    """Keywords from XMP/IPTC tags embedded in uploaded photos."""

    name: ClassVar[str] = "photo_keywords_metadata"
    verbose_name: ClassVar[str] = "Photo keywords: embedded metadata"
    description: ClassVar[str] = "Makes photos searchable using the XMP/IPTC keyword tags photographers embed in their files. Runs locally; no external calls."
    author: ClassVar[str] = "UrbanLens"

    def get_photo_keyword_providers(self) -> list[PhotoKeywordProvider]:
        """Contribute the embedded-metadata keyword reader."""
        return [MetadataKeywordProvider()]


class AiVisionKeywordProvider(PhotoKeywordProvider):
    """Asks the site's AI provider to describe the photo as keywords."""

    slug = "photo_keywords_ai_vision"
    label = "AI vision keywords"

    def is_available_for(self, image: Image) -> bool:
        """Gate on the AI photo processing subscription and every AI toggle.

        Requires: site-wide AI enabled, an uploader with AI and external APIs
        enabled on their profile, and the uploader holding the
        ``AI_PHOTO_PROCESSING`` subscription feature (vision calls cost more
        than the text features the plain ``AI`` feature covers).

        Args:
            image: The uploaded image.

        Returns:
            True when the AI vision call is allowed for this uploader.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        profile = image.profile
        if profile is None or not profile.ai_enabled or not profile.external_apis_enabled:
            return False
        if not SiteSettings.get_current().ai_enabled:
            return False
        return user_has_feature(profile.user, SiteFeature.AI_PHOTO_PROCESSING)

    def generate(self, image: Image) -> list[KeywordResult]:
        """Downscale the photo and ask the AI provider for descriptive keywords.

        Args:
            image: The uploaded image.

        Returns:
            AI-described keywords; empty when the call fails (errors logged).
        """
        from urbanlens.dashboard.services.ai.vision import describe_photo_keywords

        small = downscaled_jpeg_bytes(image)
        if small is None:
            return []
        return [KeywordResult(keyword=keyword) for keyword in describe_photo_keywords(small)]


class AiVisionKeywordsPlugin(UrbanLensPlugin):
    """AI-described photo keywords for subscribers with AI photo processing."""

    name: ClassVar[str] = "photo_keywords_ai_vision"
    verbose_name: ClassVar[str] = "Photo keywords: AI vision"
    description: ClassVar[str] = "Uses the site's AI provider to describe uploaded photos as searchable keywords. Requires the 'AI photo processing' subscription feature; photos are downscaled before being sent."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the vision-model calls."""
        from urbanlens.dashboard.services.ai.vision import SERVICE_AI_PHOTO_KEYWORDS

        return {
            SERVICE_AI_PHOTO_KEYWORDS: ServiceDefaults(
                display_name="AI photo keywords (vision)",
                calls_per_minute=10,
                calls_per_day=500,
                notes="Vision-model describe call per uploaded photo for AI-photo-processing subscribers. Costs more than text AI calls.",
            ),
        }

    def get_photo_keyword_providers(self) -> list[PhotoKeywordProvider]:
        """Contribute the AI vision keyword provider."""
        return [AiVisionKeywordProvider()]


class ClassifierKeywordProvider(PhotoKeywordProvider):
    """Labels photos with a content classifier (Cloudflare ResNet-50)."""

    slug = "photo_keywords_classifier"
    label = "Content classifier keywords"

    def is_available_for(self, image: Image) -> bool:
        """Requires configured Cloudflare credentials and the uploader's external-APIs toggle.

        Args:
            image: The uploaded image.

        Returns:
            True when the classifier call is allowed for this uploader.
        """
        from urbanlens.UrbanLens.settings.app import settings

        profile = image.profile
        if profile is None or not profile.external_apis_enabled:
            return False
        return bool(settings.cloudflare_worker_ai_endpoint and settings.cloudflare_ai_api_key)

    def generate(self, image: Image) -> list[KeywordResult]:
        """Downscale the photo and classify its content into keyword labels.

        ImageNet-style labels often bundle synonyms ("castle, fortress"); each
        synonym becomes its own keyword sharing the label's confidence.

        Args:
            image: The uploaded image.

        Returns:
            Scored keywords above ``CLASSIFIER_MIN_CONFIDENCE``.
        """
        from urbanlens.dashboard.services.ai.vision import classify_photo

        small = downscaled_jpeg_bytes(image)
        if small is None:
            return []
        results: list[KeywordResult] = []
        for label, score in classify_photo(small):
            if score < CLASSIFIER_MIN_CONFIDENCE:
                continue
            for synonym in label.split(","):
                if synonym.strip():
                    results.append(KeywordResult(keyword=synonym.strip(), confidence=score))
        return results


class ClassifierKeywordsPlugin(UrbanLensPlugin):
    """Content-classifier photo keywords (no subscription required)."""

    name: ClassVar[str] = "photo_keywords_classifier"
    verbose_name: ClassVar[str] = "Photo keywords: content classifier"
    description: ClassVar[str] = "Classifies uploaded photos with Cloudflare Workers AI ResNet-50 and stores the labels as searchable keywords. Requires Cloudflare Workers AI credentials."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the classifier calls."""
        from urbanlens.dashboard.services.ai.vision import SERVICE_PHOTO_CLASSIFIER

        return {
            SERVICE_PHOTO_CLASSIFIER: ServiceDefaults(
                display_name="Photo content classifier (ResNet-50)",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Cloudflare Workers AI image classification per uploaded photo. Near-free per call.",
            ),
        }

    def get_photo_keyword_providers(self) -> list[PhotoKeywordProvider]:
        """Contribute the classifier keyword provider."""
        return [ClassifierKeywordProvider()]
