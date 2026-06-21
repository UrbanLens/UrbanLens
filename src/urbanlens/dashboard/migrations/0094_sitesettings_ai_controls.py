"""Add AI provider, model, and feature-toggle fields to SiteSettings."""
from django.db import migrations, models


class Migration(migrations.Migration):

	dependencies = [
		("dashboard", "0093_seed_default_categories_per_user"),
	]

	operations = [
		migrations.AddField(
			model_name="sitesettings",
			name="ai_enabled",
			field=models.BooleanField(
				default=True,
				help_text="Master toggle for all AI features. Disabling this prevents all AI API calls.",
				verbose_name="AI enabled",
			),
		),
		migrations.AddField(
			model_name="sitesettings",
			name="ai_provider",
			field=models.CharField(
				choices=[("cloudflare", "Cloudflare Workers AI"), ("openai", "OpenAI")],
				default="cloudflare",
				help_text="Which AI provider to use for all AI-powered features.",
				max_length=20,
				verbose_name="AI provider",
			),
		),
		migrations.AddField(
			model_name="sitesettings",
			name="openai_model",
			field=models.CharField(
				default="gpt-5-nano",
				help_text="OpenAI model name (e.g. gpt-4o, gpt-4o-mini, gpt-5-nano). Only used when provider is OpenAI.",
				max_length=100,
				verbose_name="OpenAI model",
			),
		),
		migrations.AddField(
			model_name="sitesettings",
			name="cloudflare_model",
			field=models.CharField(
				default="@cf/mistral/mistral-7b-instruct-v0.1",
				help_text="Cloudflare Workers AI model name. Only used when provider is Cloudflare.",
				max_length=200,
				verbose_name="Cloudflare model",
			),
		),
		migrations.AddField(
			model_name="sitesettings",
			name="ai_category_suggestions_enabled",
			field=models.BooleanField(
				default=True,
				help_text="Allow AI to suggest categories for pins and locations based on their metadata.",
				verbose_name="Category suggestions",
			),
		),
	]
