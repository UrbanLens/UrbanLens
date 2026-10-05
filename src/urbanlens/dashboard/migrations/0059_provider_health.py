"""Record each external provider's health and backoff, and route the alert when one is refusing or failing (REData PL13)."""

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0058_link_wayback_retry'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProviderHealth',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('provider', models.CharField(help_text="The rate-limiter service key the provider's calls are logged under.", max_length=50, unique=True)),
                ('state', models.CharField(choices=[('healthy', 'Healthy'), ('degraded', 'Degraded'), ('backed_off', 'Backed off'), ('probing', 'Probing')], default='healthy', max_length=16)),
                ('cause', models.CharField(blank=True, choices=[('', '-'), ('refused', 'Refusing this deployment'), ('failing', 'Failing'), ('below_baseline', 'Below its normal')], default='', max_length=16)),
                ('reason', models.TextField(blank=True, default='', help_text='The counts behind the last verdict, in one sentence a person can act on.')),
                ('level', models.PositiveSmallIntegerField(default=0, help_text="Consecutive backoffs, which set the next one's length. Back to 0 after a day healthy.")),
                ('state_since', models.DateTimeField(default=django.utils.timezone.now)),
                ('episode_started_at', models.DateTimeField(blank=True, help_text='When the provider last left healthy; null while healthy.', null=True)),
                ('backed_off_until', models.DateTimeField(blank=True, null=True)),
                ('counted_from', models.DateTimeField(blank=True, help_text='Calls before this are not judged, so the failures that backed a provider off cannot back it off again once it recovers.', null=True)),
                ('last_evaluated_at', models.DateTimeField(blank=True, null=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('last_ok_at', models.DateTimeField(blank=True, null=True)),
                ('window_minutes', models.PositiveIntegerField(default=0, help_text='The window the last verdict was read from.')),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('answered', models.PositiveIntegerField(default=0)),
                ('empty', models.PositiveIntegerField(default=0)),
                ('refused', models.PositiveIntegerField(default=0)),
                ('failed', models.PositiveIntegerField(default=0)),
                ('failure_status', models.PositiveSmallIntegerField(blank=True, help_text="The commonest HTTP status among that window's failures.", null=True)),
                ('baseline_attempts', models.PositiveIntegerField(default=0, help_text="Calls over the 7 days ending a day ago, from which the provider's normal is read.")),
                ('baseline_answered_share', models.FloatField(blank=True, null=True)),
                ('baseline_empty_share', models.FloatField(blank=True, null=True)),
                ('baseline_computed_at', models.DateTimeField(blank=True, null=True)),
                ('alerted_at', models.DateTimeField(blank=True, help_text="When a person was last told about this provider's current episode.", null=True)),
            ],
            options={
                'verbose_name': 'Provider health',
                'verbose_name_plural': 'Provider health',
                'db_table': 'dashboard_provider_health',
                'ordering': ['provider'],
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='notify_provider_health_email',
            field=models.BooleanField(db_default=True, default=True, help_text='Email the admin notification address when an external provider has been refusing or failing this site for 30 minutes, daily while it lasts, and when it recovers.', verbose_name='Provider refusing or failing (email)'),
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='notify_provider_health_gotify',
            field=models.BooleanField(db_default=True, default=True, help_text='Send a Gotify push notification when an external provider has been refusing or failing this site for 30 minutes, daily while it lasts, and when it recovers.', verbose_name='Provider refusing or failing (Gotify)'),
        ),
    ]
