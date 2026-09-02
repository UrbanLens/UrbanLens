"""A Django-free package: the process that holds AI provider credentials and nothing else.

Import direction is one-way: nothing under ``urbanlens_ai`` imports
``django``, ``urbanlens``, or any package that transitively pulls either in
(``urbanlens/__init__.py`` imports ``UrbanLens/celery.py``, which configures
Django and builds the Celery app on import - so even ``import urbanlens``
would drag Django in). ``dashboard.services.ai.inference_client`` is the one
place under ``urbanlens`` allowed to import from here.

This package is deployed two ways: as ``ai-inference``'s WSGI app
(``urbanlens_ai.wsgi:application``, holding provider API keys and nothing
else - no database, no cache, no secret key), and in-process inside
``dashboard.services.ai.inference_client.LocalInferenceClient`` for local
development, gated by ``services.sandbox.guard.check_direct_inference``.
"""
