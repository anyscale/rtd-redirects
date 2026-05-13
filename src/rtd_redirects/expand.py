"""Multi-source and multi-version expansion of redirect entries.

Fans a single YAML entry that uses ``from:`` as a list, ``versions:`` per-entry,
or top-level ``defaults.versions`` into canonical 1:1 ``Redirect`` records. The
parser routes expansion-shaped entries through ``expand_entry``; canonical
entries skip this module.

This iteration supports plain version names. Pattern-based identifiers
(globs, semver ranges, exclusions, macros per design.md §"Multi-version")
raise ``ParseError`` with a "not yet supported" message; that work lands in
a follow-up PR.

Resolution rules (design.md §"Global defaults via defaults.versions"):

1. Fully-qualified ``from:`` and no ``versions:`` -> version is in the URL;
   no expansion across versions.
2. Path-only ``from:`` and explicit ``versions:`` -> use that list.
3. Path-only ``from:`` and no ``versions:`` -> inherit ``defaults.versions``.
4. Fully-qualified ``from:`` with explicit ``versions:`` -> two sources of
   truth conflict; ``ParseError``.
5. Path-only ``from:`` with no ``versions:`` and no ``defaults.versions:``
   -> ``ParseError`` (no way to know which versions to target).

Language prefix
---------------

RtD URLs are typically shaped ``<language_prefix>/<version>/<path>`` (for
example ``/en/latest/data/inference.html``). The prefix is configurable
per call so a docs project that disables the per-language URL segment in
RtD can pass ``language_prefix="/<lang>"`` of its choice. A future
languageless setup (``/<version>/<path>`` with no language segment at all)
requires enumerating the live version list to distinguish "fully-qualified"
from "path-only" URLs and is rejected here with a clear error; that mode
will land alongside RtD's version-list integration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rtd_redirects.exceptions import ParseError
from rtd_redirects.model import REDIRECT_TYPES, Redirect

DEFAULT_LANGUAGE_PREFIX = "/en"
"""Default RtD-style language URL segment. Override per call via ``language_prefix=``."""

_PATTERN_NOT_YET_SUPPORTED = (
    "version pattern identifiers (globs, semver ranges, exclusions, macros) "
    "are not yet supported; use plain version names"
)


def expand_entry(
    file: Path,
    index: int,
    entry: dict[str, Any],
    defaults_versions: list[str] | None,
    *,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> list[Redirect]:
    """Expand one multi-source / multi-version entry into canonical Redirects.

    ``file`` and ``index`` are used for error-message context only.
    ``defaults_versions`` is the parsed value of top-level ``defaults.versions``
    or ``None`` if the file didn't set one.
    ``language_prefix`` is the URL segment that sits between the host and the
    version segment (e.g. ``/en`` for ``docs.ray.io/en/latest/...``). Must be
    non-empty and start with ``/``; languageless RtD setups are not yet
    supported.
    """
    _validate_language_prefix(language_prefix)
    from_list = _read_from(file, index, entry)
    to_value = _read_to(file, index, entry)
    type_value = _read_type(file, index, entry)
    versions = _resolve_versions(
        file, index, entry, from_list, defaults_versions, language_prefix,
    )

    records: list[Redirect] = []
    for version in versions:
        for from_url in from_list:
            try:
                records.append(Redirect(
                    from_url=_qualify(from_url, version, language_prefix),
                    to_url=_qualify(to_value, version, language_prefix),
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


def _validate_language_prefix(prefix: str) -> None:
    if not prefix:
        raise ValueError(
            "language_prefix='' (languageless RtD) requires version-list "
            "enumeration to distinguish path-only from fully-qualified URLs "
            "and is not yet supported"
        )
    if not prefix.startswith("/"):
        raise ValueError(f"language_prefix must start with '/', got {prefix!r}")
    if prefix.endswith("/"):
        raise ValueError(f"language_prefix must not end with '/', got {prefix!r}")


def _read_from(file: Path, index: int, entry: dict[str, Any]) -> list[str]:
    value = entry.get("from")
    if value is None:
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
        if is_external(url):
            raise _err(
                file, index,
                f"'from' must be a project path, not an external URL ({url!r}); "
                "RtD only redirects from paths the project serves",
            )
    return result


def _read_to(file: Path, index: int, entry: dict[str, Any]) -> str:
    value = entry.get("to")
    if value is None:
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


def _resolve_versions(
    file: Path,
    index: int,
    entry: dict[str, Any],
    from_list: list[str],
    defaults_versions: list[str] | None,
    language_prefix: str,
) -> list[str | None]:
    """Decide which version segments to expand across.

    Returns ``[None]`` as a sentinel when all ``from`` URLs are fully-qualified
    and no expansion across versions is needed (each ``None`` skips
    qualification in ``_qualify``).
    """
    explicit = entry.get("versions")
    all_qualified = all(not _is_path_only(f, language_prefix) for f in from_list)
    any_qualified = any(not _is_path_only(f, language_prefix) for f in from_list)
    any_path_only = any(_is_path_only(f, language_prefix) for f in from_list)

    if explicit is not None:
        if not isinstance(explicit, list):
            raise _err(
                file, index,
                f"'versions' must be a list, got {type(explicit).__name__}",
            )
        if not explicit:
            raise _err(file, index, "'versions' list cannot be empty")
        if any_qualified:
            raise _err(
                file, index,
                f"cannot mix fully-qualified 'from' (starts with "
                f"{language_prefix}/<version>/) with explicit 'versions:'",
            )
        return list(_validate_plain_versions(file, index, explicit))

    if all_qualified:
        return [None]

    if any_qualified and any_path_only:
        raise _err(
            file, index,
            "cannot mix fully-qualified and path-only 'from' values without "
            "explicit 'versions:'",
        )

    if defaults_versions is None:
        raise _err(
            file, index,
            "path-only 'from' requires per-entry 'versions:' or top-level "
            "'defaults.versions'",
        )
    return list(_validate_plain_versions(file, index, defaults_versions))


def _validate_plain_versions(file: Path, index: int, versions: list[Any]) -> list[str]:
    out: list[str] = []
    for v in versions:
        if not isinstance(v, str):
            raise _err(
                file, index,
                f"version identifier must be a string, got {type(v).__name__}",
            )
        if _is_pattern(v):
            raise _err(file, index, f"{_PATTERN_NOT_YET_SUPPORTED}; got {v!r}")
        out.append(v)
    return out


def is_external(url: str) -> bool:
    """True when ``url`` has a URL scheme or is protocol-relative.

    Covers absolute URLs (``https://docs.anyscale.com/x``), schemes without
    a host (``mailto:foo@example.com``, ``tel:+1234567890``), and
    protocol-relative URLs (``//cdn.example.com/x``). These targets are
    absolute and must not receive the project's language-prefix qualification.
    """
    parsed = urlparse(url)
    return bool(parsed.scheme or parsed.netloc)


def _is_path_only(url: str, language_prefix: str) -> bool:
    """True when ``url`` is an intra-project path that wants qualification.

    Returns ``False`` for both fully-qualified intra-project URLs (those
    starting with ``<language_prefix>/<version>/``) and URLs that are external
    to the project (``is_external``).
    """
    if is_external(url):
        return False
    pattern = rf"^{re.escape(language_prefix)}/[^/]+/"
    return not re.match(pattern, url)


def _qualify(url: str, version: str | None, language_prefix: str) -> str:
    """Prefix a path-only ``url`` with ``<language_prefix>/<version>``.

    Returns the URL unchanged when it's already fully-qualified, external, or
    when no version was provided.
    """
    if version is None or not _is_path_only(url, language_prefix):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return f"{language_prefix}/{version}{url}"


def _is_pattern(v: str) -> bool:
    """True when an identifier looks like a glob / range / exclusion / macro."""
    return any(c in v for c in "*<>=!@")


def _err(file: Path, index: int, message: str) -> ParseError:
    return ParseError(f"{file}: redirects[{index}]: {message}")
