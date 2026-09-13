"""Command-line scripts for the app (never import into Django). Run with --help for options.

Examples:
    >>> python db.py --help

Metadata:

    File: __init__.py
    Project: Urban Lens
    Author: Jess Mann

    Modified By: Jess Mann

    Copyright (c) 2022 Urban Lens
"""

# Do not import anything here, because we don't want these files imported elsewhere.
from .utils import *
