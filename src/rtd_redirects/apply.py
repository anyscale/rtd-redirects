"""Apply a computed Diff to an RtD project.

Drives the plan against ``RtdClient`` in the safe order documented in
design.md §Architecture: deletes, adds, updates, reorders. Each operation
logs a single line to ``stderr`` (or any file-like passed via ``log=``) so a
CI run produces a per-entry audit trail.

Re-running on a synced state is a no-op: the diff against current RtD state
contains nothing to apply. If killed mid-apply, the next run recomputes the
diff and resumes from whatever wasn't applied — operations are idempotent
at the per-record level.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from rtd_redirects.client import RtdClient
from rtd_redirects.diff import Diff


@dataclass
class ApplyResult:
    """Counts of operations actually applied during a single ``apply`` run."""

    deleted: int = 0
    added: int = 0
    updated: int = 0
    reordered: int = 0

    @property
    def total(self) -> int:
        return self.deleted + self.added + self.updated + self.reordered


def apply(d: Diff, client: RtdClient, *, log: TextIO | None = None) -> ApplyResult:
    """Apply the Diff to the RtD project via ``client``.

    Operations run in order — deletes, adds, updates, reorders — so that
    space is freed before new records land, data settles before positions
    shuffle. Any exception raised by ``client`` propagates and stops the run;
    the caller can fix the underlying issue and rerun.
    """
    out = log or sys.stderr
    result = ApplyResult()

    for r in d.deletes:
        if r.pk is None:
            raise ValueError(f"cannot delete record without pk: {r.identity}")
        client.delete_redirect(r.pk)
        print(f"DELETE {r.from_url} ({r.type}) pk={r.pk}", file=out)
        result.deleted += 1

    for r in d.adds:
        created = client.create_redirect(r)
        print(
            f"CREATE {r.from_url} -> {r.to_url} ({r.type}) pk={created.pk}",
            file=out,
        )
        result.added += 1

    for u in d.updates:
        if u.target.pk is None:
            raise ValueError(f"cannot update record without pk: {u.target.identity}")
        client.update_redirect(u.target.pk, u.source)
        print(
            f"UPDATE {u.source.from_url} ({u.source.type}) pk={u.target.pk}",
            file=out,
        )
        result.updated += 1

    for u in d.reorders:
        if u.target.pk is None:
            raise ValueError(f"cannot reorder record without pk: {u.target.identity}")
        client.update_redirect(u.target.pk, u.source)
        print(
            f"REORDER {u.source.from_url} ({u.source.type}) "
            f"position={u.source.position} pk={u.target.pk}",
            file=out,
        )
        result.reordered += 1

    return result
