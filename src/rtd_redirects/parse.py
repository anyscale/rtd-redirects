"""YAML reader for redirect source files.

Reads a YAML file, validates the schema, and produces a canonical
``RedirectSet``. Error messages include the source file and the entry's
position in the ``redirects:`` list so authors see exactly where to fix.

Entries whose ``from:`` is a list are routed to ``expand.py``, which fans them
out into one record per source. Canonical 1:1 entries take the short path here.

A redirect set is one file. Ordered multi-file composition and multi-version
expansion (``versions:`` and ``defaults.versions``) were removed in 0.3.0; a
file that still uses ``defaults:`` or ``versions:`` fails to parse with a
message that says how to rewrite it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import expand_entry, is_external
from rtd_redirects.model import (
    REDIRECT_TYPES,
    URL_STYLE_TYPES,
    Redirect,
    RedirectSet,
)

SCHEMA_VERSION = 1

_REMOVED_MULTI_VERSION = (
    "was removed in rtd-redirects 0.3.0 along with multi-version expansion. "
    "Use a version-less 'page' rule, which RtD applies on every version and "
    "which fires only where the page 404s, or write one 'exact' entry per "
    "version with a fully-qualified 'from' such as /en/<version>/<path>."
)

__all__ = [
    "ParseError",
    "SCHEMA_VERSION",
    "parse_file",
    "parse_text",
]


@dataclass(frozen=True)
class _Ctx:
    file: Path
    index: int


def _duplicate_identity_error(source: Path, r: Redirect) -> ParseError:
    """A ``ParseError`` that names the offending identity and the way out.

    Authored YAML must hold one entry per ``(from_url, type)`` — a collision is
    almost always a copy-paste mistake, so the parser fails loudly. Live RtD
    data is different: RtD permits duplicate identities, and the read path
    (``plan`` / ``audit`` / ``apply`` / ``dump``) tolerates them, keeping the
    lowest-position record. See DOC-946.
    """
    return ParseError(
        f"{source}: duplicate redirect identity {r.identity}: more than one "
        "entry resolves to the same (from_url, type). Authored YAML must have "
        "one entry per identity — merge or remove the duplicate. (Live RtD "
        "data may legitimately contain duplicates; plan, audit, apply, and "
        "dump tolerate them and keep the lowest-position record.)"
    )


def parse_file(path: Path) -> RedirectSet:
    """Parse a single YAML file into a ``RedirectSet``."""
    return _parse_to_set(path)


def _parse_to_set(path: Path) -> RedirectSet:
    """Parse one file into its own ``RedirectSet``, failing on in-file duplicates."""
    rs = RedirectSet()
    for r in _parse_file(path):
        try:
            rs.add(r)
        except ValueError as e:
            raise _duplicate_identity_error(path, r) from e
    return rs


def parse_text(text: str, *, source: str | Path = "<input>") -> RedirectSet:
    """Parse YAML content from a string.

    ``source`` is only used for error messages — useful when the YAML was
    read from a git ref (``git show <ref>:<path>``) rather than disk so
    errors still point at something the author can act on.
    """
    source_path = source if isinstance(source, Path) else Path(str(source))
    rs = RedirectSet()
    for r in _process_text(text, source_path):
        try:
            rs.add(r)
        except ValueError as e:
            raise _duplicate_identity_error(source_path, r) from e
    return rs


def _parse_file(path: Path) -> Iterable[Redirect]:
    try:
        text = path.read_text()
    except OSError as e:
        raise ParseError(f"{path}: cannot read: {e}") from e
    return _process_text(text, path)


def _process_text(text: str, source: Path) -> Iterable[Redirect]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ParseError(f"{source}: YAML parse error: {e}") from e

    if doc is None:
        return []

    if not isinstance(doc, dict):
        raise ParseError(
            f"{source}: top-level must be a mapping, got {type(doc).__name__}"
        )

    _validate_schema_version(source, doc)
    _check_language_prefix(source, doc)
    if "defaults" in doc:
        raise ParseError(f"{source}: 'defaults' {_REMOVED_MULTI_VERSION}")

    redirects = doc.get("redirects")
    if redirects is None:
        return []
    if not isinstance(redirects, list):
        raise ParseError(
            f"{source}: 'redirects' must be a list, got {type(redirects).__name__}"
        )

    out: list[Redirect] = []
    for i, entry in enumerate(redirects):
        out.extend(_parse_entry(_Ctx(file=source, index=i), entry))
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


def _check_language_prefix(path: Path, doc: dict[str, Any]) -> None:
    """Reject a non-string top-level ``language_prefix:``.

    The key is still accepted so existing files parse, but nothing reads it
    since multi-version expansion, its only consumer, was removed.
    """
    value = doc.get("language_prefix")
    if value is not None and not isinstance(value, str):
        raise ParseError(
            f"{path}: 'language_prefix' must be a string, got {type(value).__name__}"
        )


def _parse_entry(ctx: _Ctx, entry: Any) -> Iterable[Redirect]:
    if not isinstance(entry, dict):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}] must be a mapping, "
            f"got {type(entry).__name__}"
        )
    if "versions" in entry:
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: 'versions' {_REMOVED_MULTI_VERSION}"
        )
    if isinstance(entry.get("from"), list) or isinstance(entry.get("to"), list):
        return expand_entry(ctx.file, ctx.index, entry)
    return [_parse_canonical(ctx, entry)]


def _parse_canonical(ctx: _Ctx, entry: dict[str, Any]) -> Redirect:
    _require_str(ctx, entry, "type")
    type_ = entry["type"]
    if type_ not in REDIRECT_TYPES:
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: invalid type {type_!r}; "
            f"expected one of {sorted(REDIRECT_TYPES)}"
        )

    # URL-style types describe project-wide transitions on RtD's side and
    # don't require from_url / to_url.
    if type_ in URL_STYLE_TYPES:
        from_value = entry.get("from", "")
        to_value = entry.get("to", "")
        if from_value and not isinstance(from_value, str):
            raise ParseError(
                f"{ctx.file}: redirects[{ctx.index}]: 'from' must be a string"
            )
        if to_value and not isinstance(to_value, str):
            raise ParseError(
                f"{ctx.file}: redirects[{ctx.index}]: 'to' must be a string"
            )
    else:
        _require_str(ctx, entry, "from")
        _require_str(ctx, entry, "to")
        from_value = entry["from"]
        to_value = entry["to"]

    if from_value and is_external(from_value):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}]: 'from' must be a project path, "
            f"not an external URL ({from_value!r}); RtD only redirects from "
            "paths the project serves"
        )

    try:
        return Redirect(
            from_url=from_value,
            to_url=to_value,
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
