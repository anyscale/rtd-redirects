"""Dump-time heuristic that groups canonical records back into ergonomic YAML entries.

The inverse of ``expand.py``: takes a flat list of canonical ``Redirect``
records (typically pulled from the RtD API via ``RtdClient.list_redirects``)
and produces a list of entry dicts ready for YAML serialization. Records
sharing every data field except ``from_url`` collapse into a single entry
with ``from:`` as a list; singletons emit as 1:1.

Collapse is lossy in shape but round-trip safe in canonical form: the
canonical ``RedirectSet`` obtained by parsing the collapsed YAML equals the
input set. ``--no-collapse`` callers can iterate the input directly instead
of routing through this module.

Tier 1 (this iteration): multi-source collapse only. Tier 2 (follow-up
PR): multi-version collapse — records that differ only in their language /
version prefix factored into a single entry with ``versions:`` as a list and
path-relative ``from:`` / ``to:``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from rtd_redirects.model import Redirect


def collapse(redirects: Iterable[Redirect]) -> list[dict[str, Any]]:
    """Group canonical Redirects into ergonomic YAML entries.

    Returns a list of dicts in the shape that ``parse.py`` expects: ``from``,
    ``to``, ``type``, and optional non-default fields. Records sharing every
    field except ``from_url`` collapse into one entry with ``from:`` as a
    sorted list. Singletons emit as 1:1.

    Entries are returned sorted by source ``position`` then by ``to_url`` for
    deterministic output. ``position:`` is included explicitly only when it
    differs from the entry's eventual index, so round-tripping preserves
    ordering without bloating the YAML.
    """
    groups: dict[tuple[Any, ...], list[Redirect]] = defaultdict(list)
    for r in redirects:
        key = (
            r.type,
            r.to_url,
            r.http_status,
            r.force,
            r.enabled,
            r.description,
            r.position,
        )
        groups[key].append(r)

    pending: list[tuple[int, str, dict[str, Any]]] = []
    for key, group in groups.items():
        type_, to_url, http_status, force, enabled, description, position = key
        from_urls = sorted(r.from_url for r in group)

        entry: dict[str, Any] = {
            "from": from_urls[0] if len(from_urls) == 1 else from_urls,
            "to": to_url,
            "type": type_,
        }
        if http_status != 301:
            entry["status"] = http_status
        if force:
            entry["force"] = True
        if not enabled:
            entry["enabled"] = False
        if description:
            entry["description"] = description

        pending.append((position, to_url, entry))

    pending.sort(key=lambda t: (t[0], t[1]))

    entries: list[dict[str, Any]] = []
    for index, (position, _, entry) in enumerate(pending):
        if position != index:
            entry["position"] = position
        entries.append(entry)

    return entries
