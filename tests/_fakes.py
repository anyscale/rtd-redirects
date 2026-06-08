"""In-memory fakes of ``RtdClient`` that model RtD's position semantics.

RtD assigns and rewrites redirect positions with insert-and-shift behavior:
creating or moving a record renumbers the rest into a contiguous ``0..N-1``
sequence. A plain ``MagicMock`` can't capture that, but the convergence
behavior of :func:`rtd_redirects.apply.apply_converging` depends on it.

The slot RtD gives a *newly created* record isn't pinned down. ``_to_api``
sends ``position`` on create, and the dashboard inserts new redirects at the
top by default — so position is honorable on create, but the effective default
varies. The bulk API apply against ``anyscale-ray`` left new records at the
tail, which is what ``"honor"`` mode reproduces: a batch whose target positions
sit past the current end clamps to the end and degenerates to tail-append.

``create_mode`` lets a test pick a placement so the suite can prove the
converging driver reconciles the live state regardless of where creates land:

- ``"honor"`` (default): insert at the requested ``position`` (clamped to the
  current length), shifting the rest.
- ``"append"``: new records always land at the tail.
- ``"prepend"``: new records always land at position 0 (the UI default).

``update_redirect`` always honors the requested position (clamped) with
insert-and-shift. ``list_redirects`` returns copies whose ``position`` is the
current index.
"""

from __future__ import annotations

from dataclasses import replace

from rtd_redirects.model import Redirect


class FakeRtd:
    """Stateful stand-in for ``RtdClient`` over an ordered list of records."""

    def __init__(
        self, initial: list[Redirect] | None = None, *, create_mode: str = "honor"
    ) -> None:
        self.create_mode = create_mode
        self._records: list[Redirect] = []
        self._next_pk = 1000
        for r in initial or []:
            pk = r.pk if r.pk is not None else self._new_pk()
            self._records.append(replace(r, pk=pk))

    def _new_pk(self) -> int:
        self._next_pk += 1
        return self._next_pk

    def list_redirects(self) -> list[Redirect]:
        return [replace(r, position=i) for i, r in enumerate(self._records)]

    def _create_slot(self, requested: int) -> int:
        if self.create_mode == "append":
            return len(self._records)
        if self.create_mode == "prepend":
            return 0
        return min(requested, len(self._records))

    def create_redirect(self, r: Redirect) -> Redirect:
        created = replace(r, pk=self._new_pk())
        slot = self._create_slot(r.position)
        self._records.insert(slot, created)
        return replace(created, position=slot)

    def update_redirect(self, pk: int, r: Redirect) -> Redirect:
        idx = next(i for i, x in enumerate(self._records) if x.pk == pk)
        self._records.pop(idx)
        slot = min(r.position, len(self._records))
        moved = replace(r, pk=pk)
        self._records.insert(slot, moved)  # insert-and-shift
        return replace(moved, position=slot)

    def delete_redirect(self, pk: int) -> None:
        self._records = [x for x in self._records if x.pk != pk]


class StuckRtd(FakeRtd):
    """``FakeRtd`` whose position writes never take effect.

    Models a client that can't converge, so tests can verify the driver stops
    after ``max_passes`` and reports the residual instead of looping forever or
    claiming success. Creates append (so the added records start out of order
    and reorders are needed), but those reorders are no-ops.
    """

    def __init__(self, initial: list[Redirect] | None = None) -> None:
        super().__init__(initial, create_mode="append")

    def update_redirect(self, pk: int, r: Redirect) -> Redirect:
        idx = next(i for i, x in enumerate(self._records) if x.pk == pk)
        return replace(self._records[idx], position=idx)
