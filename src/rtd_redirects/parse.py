"""YAML reader for redirect source files.

Reads one or more YAML files, validates the schema, and produces a canonical
``RedirectSet``. Error messages include the source file and the entry's
position in the ``redirects:`` list so authors see exactly where to fix.

Expansion-shaped entries (list-valued ``from:``, per-entry ``versions:``, and
path-only ``from:`` paired with top-level ``defaults.versions``) are routed to
``expand.py``. Canonical 1:1 entries take the short path here.

The URL language segment (``/en`` for ``docs.ray.io/en/latest/...``) is
configurable per file via top-level ``language_prefix:``, defaulting to
``/en``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX, expand_entry, is_external
from rtd_redirects.model import (
    REDIRECT_TYPES,
    URL_STYLE_TYPES,
    VERSION_AGNOSTIC_TYPES,
    Redirect,
    RedirectSet,
)

SCHEMA_VERSION = 1

__all__ = ["ParseError", "SCHEMA_VERSION", "parse_file", "parse_files", "parse_text"]


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
                raise _duplicate_identity_error(path, r) from e
    return rs


def parse_file(path: Path) -> RedirectSet:
    """Parse a single YAML file into a ``RedirectSet``."""
    return parse_files([path])


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
    language_prefix = _read_language_prefix(source, doc)
    defaults_versions = _read_defaults_versions(source, doc)

    redirects = doc.get("redirects")
    if redirects is None:
        return []
    if not isinstance(redirects, list):
        raise ParseError(
            f"{source}: 'redirects' must be a list, got {type(redirects).__name__}"
        )

    out: list[Redirect] = []
    for i, entry in enumerate(redirects):
        out.extend(_parse_entry(
            _Ctx(file=source, index=i), entry, defaults_versions, language_prefix,
        ))
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


def _read_language_prefix(path: Path, doc: dict[str, Any]) -> str:
    """Return top-level ``language_prefix:`` or the default ``/en``."""
    value = doc.get("language_prefix")
    if value is None:
        return DEFAULT_LANGUAGE_PREFIX
    if not isinstance(value, str):
        raise ParseError(
            f"{path}: 'language_prefix' must be a string, got {type(value).__name__}"
        )
    return value


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
    language_prefix: str,
) -> Iterable[Redirect]:
    if not isinstance(entry, dict):
        raise ParseError(
            f"{ctx.file}: redirects[{ctx.index}] must be a mapping, "
            f"got {type(entry).__name__}"
        )

    if _needs_expansion(entry, defaults_versions, language_prefix):
        try:
            return expand_entry(
                ctx.file, ctx.index, entry, defaults_versions,
                language_prefix=language_prefix,
            )
        except ValueError as e:
            raise ParseError(f"{ctx.file}: {e}") from e
    return [_parse_canonical(ctx, entry)]


def _needs_expansion(
    entry: dict[str, Any],
    defaults_versions: list[str] | None,
    language_prefix: str,
) -> bool:
    """True when an entry must be routed through ``expand_entry``."""
    if isinstance(entry.get("from"), list) or isinstance(entry.get("to"), list):
        return True
    if "versions" in entry:
        return True
    type_ = entry.get("type")
    if type_ in VERSION_AGNOSTIC_TYPES:
        # Page / clean URL types apply across versions on RtD's side; don't
        # fan them out across defaults.versions.
        return False
    from_value = entry.get("from")
    if (
        defaults_versions is not None
        and isinstance(from_value, str)
        and not re.match(rf"^{re.escape(language_prefix)}/[^/]+/", from_value)
    ):
        return True
    return False


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
