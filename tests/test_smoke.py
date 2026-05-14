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
    "argv",
    [
        ["list"],
        ["dump"],
        ["plan", "--file", "redirects.yaml"],
        ["diff-file", "--file", "redirects.yaml"],
        ["apply", "--file", "redirects.yaml"],
        ["audit", "--file", "redirects.yaml"],
        ["validate", "redirects.yaml"],
    ],
)
def test_cli_registers_subcommand(argv: list[str]):
    parser = _build_parser()
    args = parser.parse_args(argv)
    assert args.command == argv[0]


def test_cli_requires_subcommand():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
