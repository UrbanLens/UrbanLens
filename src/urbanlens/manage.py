"""
Django's command-line utility for administrative tasks.
"""
#!/usr/bin/env python
import os
import sys
from pathlib import Path

# Ensure /app/src is on sys.path when running this file directly.
PROJECT_SRC = Path(__file__).resolve().parents[1]  # .../src
sys.path.insert(0, str(PROJECT_SRC))

_ENV_VARS_TO_PRINT = [
    "DJANGO_SETTINGS_MODULE",
    "UL_ENVIRONMENT",
    "DJANGO_DEBUG",
    "UL_ALLOWED_HOSTS",
    "UL_VALKEY_URL",
    "UL_REDIS_URL",
    "UL_UNSAFE_ALLOW_HTTP",
]

def _print_startup_env() -> None:
    """Print key environment variables before Django initialises."""
    print("--- UrbanLens startup environment ---", flush=True)
    for var in _ENV_VARS_TO_PRINT:
        val = os.environ.get(var)
        if val is None:
            print(f"  {var}: (not set)", flush=True)
        elif any(secret in var for secret in ("PASS", "SECRET", "KEY", "TOKEN")):
            print(f"  {var}: ***", flush=True)
        else:
            print(f"  {var}: {val}", flush=True)
    print("-------------------------------------", flush=True)


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'urbanlens.UrbanLens.settings')
    _print_startup_env()
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
