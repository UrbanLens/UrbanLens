from abc import ABC
from dataclasses import dataclass, field

import requests


@dataclass(frozen=True, slots=True, kw_only=True)
class Gateway(ABC):  # noqa: B024 - Abstract so it cannot be instantiated directly
    """
    An abstract class to serve as a template for API gateways.
    """

    session: requests.Session = field(default_factory=requests.Session)
