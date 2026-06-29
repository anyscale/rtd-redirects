"""Compute a redirect-level diff between two git refs of YAML file(s).

The engine behind the ``diff-file`` subcommand and the PR-time CI check.
Reads the YAML at ``base_ref`` and ``head_ref`` via ``git show``, parses
both through ``parse_text``, and returns a ``Diff`` that describes what
the PR proposes — no RtD API calls, so the check stays independent of
external service health and can run on PRs that have no RtD credentials.

Accepts an ordered list of files and composes each ref's set the same way
``parse_files`` does: earlier files position before later ones, globally
reindexed. This is how a PR that touches ``master.yaml`` and ``current.yaml``
gets diffed against the composed live order rather than each file in
isolation. A single file keeps its authored positions untouched.

Files that don't exist at a given ref (e.g. new file in head, deleted in
head) are treated as empty so the diff cleanly shows pure adds or pure
deletes.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from rtd_redirects.diff import Diff, diff
from rtd_redirects.model import RedirectSet
from rtd_redirects.parse import compose, parse_text


class GitError(Exception):
    """Raised when git itself fails (missing binary, bad ref, etc.)."""


def diff_file(
    file_paths: str | Path | Sequence[str | Path],
    *,
    base_ref: str = "origin/master",
    head_ref: str = "HEAD",
    repo_path: str | Path | None = None,
) -> Diff:
    """Compute the redirect-level diff from ``base_ref`` to ``head_ref``.

    Returns a ``Diff`` where:

    - ``adds`` lists records the PR adds (in head, not in base),
    - ``deletes`` lists records the PR removes,
    - ``updates`` lists records the PR modifies in any non-position field,
    - ``reorders`` lists records whose position the PR changes.

    ``file_paths`` is a single path or an ordered list of paths, each relative
    to the repo root (git's ``<ref>:<path>`` syntax doesn't accept absolute
    paths). Multiple files compose in the given order on both sides before
    diffing. ``repo_path`` selects the repository to query; ``None`` uses the
    current working directory.
    """
    if isinstance(file_paths, (str, Path)):
        paths = [Path(file_paths)]
    else:
        paths = [Path(p) for p in file_paths]

    base_set = _compose_at_ref(paths, base_ref, repo_path)
    head_set = _compose_at_ref(paths, head_ref, repo_path)

    return diff(head_set, base_set)


def _compose_at_ref(
    paths: Sequence[Path],
    ref: str,
    repo_path: str | Path | None,
) -> RedirectSet:
    """Parse and compose every path that exists at ``ref`` into one set.

    Paths missing at the ref are skipped (treated as empty), so a file added or
    deleted by the PR shows as pure adds or deletes. A single present file keeps
    its authored positions; multiple compose via :func:`compose` in list order.
    """
    named: list[tuple[str, RedirectSet]] = []
    for path in paths:
        text = _read_at_ref(path, ref, repo_path)
        if text is None:
            continue
        label = f"{ref}:{path}"
        named.append((label, parse_text(text, source=label)))

    if not named:
        return RedirectSet()
    if len(named) == 1:
        return named[0][1]
    return compose(named)


_MISSING_PATH_MARKERS = (
    "does not exist",
    "exists on disk, but not in",
    "not found in",
)


def _read_at_ref(
    file_path: Path,
    ref: str,
    repo_path: str | Path | None,
) -> str | None:
    """Return file content at ``ref``, or ``None`` if the path doesn't exist there."""
    if not shutil.which("git"):
        raise GitError("git is not on PATH")

    cmd = ["git"]
    if repo_path is not None:
        cmd.extend(["-C", str(repo_path)])
    cmd.extend(["show", f"{ref}:{file_path}"])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout

    stderr = result.stderr.lower()
    if any(marker in stderr for marker in _MISSING_PATH_MARKERS):
        return None

    raise GitError(
        f"git show {ref}:{file_path} failed (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )
