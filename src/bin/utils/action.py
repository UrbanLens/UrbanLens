"""Custom argparse action for Enum arguments."""

import argparse
import enum
from typing import Any


class EnumAction(argparse.Action):
    """Argparse action handling Enums."""

    def __init__(self, **kwargs):
        enum_type = kwargs.pop("type", None)

        if enum_type is None:
            raise ValueError("type must be assigned an Enum when using EnumAction")
        if not issubclass(enum_type, enum.Enum):
            raise TypeError("type must be an Enum when using EnumAction")

        kwargs.setdefault("choices", tuple(e.name for e in enum_type))

        super().__init__(**kwargs)

        self._enum = enum_type

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        value: Any,
        option_string: str | None = None,
    ):
        if isinstance(value, str):
            value = self._enum[value]
            setattr(namespace, self.dest, value)
        elif value is None:
            raise argparse.ArgumentTypeError(f"You need to pass a value after {option_string}!")
        else:
            raise argparse.ArgumentTypeError("Invalid choice, must be a string")
