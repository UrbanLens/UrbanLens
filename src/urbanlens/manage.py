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

def main():
    """Run administrative tasks."""
    #os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'urbanlens.UrbanLens.settings.local')
    # Temporarily Override the default settings module
    os.environ['DJANGO_SETTINGS_MODULE'] = 'urbanlens.UrbanLens.settings.local'
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
