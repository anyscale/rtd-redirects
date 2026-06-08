"""Compute the diff between two RedirectSets.

Given a source set (what the YAML wants) and a target set (what RtD has),
produce four categories of change:

- **adds**: records present in source but not in target -> POST.
- **deletes**: records present in target but not in source -> DELETE.
- **updates**: identity matches in both, at least one non-position field
  differs -> PUT/PATCH with the source's fields.
- **reorders**: identity matches, every non-position field is equal, only
  ``position`` differs. Held separately so the apply layer can batch them in
  a final pass and avoid churning the RtD position counter during data-level
  changes.

Identity is the ``(from_url, type)`` tuple, matching ``RedirectSet``'s
internal keying. ``pk`` is excluded from equality (it's the RtD-side primary
key, often absent on the YAML side), so a YAML-parsed source and an
API-fetched target compare cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from rtd_redirects.model import Redirect, RedirectSet


@dataclass(frozen=True)
class Update:
    """A change to an existing record.

    ``target`` is what's currently in RtD (carries the ``pk`` the apply layer
    needs to address the record). ``source`` is the new state to write.
    """

    target: Redirect
    source: Redirect


@dataclass(frozen=True)
class Diff:
    """Structured diff between two ``RedirectSet``s."""

    adds: list[Redirect] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    deletes: list[Redirect] = field(default_factory=list)
    reorders: list[Update] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.adds or self.updates or self.deletes or self.reorders)

    def __len__(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.deletes) + len(self.reorders)


def diff(source: RedirectSet, target: RedirectSet) -> Diff:
    """Compute the diff that transforms ``target`` into ``source``.

    Identity is ``(from_url, type)``. Records with identical identity and all
    non-``pk`` data fields equal are no-ops. Identity-equal records that
    differ only in ``position`` are reorders; any other field difference is
    an update.
    """
    source_ids = source.identities()
    target_ids = target.identities()

    adds = [source.get(i) for i in (source_ids - target_ids)]
    deletes = [target.get(i) for i in (target_ids - source_ids)]

    updates: list[Update] = []
    reorders: list[Update] = []
    for identity in source_ids & target_ids:
        s = source.get(identity)
        t = target.get(identity)
        if s == t:
            continue
        if _equal_except_position(s, t):
            reorders.append(Update(target=t, source=s))
        else:
            updates.append(Update(target=t, source=s))

    adds.sort(key=lambda r: r.identity)
    deletes.sort(key=lambda r: r.identity)
    updates.sort(key=lambda u: u.source.identity)
    # Order reorders by ascending target position (identity breaks ties for
    # determinism). RtD rewrites positions with insert-and-shift semantics, so
    # settling the lowest slot first lets each write land without disturbing
    # already-placed lower slots; sorting by identity can leave a multi-record
    # batch unconverged after a single pass.
    reorders.sort(key=lambda u: (u.source.position, u.source.identity))

    return Diff(adds=adds, updates=updates, deletes=deletes, reorders=reorders)


def _equal_except_position(a: Redirect, b: Redirect) -> bool:
    """True when ``a`` and ``b`` match in every comparable field except ``position``."""
    return replace(a, position=b.position) == b
