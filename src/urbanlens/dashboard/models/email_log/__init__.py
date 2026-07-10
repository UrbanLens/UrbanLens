"""Outbound email tracking models (rate limiting + duplicate prevention)."""

from urbanlens.dashboard.models.email_log.model import EmailSendLog, EmailType

__all__ = ["EmailSendLog", "EmailType"]
