"""In-memory fakes of ``RtdClient`` that model RtD's position semantics.

RtD assigns and rewrites redirect positions with insert-and-shift behavior:
creating or moving a record renumbers the rest into a contiguous ``0..N-1``
sequence. A plain ``MagicMock`` can't capture that, but the convergence
behavior of :func:`rtd_redirects.apply.apply_converging` depends on it, so
tests that exercise multi-record ordering use these fakes instead.

Deliberate fidelity choices, matching what a real bulk apply did on the live
``anyscale-ray`` project:

- ``create_redirect`` appends. New records land at the tail rather than at
  their requested ``position`` — the condition that makes a single ``apply``
  pass leave added records out of order.
- ``update_redirect`` with a changed ``position`` removes the record and
  re-inserts it at the requested slot (clamped to the current length),
  shifting the rest.
- ``list_redirects`` returns copies whose ``position`` is the current index.
"""

from __future__ import annotations

from dataclasses import replace

from rtd_redirects.model import Redirect


class FakeRtd:
    """Stateful stand-in for ``RtdClient`` over an ordered list of records."""

    def __init__(self, initial: list[Redirect] | None = None) -> None:
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

    def create_redirect(self, r: Redirect) -> Redirect:
        created = replace(r, pk=self._new_pk())
        self._records.append(created)  # RtD lands new records at the tail
        return replace(created, position=len(self._records) - 1)

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
    claiming success.
    """

    def update_redirect(self, pk: int, r: Redirect) -> Redirect:
        idx = next(i for i, x in enumerate(self._records) if x.pk == pk)
        return replace(self._records[idx], position=idx)
