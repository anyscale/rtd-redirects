"""Shared exception types.

Lives in its own module so ``parse`` and ``expand`` can share ``ParseError``
without a circular import.
"""

from __future__ import annotations


class ParseError(Exception):
    """Raised when YAML parsing, schema validation, or expansion fails."""
