"""Smoke tests: package imports and CLI entry point is wired."""

from __future__ import annotations

import pytest

import rtd_redirects
from rtd_redirects.cli import _build_parser, main


def test_package_has_version():
    assert rtd_redirects.__version__


def test_cli_main_is_callable():
    assert callable(main)


@pytest.mark.parametrize(
    "command",
    ["list", "dump", "plan", "diff-file", "apply", "audit"],
)
def test_cli_registers_subcommand(command: str):
    parser = _build_parser()
    args = parser.parse_args([command])
    assert args.command == command


def test_cli_requires_subcommand(capsys):
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
