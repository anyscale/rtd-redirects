"""YAML reader for redirect source files.

Reads one or more YAML files, validates the schema, and produces a canonical
``RedirectSet``. Error messages include the source file and the entry's
position in the ``redirects:`` list so authors see exactly where to fix.

Multi-source (``from:`` as a list), multi-version (``versions:`` keys), and
top-level ``defaults:`` are expansion features handled by ``expand.py``. Until
that module lands, this parser rejects them with a clear message pointing at
the upcoming module rather than silently producing a different ``RedirectSet``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rtd_redirects.model import REDIRECT_TYPES, Redirect, RedirectSet

SCHEMA_VERSION = 1

_EXPANSION_NOT_YET_WIRED = (
    "expansion feature (multi-source / multi-version) requires expand.py, "
    "which is not yet wired into this parser"
)


class ParseError(Exception):
    """Raised when YAML parsing or schema validation fails."""


@dataclass(frozen=True)
class _Ctx:
    file: Path
    index: int


def parse_files(files: Iterable[Path]) -> RedirectSet:
    """Parse one or more YAML files into a single ``RedirectSet``.

    Files are processed in sorted-by-path order so concatenation is
    deterministic. Identity collisions across files raise ``ParseError``.
    """
    rs = RedirectSet()
    for path in sorted(files, key=str):
        for r in _parse_file(path):
            try:
                rs.add(r)
            except ValueError as e:
                raise ParseError(f"{path}: {e}") from e
    return rs


def parse_file(path: Path) -> RedirectSet:
    """Parse a single YAML file into a ``RedirectSet``."""
    return parse_files([path])


def _parse_file(path: Path) -> Iterable[Redirect]:
    try:
        text = path.read_text()
    except OSError as e:
        raise ParseError(f"{path}: cannot read: {e}") from e

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ParseError(f"{path}: YAML parse error: {e}") from e

    if doc is None:
        return []

    if not isinstance(doc, dict):
        raise ParseError(
            f"{path}: top-level must be a mapping, got {type(doc).__name__}"
        )

    _validate_schema_version(path, doc)

    if "defaults" in doc:
        raise ParseError(f"{path}: top-level 'defaults': {_EXPANSION_NOT_YET_WIRED}")

    redirects = doc.get("redirects")
    if redirects is None:
        return []
    if not isinstance(redirects, list):
        raise ParseError(
            f"{path}: 'redirects' must be a list, got {type(redirects).__name__}"
        )

    return [
        _parse_entry(_Ctx(file=path, index=i), entry)
        for i, entry in enumerate(redirects)
    ]


def _validate_schema_version(path: Path, doc: dict[str, Any]) -> None:
    if "schema_version" not in doc:
        raise ParseError(f"{path}: 'schema_version' is required at the top level")
    version = doc["schema_version"]
    if version != SCHEMA_VERSION:
        raise ParseError(
            f"{path}: unsupported schema_version {version!r}; "
            f"this version of rtd-redirects supports {SCHEMA_VERSION}"
        )


def _parse_entry(ctx: _Ctx, entry: Any) -> Redirect:
    if not isinstance(entry, dict):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}] must be a mapping, "
            f"got {type(entry).__name__}"
        )

    if isinstance(entry.get("from"), list) or isinstance(entry.get("to"), list):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: list-valued 'from' or 'to': "
            f"{_EXPANSION_NOT_YET_WIRED}"
        )
    if "versions" in entry:
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: per-entry 'versions': "
            f"{_EXPANSION_NOT_YET_WIRED}"
        )

    _require_str(ctx, entry, "from")
    _require_str(ctx, entry, "to")
    _require_str(ctx, entry, "type")

    type_ = entry["type"]
    if type_ not in REDIRECT_TYPES:
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: invalid type {type_!r}; "
            f"expected one of {sorted(REDIRECT_TYPES)}"
        )

    try:
        return Redirect(
            from_url=entry["from"],
            to_url=entry["to"],
            type=type_,
            http_status=entry.get("status", 301),
            force=entry.get("force", False),
            enabled=entry.get("enabled", True),
            position=entry.get("position", ctx.index),
            description=entry.get("description") or "",
        )
    except (TypeError, ValueError) as e:
        raise ParseError(f"{ctx.file}: redirects[{ctx.index}]: {e}") from e


def _require_str(ctx: _Ctx, entry: dict[str, Any], field: str) -> None:
    if field not in entry:
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: missing required field {field!r}"
        )
    if not isinstance(entry[field], str):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: {field!r} must be a string, "
            f"got {type(entry[field]).__name__}"
        )
