"""Real-user and independence checks built on precomputed ``ProfileTrustSignals``."""

from urbanlens.dashboard.services.trust.checks import are_independent, is_real_user, real_user_score
from urbanlens.dashboard.services.trust.signals import compute_signals, signals_for

__all__ = ["are_independent", "compute_signals", "is_real_user", "real_user_score", "signals_for"]
