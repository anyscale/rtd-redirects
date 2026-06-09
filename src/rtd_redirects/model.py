"""Canonical Redirect record and RedirectSet collection.

Identity for diff matching is the ``(from_url, type)`` pair. Equality across
all data fields captures whether two records are field-by-field the same; the
API-side primary key ``pk`` is excluded from equality and ``repr`` so a
YAML-parsed record can compare cleanly against a record fetched from RtD.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

REDIRECT_TYPES: frozenset[str] = frozenset({
    "page",
    "exact",
    "clean_url_to_html",
    "html_to_clean_url",
})

# Types that RtD applies across all versions automatically. These don't use
# our YAML-side ``defaults.versions`` or ``versions:`` expansion; the redirect
# applies project-wide on RtD's side regardless.
VERSION_AGNOSTIC_TYPES: frozenset[str] = frozenset({
    "page",
    "clean_url_to_html",
    "html_to_clean_url",
})

# Types where RtD doesn't require ``from_url`` / ``to_url`` on the API. They
# describe a URL-style transition for the whole project, not a per-page rule.
URL_STYLE_TYPES: frozenset[str] = frozenset({
    "clean_url_to_html",
    "html_to_clean_url",
})


@dataclass
class Redirect:
    """One RtD v3 redirect record."""

    from_url: str
    to_url: str
    type: str
    http_status: int = 301
    force: bool = False
    enabled: bool = True
    position: int = 0
    description: str = ""
    pk: int | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.type not in REDIRECT_TYPES:
            raise ValueError(
                f"Invalid redirect type {self.type!r}; "
                f"expected one of {sorted(REDIRECT_TYPES)}"
            )
        if not 300 <= self.http_status < 400:
            raise ValueError(
                f"http_status must be a 3xx code; got {self.http_status}"
            )
        if self.position < 0:
            raise ValueError(f"position must be non-negative; got {self.position}")

    @property
    def identity(self) -> tuple[str, str]:
        """Identity used for diff matching: ``(from_url, type)``."""
        return (self.from_url, self.type)


@dataclass(frozen=True)
class DuplicateGroup:
    """Live API records that share one ``(from_url, type)`` identity.

    RtD's v3 API doesn't enforce identity uniqueness, but ``RedirectSet`` keys
    on identity, so :meth:`RedirectSet.from_api` keeps one record and sets the
    rest aside here. ``kept`` is the record RtD actually serves — the lowest
    ``position`` under RtD's strict first-match rule. ``shadowed`` are the
    unreachable extras, in position order.
    """

    identity: tuple[str, str]
    kept: Redirect
    shadowed: list[Redirect]

    @property
    def same_target(self) -> bool:
        """True when every shadowed record shares ``kept``'s ``to_url``.

        ``False`` is the dangerous case: the identity resolves to a different
        destination than a shadowed duplicate intended, so the live redirect
        silently serves the wrong target.
        """
        return all(s.to_url == self.kept.to_url for s in self.shadowed)


class RedirectSet:
    """Ordered collection of ``Redirect`` records, keyed by identity.

    Iteration yields records sorted by ``position``. ``add`` rejects identity
    collisions so duplicate YAML entries fail loudly at parse time; use
    ``replace`` to upsert a record from the API or after expansion.
    """

    def __init__(self, redirects: Iterable[Redirect] = ()) -> None:
        self._by_identity: dict[tuple[str, str], Redirect] = {}
        for r in redirects:
            self.add(r)

    @classmethod
    def from_api(
        cls, records: Iterable[Redirect]
    ) -> tuple[RedirectSet, list[DuplicateGroup]]:
        """Build a set from live API records, tolerating duplicate identities.

        RtD's v3 API doesn't enforce ``(from_url, type)`` uniqueness: the
        dashboard can create duplicate redirects, and a retried POST could too.
        The constructor and ``add`` reject duplicates so *authored* YAML fails
        loudly, but live API data must not crash the read path.

        Keeps the lowest-``position`` record per identity — the one RtD serves
        under strict first-match — and returns the shadowed extras as
        ``DuplicateGroup`` records. Never silently drops a record: every
        duplicate is reported so callers can warn, treat it as drift, or delete
        it. Groups are sorted by identity for stable output.
        """
        grouped: dict[tuple[str, str], list[Redirect]] = {}
        for r in records:
            grouped.setdefault(r.identity, []).append(r)

        kept: list[Redirect] = []
        groups: list[DuplicateGroup] = []
        for identity, recs in grouped.items():
            ordered = sorted(recs, key=lambda r: (r.position, r.pk or 0))
            kept.append(ordered[0])
            if len(ordered) > 1:
                groups.append(DuplicateGroup(
                    identity=identity, kept=ordered[0], shadowed=ordered[1:],
                ))

        groups.sort(key=lambda g: g.identity)
        # kept identities are unique by construction; the constructor re-checks.
        return cls(kept), groups

    def add(self, r: Redirect) -> None:
        if r.identity in self._by_identity:
            raise ValueError(f"Duplicate identity: {r.identity}")
        self._by_identity[r.identity] = r

    def replace(self, r: Redirect) -> None:
        """Add or overwrite the record with this identity."""
        self._by_identity[r.identity] = r

    def remove(self, identity: tuple[str, str]) -> Redirect:
        return self._by_identity.pop(identity)

    def get(self, identity: tuple[str, str]) -> Redirect | None:
        return self._by_identity.get(identity)

    def identities(self) -> set[tuple[str, str]]:
        return set(self._by_identity)

    def __contains__(self, identity: object) -> bool:
        return identity in self._by_identity

    def __iter__(self) -> Iterator[Redirect]:
        return iter(sorted(self._by_identity.values(), key=lambda r: r.position))

    def __len__(self) -> int:
        return len(self._by_identity)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RedirectSet):
            return NotImplemented
        return self._by_identity == other._by_identity

    def __repr__(self) -> str:
        return f"RedirectSet({list(self)!r})"
