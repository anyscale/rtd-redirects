"""rtd-redirects CLI entry point.

Subcommands are stubbed out here so the surface is reviewable before
each module lands. Implementations arrive in subsequent PRs.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtd-redirects",
        description="Manage Read the Docs redirects as code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list",
        help="List redirects currently configured on the RtD project.",
    )
    subparsers.add_parser(
        "dump",
        help="Export the RtD project's current redirects to a YAML file.",
    )
    subparsers.add_parser(
        "plan",
        help="Show the diff between a YAML file and the RtD project, without applying.",
    )
    subparsers.add_parser(
        "diff-file",
        help="Show the redirect-level diff between two git refs of a YAML file.",
    )
    subparsers.add_parser(
        "apply",
        help="Apply a YAML file to the RtD project.",
    )
    subparsers.add_parser(
        "audit",
        help="Report drift between a YAML file and the RtD project.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the rtd-redirects CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    print(
        f"rtd-redirects {args.command}: not yet implemented",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
