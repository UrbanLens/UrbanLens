"""Achievements: metric registry, streak tracking, and award evaluation."""
from urbanlens.dashboard.services.achievements.activity import rebuild_streak, record_activity
from urbanlens.dashboard.services.achievements.evaluate import (
    active_metric_keys,
    evaluate_achievement_for_all,
    evaluate_all_profiles,
    evaluate_profile,
    progress_for_profile,
)
from urbanlens.dashboard.services.achievements.metrics import (
    Metric,
    all_metrics,
    compute_values,
    get_metric,
    grouped_metric_choices,
    metric_choices,
    metrics_for_triggers,
    register,
    streak_summary,
)
