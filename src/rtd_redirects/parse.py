"""YAML reader for redirect source files.

Reads one or more YAML files, validates the schema, and produces a canonical
``RedirectSet``. Error messages include the source file and the entry's
position in the ``redirects:`` list so authors see exactly where to fix.

Expansion-shaped entries (list-valued ``from:``, per-entry ``versions:``, and
path-only ``from:`` paired with top-level ``defaults.versions``) are routed to
``expand.py``. Canonical 1:1 entries take the short path here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import expand_entry
from rtd_redirects.model import REDIRECT_TYPES, Redirect, RedirectSet

SCHEMA_VERSION = 1

_VERSION_PREFIX = re.compile(r"^/en/([^/]+)/")

__all__ = ["ParseError", "SCHEMA_VERSION", "parse_file", "parse_files"]


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
    defaults_versions = _read_defaults_versions(path, doc)

    redirects = doc.get("redirects")
    if redirects is None:
        return []
    if not isinstance(redirects, list):
        raise ParseError(
            f"{path}: 'redirects' must be a list, got {type(redirects).__name__}"
        )

    out: list[Redirect] = []
    for i, entry in enumerate(redirects):
        out.extend(_parse_entry(_Ctx(file=path, index=i), entry, defaults_versions))
    return out


def _validate_schema_version(path: Path, doc: dict[str, Any]) -> None:
    if "schema_version" not in doc:
        raise ParseError(f"{path}: 'schema_version' is required at the top level")
    version = doc["schema_version"]
    if version != SCHEMA_VERSION:
        raise ParseError(
            f"{path}: unsupported schema_version {version!r}; "
            f"this version of rtd-redirects supports {SCHEMA_VERSION}"
        )


def _read_defaults_versions(path: Path, doc: dict[str, Any]) -> list[str] | None:
    """Return ``defaults.versions`` from the top level, or ``None`` if absent."""
    defaults = doc.get("defaults")
    if defaults is None:
        return None
    if not isinstance(defaults, dict):
        raise ParseError(
            f"{path}: 'defaults' must be a mapping, got {type(defaults).__name__}"
        )
    versions = defaults.get("versions")
    if versions is None:
        return None
    if not isinstance(versions, list):
        raise ParseError(
            f"{path}: 'defaults.versions' must be a list, "
            f"got {type(versions).__name__}"
        )
    return versions


def _parse_entry(
    ctx: _Ctx,
    entry: Any,
    defaults_versions: list[str] | None,
) -> Iterable[Redirect]:
    if not isinstance(entry, dict):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}] must be a mapping, "
            f"got {type(entry).__name__}"
        )

    if _needs_expansion(entry, defaults_versions):
        return expand_entry(ctx.file, ctx.index, entry, defaults_versions)
    return [_parse_canonical(ctx, entry)]


def _needs_expansion(entry: dict[str, Any], defaults_versions: list[str] | None) -> bool:
    """True when an entry must be routed through ``expand_entry``."""
    if isinstance(entry.get("from"), list) or isinstance(entry.get("to"), list):
        return True
    if "versions" in entry:
        return True
    from_value = entry.get("from")
    if (
        defaults_versions is not None
        and isinstance(from_value, str)
        and not _VERSION_PREFIX.match(from_value)
    ):
        return True
    return False


def _parse_canonical(ctx: _Ctx, entry: dict[str, Any]) -> Redirect:
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
