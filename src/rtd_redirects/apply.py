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
from rtd_redirects.diff import Diff, diff
from rtd_redirects.model import RedirectSet


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


DEFAULT_MAX_PASSES = 10


@dataclass
class ConvergeResult:
    """Outcome of :func:`apply_converging`.

    ``result`` accumulates the per-operation counts across every pass.
    ``passes`` is how many ``apply`` passes ran. ``residual`` is the diff still
    outstanding after the final pass: empty when the live state converged to
    ``source``, non-empty when it did not.
    """

    result: ApplyResult
    passes: int
    residual: Diff

    @property
    def converged(self) -> bool:
        return self.residual.is_empty


def apply_converging(
    source: RedirectSet,
    client: RtdClient,
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    log: TextIO | None = None,
) -> ConvergeResult:
    """Apply ``source`` to RtD and reconcile until the live state matches it.

    A single :func:`apply` pass is not guaranteed to converge. RtD assigns a
    position on create and rewrites positions with insert-and-shift semantics,
    so records added or moved in one pass can land out of order relative to
    ``source`` — and a diff computed up front can't enumerate reorders for
    records that don't exist yet. This drives :func:`apply` repeatedly: each
    pass re-fetches live state, diffs it against ``source``, and applies the
    remainder, stopping when the diff is empty or ``max_passes`` is reached.

    The returned :class:`ConvergeResult` carries the cumulative counts and the
    final ``residual``. A non-empty residual means the apply did not converge;
    callers should surface that rather than report success, because an
    unconverged ordering can leave a specific rule shadowed by a more general
    one (RtD is strict first-match by position).
    """
    out = log or sys.stderr
    result = ApplyResult()
    residual: Diff = Diff()
    passes = 0

    while passes < max_passes:
        target = RedirectSet(client.list_redirects())
        residual = diff(source, target)
        if residual.is_empty:
            return ConvergeResult(result=result, passes=passes, residual=residual)
        pass_result = apply(residual, client, log=out)
        result.deleted += pass_result.deleted
        result.added += pass_result.added
        result.updated += pass_result.updated
        result.reordered += pass_result.reordered
        passes += 1

    # Final read so the caller learns whether the last pass settled the state.
    target = RedirectSet(client.list_redirects())
    residual = diff(source, target)
    return ConvergeResult(result=result, passes=passes, residual=residual)
