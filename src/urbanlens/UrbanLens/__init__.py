"""UrbanLens Django project package."""

from __future__ import annotations

from urbanlens.UrbanLens.celery import app as celery_app

__all__ = ("celery_app",)
