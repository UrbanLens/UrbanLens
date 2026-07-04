# Generic imports
from __future__ import annotations

import logging

# App imports
from djangofoundry import helpers

logger = logging.getLogger(__name__)


class Queue(helpers.queue.Queue):
    unique_key = ["id"]
