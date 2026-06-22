"""Constants and choices for site-wide settings."""

from __future__ import annotations

AI_PROVIDER_OPENAI = "openai"
AI_PROVIDER_CLOUDFLARE = "cloudflare"
AI_PROVIDER_CHOICES = [
    (AI_PROVIDER_CLOUDFLARE, "Cloudflare Workers AI"),
    (AI_PROVIDER_OPENAI, "OpenAI"),
]

SEARCH_PROVIDER_BRAVE = "brave"
SEARCH_PROVIDER_GOOGLE = "google"
SEARCH_PROVIDER_CHOICES = [
    (SEARCH_PROVIDER_BRAVE, "Brave Search"),
    (SEARCH_PROVIDER_GOOGLE, "Google Custom Search"),
]

DEFAULT_OPENAI_MODEL = "gpt-5-nano"
DEFAULT_CLOUDFLARE_MODEL = "@cf/mistral/mistral-7b-instruct-v0.1"
