"""rtd-redirects CLI entry point.

Wires the six MVP subcommands end-to-end against the underlying modules:

- ``list``: ``RtdClient.list_redirects``
- ``dump``: ``RtdClient.list_redirects`` + ``collapse`` + YAML serialization
- ``plan``: ``parse_file`` + ``RtdClient.list_redirects`` + ``diff`` (no mutation)
- ``diff-file``: ``diff_file`` (git-only, no API)
- ``apply``: ``parse_file`` + ``RtdClient.list_redirects`` + ``diff`` + ``apply``
- ``audit``: same as ``plan`` but exits non-zero when drift is detected

The ``client_factory`` keyword on ``main()`` exists so tests can inject a
mock client without monkeypatching the ``RtdClient`` import. Production code
uses the default, which constructs a real ``RtdClient`` from
``RTD_API_TOKEN``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import yaml

from rtd_redirects.apply import apply as apply_diff
from rtd_redirects.client import RtdAuthError, RtdClient, RtdClientError
from rtd_redirects.collapse import collapse
from rtd_redirects.diff import Diff, diff
from rtd_redirects.diff_file import GitError, diff_file
from rtd_redirects.exceptions import ParseError
from rtd_redirects.model import RedirectSet
from rtd_redirects.parse import SCHEMA_VERSION, parse_file
from rtd_redirects.validate import Finding, fix_ordering, validate

ClientFactory = Callable[[str], RtdClient]

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2
EXIT_RTD = 3
EXIT_GIT = 4
EXIT_PARSE = 5
EXIT_VALIDATION = 6


def main(
    argv: list[str] | None = None,
    *,
    client_factory: ClientFactory = RtdClient,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "list": _cmd_list,
        "dump": _cmd_dump,
        "plan": _cmd_plan,
        "diff-file": _cmd_diff_file,
        "apply": _cmd_apply,
        "audit": _cmd_audit,
        "validate": _cmd_validate,
    }

    try:
        return handlers[args.command](args, client_factory=client_factory)
    except ParseError as e:
        print(f"error: parse: {e}", file=sys.stderr)
        return EXIT_PARSE
    except (RtdAuthError, RtdClientError) as e:
        print(f"error: rtd: {e}", file=sys.stderr)
        return EXIT_RTD
    except GitError as e:
        print(f"error: git: {e}", file=sys.stderr)
        return EXIT_GIT
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename}", file=sys.stderr)
        return EXIT_PARSE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtd-redirects",
        description="Manage Read the Docs redirects as code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    project_help = (
        "RtD project slug. Defaults to the RTD_PROJECT_SLUG env var."
    )
    file_help = "Path to the YAML redirect source file."

    p_list = subparsers.add_parser(
        "list", help="List redirects currently configured on the RtD project.",
    )
    p_list.add_argument("--project", "-p", default=None, help=project_help)

    p_dump = subparsers.add_parser(
        "dump", help="Export the RtD project's redirects to a YAML file.",
    )
    p_dump.add_argument("--project", "-p", default=None, help=project_help)
    p_dump.add_argument(
        "--output", "-o", default=None,
        help="Output file path. Writes to stdout when omitted.",
    )

    p_plan = subparsers.add_parser(
        "plan", help="Show the diff between a YAML file and the RtD project.",
    )
    p_plan.add_argument("--project", "-p", default=None, help=project_help)
    p_plan.add_argument("--file", "-f", required=True, help=file_help)
    p_plan.add_argument(
        "--strict", action="store_true",
        help="Run the order / chain validator and exit non-zero on any error finding.",
    )

    p_diff = subparsers.add_parser(
        "diff-file", help="Show the diff between two git refs of a YAML file.",
    )
    p_diff.add_argument(
        "--base", default="origin/master",
        help="Base git ref (default: origin/master).",
    )
    p_diff.add_argument(
        "--head", default="HEAD",
        help="Head git ref (default: HEAD).",
    )
    p_diff.add_argument("--file", "-f", required=True, help=file_help)
    p_diff.add_argument(
        "--repo", default=None,
        help="Path to the repository (default: current working directory).",
    )

    p_apply = subparsers.add_parser(
        "apply", help="Apply a YAML file to the RtD project.",
    )
    p_apply.add_argument("--project", "-p", default=None, help=project_help)
    p_apply.add_argument("--file", "-f", required=True, help=file_help)
    p_apply.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip interactive confirmation. Required in non-interactive contexts.",
    )
    p_apply.add_argument(
        "--strict", action="store_true",
        help="Run the order / chain validator and refuse to apply on any error finding.",
    )

    p_audit = subparsers.add_parser(
        "audit", help="Report drift between a YAML file and the RtD project.",
    )
    p_audit.add_argument("--project", "-p", default=None, help=project_help)
    p_audit.add_argument("--file", "-f", required=True, help=file_help)

    p_validate = subparsers.add_parser(
        "validate",
        help="Validate ordering and chain risks in one or more YAML files. "
             "Requires no RtD credentials; usable from pre-commit and local agents.",
    )
    p_validate.add_argument(
        "files", nargs="+",
        help="YAML file(s) to validate.",
    )
    p_validate.add_argument(
        "--fix", action="store_true",
        help="Reorder rules deterministically to satisfy subset constraints and "
             "rewrite the file(s) in place. Chain warnings are not auto-fixed.",
    )

    return parser


def _resolve_project(args: argparse.Namespace) -> str:
    if args.project:
        return args.project
    env = os.environ.get("RTD_PROJECT_SLUG")
    if env:
        return env
    raise SystemExit(
        "error: --project required (or set RTD_PROJECT_SLUG)",
    )


def _cmd_list(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    client = client_factory(_resolve_project(args))
    for r in client.list_redirects():
        print(f"{r.from_url} -> {r.to_url} ({r.type}) pk={r.pk}")
    return EXIT_OK


def _cmd_dump(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    client = client_factory(_resolve_project(args))
    entries = collapse(client.list_redirects())
    doc = {"schema_version": SCHEMA_VERSION, "redirects": entries}
    yaml_text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    if args.output:
        Path(args.output).write_text(yaml_text)
        print(f"wrote {len(entries)} entries to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(yaml_text)
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_file(Path(args.file))
    client = client_factory(_resolve_project(args))
    target = RedirectSet(client.list_redirects())
    d = diff(source, target)
    _print_diff(d)
    if d.is_empty:
        print("plan: no changes", file=sys.stderr)

    if args.strict:
        findings = validate(source)
        _print_findings(findings, file=sys.stderr)
        if any(f.severity == "error" for f in findings):
            return EXIT_VALIDATION

    return EXIT_OK


def _cmd_diff_file(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    d = diff_file(
        args.file,
        base_ref=args.base,
        head_ref=args.head,
        repo_path=args.repo,
    )
    _print_diff(d)
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_file(Path(args.file))

    if args.strict:
        findings = validate(source)
        _print_findings(findings, file=sys.stderr)
        if any(f.severity == "error" for f in findings):
            print(
                "apply: refusing to apply with validation errors "
                "(re-run without --strict to override)",
                file=sys.stderr,
            )
            return EXIT_VALIDATION

    client = client_factory(_resolve_project(args))
    target = RedirectSet(client.list_redirects())
    d = diff(source, target)

    if d.is_empty:
        print("apply: no changes", file=sys.stderr)
        return EXIT_OK

    _print_diff(d, file=sys.stderr)

    if not args.yes:
        try:
            response = input("Apply these changes? [y/N] ").strip().lower()
        except EOFError:
            print("error: not a tty; pass --yes to apply non-interactively", file=sys.stderr)
            return EXIT_USAGE
        if response not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return EXIT_USAGE

    result = apply_diff(d, client)
    print(
        f"applied: {result.deleted} deleted, {result.added} added, "
        f"{result.updated} updated, {result.reordered} reordered",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_file(Path(args.file))
    findings = validate(source)

    client = client_factory(_resolve_project(args))
    target = RedirectSet(client.list_redirects())
    d = diff(source, target)

    if not d.is_empty:
        print("audit: drift detected", file=sys.stderr)
        _print_diff(d, file=sys.stderr)
    else:
        print("audit: no drift", file=sys.stderr)

    if findings:
        _print_findings(findings, file=sys.stderr)

    has_errors = any(f.severity == "error" for f in findings)
    if not d.is_empty and has_errors:
        return EXIT_VALIDATION  # validation errors take precedence
    if has_errors:
        return EXIT_VALIDATION
    if not d.is_empty:
        return EXIT_DRIFT
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    """Validate one or more YAML files. No RtD API access required."""
    exit_code = EXIT_OK
    for path_str in args.files:
        path = Path(path_str)
        source = parse_file(path)
        findings = validate(source)

        if args.fix:
            ordering_errors = [
                f for f in findings if f.kind == "ordering" and f.severity == "error"
            ]
            if ordering_errors:
                fixed = fix_ordering(source)
                _write_yaml(path, fixed)
                print(
                    f"{path}: reordered {len(ordering_errors)} unreachable rule(s); "
                    "re-run validate to confirm",
                    file=sys.stderr,
                )
                # Re-validate after fix to surface anything that remains (chains, etc.).
                findings = validate(fixed)

        if findings:
            print(f"\n{path}:", file=sys.stderr)
            _print_findings(findings, file=sys.stderr)
            if any(f.severity == "error" for f in findings):
                exit_code = EXIT_VALIDATION
        else:
            print(f"{path}: ok", file=sys.stderr)
    return exit_code


def _write_yaml(path: Path, source: RedirectSet) -> None:
    """Rewrite a YAML file from a (possibly fixed) RedirectSet.

    Reads top-level metadata (schema_version, language_prefix, defaults) from
    the existing file so they're preserved. Loses comments and authoring
    formatting; round-trip-safe for canonical content.
    """
    raw = yaml.safe_load(path.read_text()) or {}
    new_doc: dict[str, object] = {}
    new_doc["schema_version"] = raw.get("schema_version", SCHEMA_VERSION)
    if "language_prefix" in raw:
        new_doc["language_prefix"] = raw["language_prefix"]
    if "defaults" in raw:
        new_doc["defaults"] = raw["defaults"]
    new_doc["redirects"] = collapse(source)
    path.write_text(yaml.safe_dump(new_doc, sort_keys=False, default_flow_style=False))


def _print_findings(findings: list[Finding], *, file: TextIO | None = None) -> None:
    """Render validation findings one per line. Empty input produces no output."""
    if file is None:
        file = sys.stderr
    if not findings:
        return
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    print(f"\nvalidate: {errors} error, {warnings} warning", file=file)
    for f in findings:
        print(f"  {f.severity.upper()} {f.kind}: {f.message}", file=file)


def _print_diff(d: Diff, *, file: TextIO | None = None) -> None:
    """Render a Diff as a human-readable summary.

    ``+`` adds, ``-`` deletes, ``~`` updates, ``@`` reorders. Footer line
    summarizes counts so a quick scan tells you the shape of the change.
    Late-binds ``file`` to ``sys.stdout`` so pytest's ``capsys`` (which
    replaces ``sys.stdout`` at fixture-setup time) captures the output.
    """
    if file is None:
        file = sys.stdout
    for r in d.adds:
        print(f"+ {r.from_url} -> {r.to_url} ({r.type})", file=file)
    for r in d.deletes:
        print(f"- {r.from_url} -> {r.to_url} ({r.type}) pk={r.pk}", file=file)
    for u in d.updates:
        print(
            f"~ {u.source.from_url} -> {u.source.to_url} ({u.source.type}) "
            f"pk={u.target.pk}",
            file=file,
        )
    for u in d.reorders:
        print(
            f"@ {u.source.from_url} ({u.source.type}) "
            f"position {u.target.position} -> {u.source.position} pk={u.target.pk}",
            file=file,
        )
    if not d.is_empty:
        print(file=file)
    print(
        f"{len(d.adds)} add, {len(d.updates)} update, "
        f"{len(d.deletes)} delete, {len(d.reorders)} reorder",
        file=file,
    )


if __name__ == "__main__":
    sys.exit(main())
