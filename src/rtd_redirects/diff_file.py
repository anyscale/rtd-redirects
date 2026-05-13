"""Compute a redirect-level diff between two git refs of a YAML file.

The engine behind the ``diff-file`` subcommand and the PR-time CI check.
Reads the YAML at ``base_ref`` and ``head_ref`` via ``git show``, parses
both through ``parse_text``, and returns a ``Diff`` that describes what
the PR proposes — no RtD API calls, so the check stays independent of
external service health and can run on PRs that have no RtD credentials.

Files that don't exist at a given ref (e.g. new file in head, deleted in
head) are treated as empty so the diff cleanly shows pure adds or pure
deletes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rtd_redirects.diff import Diff, diff
from rtd_redirects.model import RedirectSet
from rtd_redirects.parse import parse_text


class GitError(Exception):
    """Raised when git itself fails (missing binary, bad ref, etc.)."""


def diff_file(
    file_path: str | Path,
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

    ``file_path`` must be relative to the repo root (git's ``<ref>:<path>``
    syntax doesn't accept absolute paths). ``repo_path`` selects the
    repository to query; ``None`` uses the current working directory.
    """
    file_path = Path(file_path)
    base_text = _read_at_ref(file_path, base_ref, repo_path)
    head_text = _read_at_ref(file_path, head_ref, repo_path)

    base_set = (
        parse_text(base_text, source=f"{base_ref}:{file_path}")
        if base_text is not None
        else RedirectSet()
    )
    head_set = (
        parse_text(head_text, source=f"{head_ref}:{file_path}")
        if head_text is not None
        else RedirectSet()
    )

    return diff(head_set, base_set)


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
