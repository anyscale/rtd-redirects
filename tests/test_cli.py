"""Tests for rtd_redirects.cli end-to-end."""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml

from rtd_redirects.cli import (
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_PARSE,
    EXIT_RTD,
    EXIT_VALIDATION,
    main,
)
from rtd_redirects.client import RtdAuthError, RtdClient
from rtd_redirects.model import Redirect

from ._fakes import FakeRtd, StuckRtd


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

    def test_multi_file_composes_in_order(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        """``--file master.yaml current.yaml`` composes both as one source."""
        master = _write_yaml(tmp_path / "master.yaml", """
            schema_version: 1
            redirects:
              - from: /en/master/x.html
                to:   /en/master/y.html
                type: exact
        """)
        current = _write_yaml(tmp_path / "current.yaml", """
            schema_version: 1
            redirects:
              - from: /old/*
                to:   /new/:splat
                type: page
        """)
        mock_client.list_redirects.return_value = []
        rc = main(
            ["plan", "--project", "p", "--file", str(master), str(current)],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # Both files' rules show as adds against the empty live project.
        assert "+ /en/master/x.html" in out
        assert "+ /old/*" in out
        assert "2 add" in out


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

    def test_multi_file_diff_composes(
        self, repo: Path, factory, capsys: pytest.CaptureFixture,
    ):
        """diff-file accepts an ordered file list and composes both sides."""
        (repo / "master.yaml").write_text("schema_version: 1\nredirects: []\n")
        (repo / "current.yaml").write_text(
            "schema_version: 1\nredirects:\n  - from: /a\n    to: /b\n    type: exact\n",
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        (repo / "master.yaml").write_text(
            "schema_version: 1\nredirects:\n  - from: /m\n    to: /n\n    type: exact\n",
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "stage"], cwd=repo, check=True, capture_output=True,
        )
        rc = main(
            [
                "diff-file", "--file", "master.yaml", "current.yaml",
                "--base", base, "--head", "HEAD", "--repo", str(repo),
            ],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "+ /m -> /n" in out
        assert "1 add" in out


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
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        fake = FakeRtd()  # nothing in RtD; apply must add /a and converge
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--yes"],
            client_factory=lambda _project: fake,
        )
        assert rc == EXIT_OK
        assert [r.from_url for r in fake.list_redirects()] == ["/a"]
        assert "applied: 0 deleted, 1 added" in capsys.readouterr().err

    def test_interactive_yes_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        fake = FakeRtd()
        monkeypatch.setattr("builtins.input", lambda _: "y")
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=lambda _project: fake,
        )
        assert rc == EXIT_OK
        assert [r.from_url for r in fake.list_redirects()] == ["/a"]

    def test_reports_drift_when_apply_cannot_converge(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        # Adds land but reorders never settle (StuckRtd), so the more-specific
        # rule can't be lifted above the catch-all: apply must report drift
        # rather than claim success on a shadowed ordering.
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a/sub/*
                to:   /sub
                type: page
              - from: /a/*
                to:   /general
                type: page
        """)
        fake = StuckRtd()
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--yes"],
            client_factory=lambda _project: fake,
        )
        assert rc == EXIT_DRIFT
        assert "did not converge" in capsys.readouterr().err

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


_UNREACHABLE_YAML = """
schema_version: 1
redirects:
  - from: /api/*
    to:   /v2/:splat
    type: page
  - from: /api/v1/foo.html
    to:   /v2/foo.html
    type: page
"""


class TestPlanStrict:
    def test_strict_with_no_findings_exits_ok(
        self, tmp_path: Path, mock_client: MagicMock, factory,
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
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--strict"],
            client_factory=factory,
        )
        assert rc == EXIT_OK

    def test_strict_with_ordering_error_exits_validation(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        mock_client.list_redirects.return_value = []
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--strict"],
            client_factory=factory,
        )
        assert rc == EXIT_VALIDATION
        err = capsys.readouterr().err
        assert "validate:" in err
        assert "ERROR ordering" in err

    def test_no_strict_skips_validation(
        self, tmp_path: Path, mock_client: MagicMock, factory,
    ):
        """Without --strict, even an unreachable rule doesn't fail plan."""
        _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        mock_client.list_redirects.return_value = []
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK


class TestApplyStrict:
    def test_strict_with_ordering_error_refuses_to_apply(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        mock_client.list_redirects.return_value = []
        rc = main(
            [
                "apply", "--project", "p",
                "--file", str(tmp_path / "r.yaml"),
                "--strict", "--yes",
            ],
            client_factory=factory,
        )
        assert rc == EXIT_VALIDATION
        # Must have refused before mutating
        mock_client.create_redirect.assert_not_called()
        assert "refusing" in capsys.readouterr().err


class TestAuditFindings:
    def test_clean_file_no_drift_exits_ok(
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

    def test_ordering_error_in_audit_exits_validation(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        # No drift — RtD matches the YAML — but validation still flags.
        mock_client.list_redirects.return_value = [
            _r("/api/*", "/v2/:splat", pk=1),
            _r("/api/v1/foo.html", "/v2/foo.html", pk=2),
        ]
        # Have to adjust _r's defaults so the records actually match parsed
        # YAML; do it inline.
        from rtd_redirects.model import Redirect
        mock_client.list_redirects.return_value = [
            Redirect(
                from_url="/api/*", to_url="/v2/:splat",
                type="page", pk=1,
            ),
            Redirect(
                from_url="/api/v1/foo.html", to_url="/v2/foo.html",
                type="page", pk=2, position=1,
            ),
        ]
        rc = main(
            ["audit", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_VALIDATION
        err = capsys.readouterr().err
        assert "validate:" in err

    def test_drift_alone_exits_drift(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        """No validation findings, but RtD differs from YAML."""
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        # RtD has an extra record not in YAML
        mock_client.list_redirects.return_value = [
            _r("/a", "/b", pk=1),
            _r("/c", "/d", pk=2),
        ]
        rc = main(
            ["audit", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_DRIFT


class TestValidateSubcommand:
    """`validate` is the API-free entry point for local agents and pre-commit."""

    def test_clean_file_exits_ok(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        f = _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        rc = main(["validate", str(f)], client_factory=factory)
        assert rc == EXIT_OK
        assert "ok" in capsys.readouterr().err

    def test_unreachable_rule_flagged(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        f = _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        rc = main(["validate", str(f)], client_factory=factory)
        assert rc == EXIT_VALIDATION
        err = capsys.readouterr().err
        assert "ERROR ordering" in err

    def test_multiple_files(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        clean = _write_yaml(tmp_path / "clean.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: exact
        """)
        bad = _write_yaml(tmp_path / "bad.yaml", _UNREACHABLE_YAML)
        rc = main(["validate", str(clean), str(bad)], client_factory=factory)
        assert rc == EXIT_VALIDATION
        err = capsys.readouterr().err
        assert "clean.yaml: ok" in err
        assert "bad.yaml" in err
        assert "ERROR ordering" in err

    def test_fix_rewrites_file_and_exits_clean(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        f = _write_yaml(tmp_path / "r.yaml", _UNREACHABLE_YAML)
        rc = main(["validate", str(f), "--fix"], client_factory=factory)
        assert rc == EXIT_OK
        # File now passes validation
        rc_after = main(["validate", str(f)], client_factory=factory)
        assert rc_after == EXIT_OK

    def test_fix_preserves_top_level_metadata(
        self, tmp_path: Path, factory,
    ):
        f = _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            language_prefix: /en
            defaults:
              versions: [latest]
            redirects:
              - from: /api/*
                to:   /v2/:splat
                type: exact
              - from: /api/v1/foo.html
                to:   /v2/foo.html
                type: exact
        """)
        rc = main(["validate", str(f), "--fix"], client_factory=factory)
        assert rc == EXIT_OK
        rewritten = yaml.safe_load(f.read_text())
        assert rewritten["schema_version"] == 1
        assert rewritten["language_prefix"] == "/en"
        assert rewritten["defaults"]["versions"] == ["latest"]

    def test_no_api_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """validate must work without RTD_API_TOKEN — used by pre-commit hooks."""
        monkeypatch.delenv("RTD_API_TOKEN", raising=False)
        f = _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: exact
        """)
        # Factory shouldn't even be invoked. Use a poisoned factory to prove it.
        def poisoned(_slug):
            raise RuntimeError("RtD client should not be constructed")
        rc = main(["validate", str(f)], client_factory=poisoned)
        assert rc == EXIT_OK

    def test_composed_flag_catches_cross_file_ordering(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        """--composed validates the ordered composition, catching an ordering
        error that only exists once the files are composed together.

        Each file is independently well-ordered; the error appears only when the
        broad wildcard in the first file is composed ahead of the specific rule
        in the second.
        """
        first = _write_yaml(tmp_path / "first.yaml", """
            schema_version: 1
            redirects:
              - from: /en/latest/api/*
                to:   /en/latest/v2/:splat
                type: exact
        """)
        second = _write_yaml(tmp_path / "second.yaml", """
            schema_version: 1
            redirects:
              - from: /en/latest/api/foo.html
                to:   /en/latest/v2/foo.html
                type: exact
        """)
        # Per-file: both clean.
        rc_per_file = main([
            "validate", str(first), str(second),
        ], client_factory=factory)
        assert rc_per_file == EXIT_OK
        # Composed: the wildcard at position 0 shadows the specific rule.
        rc = main([
            "validate", str(first), str(second), "--composed",
        ], client_factory=factory)
        assert rc == EXIT_VALIDATION
        err = capsys.readouterr().err
        assert "composed" in err
        assert "ERROR ordering" in err

    def test_composed_clean_set_exits_ok(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        master = _write_yaml(tmp_path / "master.yaml", """
            schema_version: 1
            redirects:
              - from: /en/master/x.html
                to:   /en/master/y.html
                type: exact
        """)
        current = _write_yaml(tmp_path / "current.yaml", """
            schema_version: 1
            redirects:
              - from: /old/*
                to:   /new/:splat
                type: page
        """)
        rc = main([
            "validate", str(master), str(current), "--composed",
        ], client_factory=factory)
        assert rc == EXIT_OK
        assert "ok" in capsys.readouterr().err

    def test_composed_with_fix_is_usage_error(
        self, tmp_path: Path, factory, capsys: pytest.CaptureFixture,
    ):
        f = _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
        """)
        rc = main([
            "validate", str(f), "--composed", "--fix",
        ], client_factory=factory)
        assert rc != EXIT_OK
        assert "cannot be combined with --fix" in capsys.readouterr().err


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


class TestDuplicateHandling:
    """DOC-946: live duplicate (from_url, type) identities must not crash the
    read path. plan/dump warn, audit treats them as drift, apply heals them.
    Before the fix these commands died with an uncaught ValueError traceback.
    """

    @staticmethod
    def _dup_pair() -> list[Redirect]:
        # The /serialization.html discovery shape: one identity, two records,
        # the lower-position one fires the *wrong* target (the dangerous case).
        return [
            Redirect(
                from_url="/serialization.html", to_url="/configure.html",
                type="exact", position=0, pk=7486,  # kept — what RtD serves
            ),
            Redirect(
                from_url="/serialization.html", to_url="/ray-core/serial.html",
                type="exact", position=1, pk=7450,  # shadowed, different target
            ),
        ]

    def test_plan_warns_and_does_not_crash(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /serialization.html
                to:   /configure.html
                type: exact
        """)
        mock_client.list_redirects.return_value = self._dup_pair()
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        err = capsys.readouterr().err
        assert "duplicate live" in err
        assert "DIFFERENT TARGET" in err

    def test_audit_treats_duplicate_as_drift(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        # YAML matches the kept record exactly, so the deduped diff is empty —
        # the duplicate alone must still register as drift.
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /serialization.html
                to:   /configure.html
                type: exact
        """)
        mock_client.list_redirects.return_value = self._dup_pair()
        rc = main(
            ["audit", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_DRIFT
        err = capsys.readouterr().err
        assert "drift detected" in err
        assert "duplicate live" in err

    def test_dump_emits_kept_only_and_round_trips(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        mock_client.list_redirects.return_value = self._dup_pair()
        out_path = tmp_path / "out.yaml"
        rc = main(
            ["dump", "--project", "p", "--output", str(out_path)],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        assert "duplicate live" in capsys.readouterr().err
        doc = yaml.safe_load(out_path.read_text())
        entries = [e for e in doc["redirects"] if e["from"] == "/serialization.html"]
        assert len(entries) == 1  # only the kept record is written
        assert entries[0]["to"] == "/configure.html"
        # The dumped YAML re-parses without the duplicate-identity error.
        from rtd_redirects.parse import parse_file
        assert len(parse_file(out_path)) == 1

    def test_apply_heals_live_duplicate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /serialization.html
                to:   /configure.html
                type: exact
        """)
        fake = FakeRtd(initial=self._dup_pair())
        rc = main(
            ["apply", "--project", "p", "--file", str(tmp_path / "r.yaml"), "--yes"],
            client_factory=lambda _project: fake,
        )
        assert rc == EXIT_OK
        live = fake.list_redirects()
        assert len(live) == 1
        assert live[0].to_url == "/configure.html"
        assert "duplicate live" in capsys.readouterr().err

    def test_same_target_duplicate_not_flagged_dangerous(
        self, tmp_path: Path, mock_client: MagicMock, factory,
        capsys: pytest.CaptureFixture,
    ):
        # Pure double-insert (same target): warn, but not [DIFFERENT TARGET].
        _write_yaml(tmp_path / "r.yaml", """
            schema_version: 1
            redirects:
              - from: /dep.html
                to:   /deps
                type: exact
        """)
        mock_client.list_redirects.return_value = [
            Redirect(from_url="/dep.html", to_url="/deps", type="exact", position=0, pk=1),
            Redirect(from_url="/dep.html", to_url="/deps", type="exact", position=1, pk=2),
        ]
        rc = main(
            ["plan", "--project", "p", "--file", str(tmp_path / "r.yaml")],
            client_factory=factory,
        )
        assert rc == EXIT_OK
        err = capsys.readouterr().err
        assert "duplicate live" in err
        assert "DIFFERENT TARGET" not in err
