"""Record which AI model answered a call, and how many tokens it reported, on ApiCallLog."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0060_apicalllog_was_rejected_input"),
    ]

    operations = [
        migrations.AddField(
            model_name="apicalllog",
            name="model",
            field=models.CharField(blank=True, help_text="The AI model that answered, as the provider names it (a Cloudflare '@cf/...' path, an OpenAI or Anthropic id, an Ollama tag). Null for every call that is not an AI call, and for an AI call refused before it was made.", max_length=200, null=True),
        ),
        migrations.AddField(
            model_name="apicalllog",
            name="input_tokens",
            field=models.IntegerField(blank=True, help_text="Prompt tokens, as the provider reported them. Null when it reported none (a classifier, some Cloudflare models) or the call is not an AI call.", null=True),
        ),
        migrations.AddField(
            model_name="apicalllog",
            name="output_tokens",
            field=models.IntegerField(blank=True, help_text="Completion tokens, as the provider reported them. Null when it reported none or the call is not an AI call.", null=True),
        ),
    ]
