"""Tests for rtd_redirects.diff_file: git-show-based PR-time diff."""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from rtd_redirects.diff_file import GitError, diff_file
from rtd_redirects.exceptions import ParseError


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with a 'base' branch and no commits yet."""
    _git("init", "-b", "base", cwd=tmp_path)
    _git("config", "user.email", "test@test.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    return tmp_path


def _commit(repo: Path, name: str, content: str) -> str:
    """Write a file, commit it, return the commit SHA."""
    (repo / name).write_text(dedent(content).lstrip("\n"))
    _git("add", name, cwd=repo)
    _git("commit", "-m", f"update {name}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _yaml(*entries: str) -> str:
    body = "\n".join(f"  - {e}" for e in entries)
    return f"schema_version: 1\nredirects:\n{body}\n"


_E_A = 'from: /a.html\n    to:   /b.html\n    type: exact'
_E_C = 'from: /c.html\n    to:   /d.html\n    type: exact'
_E_A_NEW = 'from: /a.html\n    to:   /b-new.html\n    type: exact'


class TestUnchanged:
    def test_no_changes_yields_empty_diff(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=base, repo_path=repo)
        assert d.is_empty


class TestPureAdds:
    def test_file_new_at_head(self, repo: Path):
        base = _commit(repo, "other.yaml", "schema_version: 1\nredirects: []\n")
        head = _commit(repo, "redirects.yaml", _yaml(_E_A))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert len(d.adds) == 1
        assert d.adds[0].from_url == "/a.html"
        assert not d.deletes and not d.updates and not d.reorders

    def test_entries_added_to_existing_file(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        head = _commit(repo, "redirects.yaml", _yaml(_E_A, _E_C))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert [r.from_url for r in d.adds] == ["/c.html"]


class TestPureDeletes:
    def test_file_deleted_at_head(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        (repo / "redirects.yaml").unlink()
        _git("add", "-A", cwd=repo)
        _git("commit", "-m", "remove", cwd=repo)
        head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert len(d.deletes) == 1
        assert d.deletes[0].from_url == "/a.html"

    def test_entries_removed_from_existing_file(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A, _E_C))
        head = _commit(repo, "redirects.yaml", _yaml(_E_A))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert [r.from_url for r in d.deletes] == ["/c.html"]


class TestUpdates:
    def test_to_url_change_is_update(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        head = _commit(repo, "redirects.yaml", _yaml(_E_A_NEW))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert len(d.updates) == 1
        u = d.updates[0]
        assert u.source.to_url == "/b-new.html"
        assert u.target.to_url == "/b.html"


class TestMixed:
    def test_add_and_delete_and_update(self, repo: Path):
        # Base: A (canonical), C (will be deleted)
        base = _commit(repo, "redirects.yaml", _yaml(_E_A, _E_C))
        # Head: A_NEW (update of A), <new entry replacing C with different from>
        new_entry = 'from: /new.html\n    to:   /target.html\n    type: exact'
        head = _commit(repo, "redirects.yaml", _yaml(_E_A_NEW, new_entry))
        d = diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
        assert [r.from_url for r in d.adds] == ["/new.html"]
        assert [r.from_url for r in d.deletes] == ["/c.html"]
        assert [u.source.from_url for u in d.updates] == ["/a.html"]


class TestMultiFile:
    """Ordered composition across refs — the master.yaml + current.yaml case."""

    def _commit_files(self, repo: Path, files: dict[str, str], msg: str) -> str:
        for name, content in files.items():
            (repo / name).write_text(dedent(content).lstrip("\n"))
            _git("add", name, cwd=repo)
        _git("commit", "-m", msg, cwd=repo)
        return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    def test_change_to_one_file_diffs_against_composed_set(self, repo: Path):
        master = "schema_version: 1\nredirects: []\n"
        current = _yaml(_E_A)
        base = self._commit_files(
            repo, {"master.yaml": master, "current.yaml": current}, "base",
        )
        # Head adds a rule to master.yaml only.
        master_head = _yaml('from: /m.html\n    to:   /n.html\n    type: exact')
        head = self._commit_files(repo, {"master.yaml": master_head}, "stage master")

        d = diff_file(
            ["master.yaml", "current.yaml"],
            base_ref=base, head_ref=head, repo_path=repo,
        )
        # New master rule lands at position 0; current's /a.html shifts to 1.
        assert [r.from_url for r in d.adds] == ["/m.html"]
        assert [u.source.from_url for u in d.reorders] == ["/a.html"]
        assert not d.deletes and not d.updates

    def test_new_master_file_at_head_composes(self, repo: Path):
        # Base has only current.yaml; head adds master.yaml ahead of it.
        base = self._commit_files(repo, {"current.yaml": _yaml(_E_A)}, "base")
        master = _yaml('from: /staged.html\n    to:   /dest.html\n    type: exact')
        head = self._commit_files(repo, {"master.yaml": master}, "add master")

        d = diff_file(
            ["master.yaml", "current.yaml"],
            base_ref=base, head_ref=head, repo_path=repo,
        )
        # The staged master rule is the add; /a.html shifts down one position as
        # the higher-priority master rule composes ahead of it.
        assert [r.from_url for r in d.adds] == ["/staged.html"]
        assert [u.source.from_url for u in d.reorders] == ["/a.html"]
        assert not d.deletes and not d.updates

    def test_composed_order_is_master_before_current(self, repo: Path):
        """Composing master ahead of current gives the master rule position 0 and
        shifts current's rule down — RtD insert-and-shift first-match semantics.
        Staging a master rule is an add of that rule plus a reorder of the
        current rule it now precedes.
        """
        master = _yaml('from: /en/master/x.html\n    to:   /en/master/y.html\n    type: exact')
        current = _yaml(
            'from: /old/*\n    to:   /new/:splat\n    type: page',
        )
        base = self._commit_files(
            repo, {"master.yaml": "schema_version: 1\nredirects: []\n",
                   "current.yaml": current}, "base",
        )
        head = self._commit_files(repo, {"master.yaml": master}, "stage master")

        d = diff_file(
            ["master.yaml", "current.yaml"],
            base_ref=base, head_ref=head, repo_path=repo,
        )
        assert [r.from_url for r in d.adds] == ["/en/master/x.html"]
        # The master rule lands at position 0; /old/* shifts from 0 to 1.
        assert d.adds[0].position == 0
        assert [u.source.from_url for u in d.reorders] == ["/old/*"]
        reorder = d.reorders[0]
        assert reorder.target.position == 0 and reorder.source.position == 1
        assert not d.updates and not d.deletes


class TestErrors:
    def test_invalid_ref_raises_git_error(self, repo: Path):
        _commit(repo, "redirects.yaml", _yaml(_E_A))
        with pytest.raises(GitError, match="git show"):
            diff_file(
                "redirects.yaml",
                base_ref="nonexistent-ref-zzz",
                head_ref="HEAD",
                repo_path=repo,
            )

    def test_malformed_yaml_at_ref_raises_parse_error(self, repo: Path):
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        head = _commit(repo, "redirects.yaml", "schema_version: 1\nredirects:\n  - {oops\n")
        with pytest.raises(ParseError, match="YAML parse error"):
            diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)

    def test_parse_error_includes_ref_in_source(self, repo: Path):
        """Error messages name the ref so the author knows which side broke."""
        base = _commit(repo, "redirects.yaml", _yaml(_E_A))
        # head has an entry missing required fields
        bad = "schema_version: 1\nredirects:\n  - from: /x\n    type: exact\n"
        head = _commit(repo, "redirects.yaml", bad)
        with pytest.raises(ParseError, match=f"{head}:redirects.yaml"):
            diff_file("redirects.yaml", base_ref=base, head_ref=head, repo_path=repo)
