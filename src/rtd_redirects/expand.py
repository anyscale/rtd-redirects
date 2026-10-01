"""Multi-source expansion of redirect entries.

Fans a single YAML entry whose ``from:`` is a list into one canonical
``Redirect`` per source, each sharing the entry's ``to`` and other fields.
The parser routes list-valued entries through ``expand_entry``; canonical
entries skip this module. ``collapse.py`` is the dump-time inverse.

Multi-version expansion (per-entry ``versions:`` and top-level
``defaults.versions``) was removed in 0.3.0. RtD's version-less ``page`` type
with ``force: false`` covers the cross-version case, and a version-specific
``exact`` rule is written with a fully-qualified ``from``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rtd_redirects.exceptions import ParseError
from rtd_redirects.model import REDIRECT_TYPES, URL_STYLE_TYPES, Redirect

DEFAULT_LANGUAGE_PREFIX = "/en"
"""Default RtD-style language URL segment. Override per call via ``language_prefix=``."""



def expand_entry(file: Path, index: int, entry: dict[str, Any]) -> list[Redirect]:
    """Expand one multi-source entry into canonical Redirects, one per ``from``.

    ``file`` and ``index`` are used for error-message context only.
    """
    type_value = _read_type(file, index, entry)
    from_list = _read_from(file, index, entry, type_value)
    to_value = _read_to(file, index, entry, type_value)

    records: list[Redirect] = []
    for from_url in from_list:
        try:
            records.append(Redirect(
                from_url=from_url,
                to_url=to_value,
                type=type_value,
                http_status=entry.get("status", 301),
                force=entry.get("force", False),
                enabled=entry.get("enabled", True),
                position=entry.get("position", index),
                description=entry.get("description") or "",
            ))
        except (TypeError, ValueError) as e:
            raise _err(file, index, str(e)) from e
    return records


def _read_from(file: Path, index: int, entry: dict[str, Any], type_value: str) -> list[str]:
    """Read ``from:`` for an entry. URL-style types (``clean_url_to_html`` /
    ``html_to_clean_url``) describe project-wide URL transitions and don't
    require this field on the API; missing/empty values produce a single
    empty-string entry that ``_to_api`` will omit.
    """
    value = entry.get("from")
    if value is None:
        if type_value in URL_STYLE_TYPES:
            return [""]
        raise _err(file, index, "missing required field 'from'")
    if isinstance(value, str):
        result = [value]
    elif isinstance(value, list):
        if not value:
            raise _err(file, index, "'from' list cannot be empty")
        for item in value:
            if not isinstance(item, str):
                raise _err(
                    file, index,
                    f"'from' list items must be strings, got {type(item).__name__}",
                )
        result = list(value)
    else:
        raise _err(
            file, index,
            f"'from' must be a string or list of strings, got {type(value).__name__}",
        )
    for url in result:
        if url and is_external(url):
            raise _err(
                file, index,
                f"'from' must be a project path, not an external URL ({url!r}); "
                "RtD only redirects from paths the project serves",
            )
    return result


def _read_to(file: Path, index: int, entry: dict[str, Any], type_value: str) -> str:
    """Read ``to:`` for an entry. URL-style types may omit this."""
    value = entry.get("to")
    if value is None:
        if type_value in URL_STYLE_TYPES:
            return ""
        raise _err(file, index, "missing required field 'to'")
    if not isinstance(value, str):
        raise _err(file, index, f"'to' must be a string, got {type(value).__name__}")
    return value


def _read_type(file: Path, index: int, entry: dict[str, Any]) -> str:
    value = entry.get("type")
    if value is None:
        raise _err(file, index, "missing required field 'type'")
    if not isinstance(value, str):
        raise _err(file, index, f"'type' must be a string, got {type(value).__name__}")
    if value not in REDIRECT_TYPES:
        raise _err(
            file, index,
            f"invalid type {value!r}; expected one of {sorted(REDIRECT_TYPES)}",
        )
    return value


def is_external(url: str) -> bool:
    """True when ``url`` has a URL scheme or is protocol-relative.

    Covers absolute URLs (``https://docs.anyscale.com/x``), schemes without
    a host (``mailto:foo@example.com``, ``tel:+1234567890``), and
    protocol-relative URLs (``//cdn.example.com/x``). These targets are
    absolute and must not receive the project's language-prefix qualification.
    """
    parsed = urlparse(url)
    return bool(parsed.scheme or parsed.netloc)


def _err(file: Path, index: int, message: str) -> ParseError:
    return ParseError(f"{file}: redirects[{index}]: {message}")
