"""Small interpreter compatibility hooks for the GeoWiki application venv.

The cloud service currently uses Python 3.10, while ``enum.StrEnum`` entered
the standard library in Python 3.11.  Python imports ``sitecustomize`` during
normal startup, before Uvicorn loads application modules.  Supplying the
missing type here lets the same hash-pinned application sources run on both
interpreter generations without changing their decision or retrieval logic.
"""

from __future__ import annotations

import enum


if not hasattr(enum, "StrEnum"):

    class StrEnum(str, enum.Enum):
        """Python 3.10-compatible subset of the Python 3.11 ``StrEnum``."""

        def __new__(cls, value: str):
            if not isinstance(value, str):
                raise TypeError(f"{value!r} is not a string")
            member = str.__new__(cls, value)
            member._value_ = value
            return member

        def __str__(self) -> str:
            return self.value

        @staticmethod
        def _generate_next_value_(
            name: str,
            start: int,
            count: int,
            last_values: list[str],
        ) -> str:
            del start, count, last_values
            return name.lower()

    StrEnum.__module__ = "enum"
    enum.StrEnum = StrEnum
