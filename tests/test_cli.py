"""Tests for rtd_redirects.cli end-to-end."""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml

from rtd_redirects.cli import EXIT_DRIFT, EXIT_OK, EXIT_PARSE, EXIT_RTD, main
from rtd_redirects.client import RtdAuthError, RtdClient
from rtd_redirects.model import Redirect


def _r(from_url: str, to_url: str = "/dest", *, pk: int | None = None) -> Redirect:
    return Redirect(from_url=from_url, to_url=to_url, type="exact", pk=pk)


@pytest.fixture
def mock_client() -> MagicMock:
    c = MagicMock(spec=RtdClient)
    c.list_redirects.return_value = []
    c.create_redirect.side_effect = lambda r: _r(r.from_url, r.to_url, pk=99)
    c.update_redirect.side_effect = lambda pk, r: _r(r.from_url, r.to_url, pk=pk)
    return c


@pytest.fixture
def factory(mock_client: MagicMock):
    """Factory that ignores its arg and returns the same mock client."""
    return lambda _project: mock_client


def _write_yaml(path: Path, body: str) -> Path:
    path.write_text(dedent(body).lstrip("\n"))
    return path


class TestList:
    def test_lists_redirects_from_client(
        self, mock_client: MagicMock, factory, capsys: pytest.CaptureFixture,
    ):
        mock_client.list_redirects.return_value = [
            _r("/a", "/b", pk=1),
            _r("/c", "/d", pk=2),
        ]
        rc = main(["list", "--project", "test"], client_factory=factory)
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "/a -> /b" in out
        assert "/c -> /d" in out
        assert "pk=1" in out

    def test_project_from_env(
        self, mock_client: MagicMock, factory,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        monkeypatch.setenv("RTD_PROJECT_SLUG", "from-env")
        called_with: list[str] = []
        rc = main(["list"], client_factory=lambda slug: (
            called_with.append(slug) or mock_client
        ))
        assert rc == EXIT_OK
        assert called_with == ["from-env"]

    def test_missing_project_raises(
        self, factory, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("RTD_PROJECT_SLUG", raising=False)
        with pytest.raises(SystemExit, match="--project required"):
            main(["list"], client_factory=factory)


class TestDump:
    def test_dump_writes_yaml_to_file(
        self, tmp_path: Path, mock_client: MagicMock, factory,
    ):
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        out_path = tmp_path / "out.yaml"
        rc = main(
            ["dump", "--project", "p", "--output", str(out_path)],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        doc = yaml.safe_load(out_path.read_text())
        assert doc["schema_version"] == 1
        assert doc["redirects"][0]["from"] == "/a"

    def test_dump_to_stdout_when_no_output(
        self, mock_client: MagicMock, factory, capsys: pytest.CaptureFixture,
    ):
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        rc = main(["dump", "--project", "p"], client_factory=factory)
        assert rc == EXIT_OK
        doc = yaml.safe_load(capsys.readouterr().out)
        assert doc["redirects"][0]["to"] == "/b"


class TestPlan:
    def test_no_changes_exits_ok_and_says_so(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        assert "no changes" in capsys.readouterr().err

    def test_shows_diff_summary(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
              - from: /new
                to:   /target
                type: exact
        """)
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "+ /new -> /target" in out
        assert "1 add" in out


class TestDiffFile:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init", "-b", "base"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
        return tmp_path

    def test_unchanged_yields_zero_summary(
        self, repo: Path, factory, capsys: pytest.CaptureFixture,
    ):
        (repo / "r.yaml").write_text(
            "schema_version: 1\nredirects:\n  - from: /a\n    to: /b\n    type: exact\n",
        )
        subprocess.run(["git", "add", "r.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        rc = main(
            [
                "diff-file", "--file", "r.yaml",
                "--base", "HEAD", "--head", "HEAD", "--repo", str(repo),
            ],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "0 add, 0 update, 0 delete, 0 reorder" in out


class TestApply:
    def test_no_changes_short_circuits(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--yes"],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        assert "no changes" in capsys.readouterr().err
        mock_client.create_redirect.assert_not_called()

    def test_yes_flag_skips_confirmation_and_applies(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = []  # nothing in RtD; add /a
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--yes"],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        mock_client.create_redirect.assert_called_once()
        assert "applied: 0 deleted, 1 added" in capsys.readouterr().err

    def test_interactive_yes_applies(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = []
        monkeypatch.setattr("builtins.input", lambda _: "y")
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        mock_client.create_redirect.assert_called_once()

    def test_interactive_no_aborts(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = []
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc != EXIT_OK
        mock_client.create_redirect.assert_not_called()

    def test_no_tty_without_yes_fails(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = []

        def _raise_eof(_):
            raise EOFError()

        monkeypatch.setattr("builtins.input", _raise_eof)
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc != EXIT_OK
        assert "not a tty" in capsys.readouterr().err


class TestAudit:
    def test_no_drift_exits_ok(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        mock_client.list_redirects.return_value = [_r("/a", "/b", pk=1)]
        rc = main(
            ["audit", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        assert "no drift" in capsys.readouterr().err

    def test_drift_exits_nonzero(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        # RtD has an extra record not in YAML -> drift
        mock_client.list_redirects.return_value = [
            _r("/a", "/b", pk=1),
            _r("/drift", "/target", pk=2),
        ]
        rc = main(
            ["audit", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_DRIFT
        err = capsys.readouterr().err
        assert "drift detected" in err
        assert "- /drift" in err


class TestErrorHandling:
    def test_parse_error_returns_exit_parse(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", "schema_version: 99\nredirects: []\n")
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_PARSE
        assert "error: parse:" in capsys.readouterr().err

    def test_rtd_auth_error_returns_exit_rtd(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects: []
        """)
        mock_client.list_redirects.side_effect = RtdAuthError("token bad")
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_RTD
        assert "error: rtd:" in capsys.readouterr().err

    def test_missing_file_returns_exit_parse(
        self, factory, capsys: pytest.CaptureFixture,
    ):
        rc = main(
            ["plan", "--project", "p", "--file", "/nonexistent/path.yaml"],
            client_factory=factory,
        )
        assert rc == EXIT_PARSE
