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
    "prefix",
    "sphinx_html",
    "sphinx_htmldir",
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
