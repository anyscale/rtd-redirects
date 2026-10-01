"""rtd-redirects CLI entry point.

Wires the subcommands end-to-end against the underlying modules:

- ``list``: ``RtdClient.list_redirects``
- ``dump``: ``RtdClient.list_redirects`` + ``collapse`` + YAML serialization
- ``plan``: ``parse_files`` + ``RtdClient.list_redirects`` + ``diff`` (no mutation)
- ``diff-file``: ``diff_file`` (git-only, no API)
- ``apply``: ``parse_files`` + ``RtdClient.list_redirects`` + ``diff`` + ``apply``
- ``audit``: same as ``plan`` but exits non-zero when drift is detected
- ``validate``: ordering and chain checks (no API)
- ``simulate``: replay URLs through two rule sets across a version matrix (no API)

``plan``, ``apply``, ``audit``, and ``diff-file`` accept an ordered list of
``--file`` paths and compose them as one source of truth (earlier files match
first; see ``parse.compose``). ``validate --composed`` runs the same composition
through the credential-free validator.

The ``client_factory`` keyword on ``main()`` exists so tests can inject a
mock client without monkeypatching the ``RtdClient`` import. Production code
uses the default, which constructs a real ``RtdClient`` from
``RTD_API_TOKEN``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

import yaml

from rtd_redirects.apply import apply_converging
from rtd_redirects.client import RtdAuthError, RtdClient, RtdClientError
from rtd_redirects.collapse import collapse
from rtd_redirects.diff import Diff, diff
from rtd_redirects.diff_file import GitError, compose_at_ref, diff_file
from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX
from rtd_redirects.model import DuplicateGroup, RedirectSet
from rtd_redirects.pages import PageSourceError, load_pages, spec_kind
from rtd_redirects.parse import SCHEMA_VERSION, parse_file, parse_files
from rtd_redirects.resolve import DEFAULT_MAX_HOPS
from rtd_redirects.simulate import (
    RenameMap,
    SimulateError,
    VersionPages,
    format_report,
    load_rename_map,
    parse_test_url,
    prefix_tests,
    renames_from_git,
    rule_source_tests,
    simulate,
    summarize,
)
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
        "simulate": _cmd_simulate,
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
    except (PageSourceError, SimulateError) as e:
        print(f"error: simulate: {e}", file=sys.stderr)
        return EXIT_USAGE
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
    file_help = (
        "Path to the YAML redirect source file. Accepts multiple ordered paths "
        "(e.g. master.yaml current.yaml) composed as one source of truth: "
        "earlier files match first."
    )

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
    p_plan.add_argument(
        "--file", "-f", required=True, nargs="+", metavar="FILE", help=file_help,
    )
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
    p_diff.add_argument(
        "--file", "-f", required=True, nargs="+", metavar="FILE", help=file_help,
    )
    p_diff.add_argument(
        "--repo", default=None,
        help="Path to the repository (default: current working directory).",
    )

    p_apply = subparsers.add_parser(
        "apply", help="Apply a YAML file to the RtD project.",
    )
    p_apply.add_argument("--project", "-p", default=None, help=project_help)
    p_apply.add_argument(
        "--file", "-f", required=True, nargs="+", metavar="FILE", help=file_help,
    )
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
    p_audit.add_argument(
        "--file", "-f", required=True, nargs="+", metavar="FILE", help=file_help,
    )

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
    p_validate.add_argument(
        "--composed", action="store_true",
        help="Validate the ordered composition of all files as one redirect set "
             "(earlier files match first) instead of each file independently. "
             "Catches cross-file ordering errors. Incompatible with --fix.",
    )

    _add_simulate_parser(subparsers)

    return parser


class _PageSpecAction(argparse.Action):
    """Append ``(side, VERSION=SOURCE)`` to a list shared by the --pages flags."""

    def __init__(self, option_strings, dest, const=None, **kwargs):
        super().__init__(option_strings, dest, nargs=None, const=const, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        specs = list(getattr(namespace, self.dest) or [])
        specs.append((self.const, values))
        setattr(namespace, self.dest, specs)


def _add_simulate_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "simulate",
        help="Replay URLs through the redirect rules before and after a change, "
             "across a version matrix, and report regressions, wrong landings, "
             "and loops. Requires no RtD credentials.",
    )
    rules = p.add_argument_group("redirect rules")
    rules.add_argument(
        "--file", "-f", nargs="+", metavar="FILE",
        help="Redirect file(s), relative to the repo root, read at --base for the "
             "before side and --head for the after side. Multiple files compose in "
             "order.",
    )
    rules.add_argument(
        "--base", default="origin/master",
        help="Git ref for the before side (default: origin/master).",
    )
    rules.add_argument(
        "--head", default="HEAD",
        help="Git ref for the after side (default: HEAD). WORKTREE reads the files "
             "on disk.",
    )
    rules.add_argument(
        "--before-file", nargs="+", metavar="FILE",
        help="Read the before side from these files on disk instead of --base.",
    )
    rules.add_argument(
        "--after-file", nargs="+", metavar="FILE",
        help="Read the after side from these files on disk instead of --head.",
    )
    rules.add_argument(
        "--repo", default=None,
        help="Path to the repository (default: current working directory).",
    )

    pages = p.add_argument_group(
        "version matrix",
        "Each version needs a page set: html:DIR, inv:PATH_OR_URL, "
        "sitemap:PATH_OR_URL, git:REF:SRCDIR (REF may be WORKTREE), or list:PATH. "
        "Include master, latest, and at least one older release.",
    )
    # All three flags feed one ordered list so the report follows the order
    # versions appear on the command line.
    pages.add_argument(
        "--pages", action=_PageSpecAction, const="both", dest="page_specs", default=[],
        metavar="VERSION=SOURCE",
        help="Page set for VERSION on both sides. Repeat per version.",
    )
    pages.add_argument(
        "--pages-before", action=_PageSpecAction, const="before", dest="page_specs",
        metavar="VERSION=SOURCE",
        help="Page set for VERSION before the change, overriding --pages.",
    )
    pages.add_argument(
        "--pages-after", action=_PageSpecAction, const="after", dest="page_specs",
        metavar="VERSION=SOURCE",
        help="Page set for VERSION after the change, overriding --pages.",
    )

    tests = p.add_argument_group("test URLs")
    tests.add_argument(
        "--url", action="append", default=[], metavar="URL",
        help="A URL or path to test. A /<lang>/<version>/ path pins the version; "
             "any other path runs on every version. Repeatable.",
    )
    tests.add_argument(
        "--urls-file", action="append", default=[], metavar="FILE",
        help="A file of URLs to test, one per line. Repeatable.",
    )
    tests.add_argument(
        "--prefix", action="append", default=[], metavar="PATH",
        help="Test every page under PATH in any version's before page set. "
             "Repeatable.",
    )
    tests.add_argument(
        "--no-rule-sources", action="store_true",
        help="Don't test each rule's from URL. On by default, with wildcards "
             "instantiated by a sample page name.",
    )

    judge = p.add_argument_group("judging")
    judge.add_argument(
        "--rename-map", action="append", default=[], metavar="FILE",
        help="YAML map of old page path to new page path, used to decide whether "
             "a moved page landed on its new home. Keys ending in * map a prefix "
             "with :splat. Repeatable.",
    )
    judge.add_argument(
        "--git-renames", metavar="SRCDIR",
        help="Also derive page renames from git renames of Sphinx sources under "
             "SRCDIR between --base and --head.",
    )
    judge.add_argument(
        "--hop-budget", type=int, default=None, metavar="N",
        help="Fail any URL whose after resolution needs more than N redirects.",
    )
    judge.add_argument(
        "--max-hops", type=int, default=DEFAULT_MAX_HOPS, metavar="N",
        help=f"Treat more than N redirects as a loop (default: {DEFAULT_MAX_HOPS}).",
    )
    judge.add_argument(
        "--language-prefix", default=DEFAULT_LANGUAGE_PREFIX,
        help=f"URL language segment (default: {DEFAULT_LANGUAGE_PREFIX}).",
    )

    out = p.add_argument_group("output")
    out.add_argument("--format", choices=["text", "json"], default="text")
    out.add_argument(
        "--limit", type=int, default=40, metavar="N",
        help="Rows to print per text section (default: 40).",
    )


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
    # Emit one record per identity (the lowest-position one RtD serves) so the
    # dumped YAML re-parses cleanly. Duplicates RtD permits are reported to
    # stderr, not written, keeping the bootstrap file a clean source of truth.
    target, dups = RedirectSet.from_api(client.list_redirects())
    _print_duplicate_groups(dups)
    entries = collapse(target)
    doc = {"schema_version": SCHEMA_VERSION, "redirects": entries}
    yaml_text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    if args.output:
        Path(args.output).write_text(yaml_text)
        print(f"wrote {len(entries)} entries to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(yaml_text)
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_files([Path(f) for f in args.file])
    client = client_factory(_resolve_project(args))
    target, dups = RedirectSet.from_api(client.list_redirects())
    _print_duplicate_groups(dups)
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
    )  # args.file is a list (nargs="+"); diff_file composes it in order
    _print_diff(d)
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_files([Path(f) for f in args.file])

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
    target, dups = RedirectSet.from_api(client.list_redirects())
    _print_duplicate_groups(dups, file=sys.stderr)
    d = diff(source, target)
    shadowed = [s for g in dups for s in g.shadowed]
    if shadowed:
        # apply_converging heals these (deletes the shadowed extras); fold them
        # into the preview so the confirmation prompt reflects the real change.
        d = replace(d, deletes=d.deletes + shadowed)

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

    outcome = apply_converging(source, client, log=sys.stderr)
    result = outcome.result
    passes_note = (
        f" (converged in {outcome.passes} passes)" if outcome.passes > 1 else ""
    )
    print(
        f"applied: {result.deleted} deleted, {result.added} added, "
        f"{result.updated} updated, {result.reordered} reordered{passes_note}",
        file=sys.stderr,
    )
    if not outcome.converged:
        print(
            f"apply: live state did not converge after {outcome.passes} passes; "
            f"{len(outcome.residual)} change(s) still differ:",
            file=sys.stderr,
        )
        _print_diff(outcome.residual, file=sys.stderr)
        return EXIT_DRIFT
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    source = parse_files([Path(f) for f in args.file])
    findings = validate(source)

    client = client_factory(_resolve_project(args))
    target, dups = RedirectSet.from_api(client.list_redirects())
    d = diff(source, target)

    # Live duplicate identities are drift: RtD permits them, the source-of-truth
    # doesn't, and they shadow records or serve the wrong target until healed.
    drift = not d.is_empty or bool(dups)
    if drift:
        print("audit: drift detected", file=sys.stderr)
        if not d.is_empty:
            _print_diff(d, file=sys.stderr)
        _print_duplicate_groups(dups, file=sys.stderr)
    else:
        print("audit: no drift", file=sys.stderr)

    if findings:
        _print_findings(findings, file=sys.stderr)

    # Validation errors take precedence over drift in the exit code.
    if any(f.severity == "error" for f in findings):
        return EXIT_VALIDATION
    if drift:
        return EXIT_DRIFT
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    """Validate one or more YAML files. No RtD API access required.

    Default mode validates each file independently (the pre-commit contract).
    ``--composed`` instead composes the files in order and validates the single
    composed set, which is what catches cross-file ordering errors — a specific
    ``master.yaml`` rule shadowed by a broad ``current.yaml`` catch-all, or the
    reverse. Composed mode can't rewrite a set back into N files, so it's
    incompatible with ``--fix``.
    """
    if args.composed:
        return _cmd_validate_composed(args)

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


def _cmd_validate_composed(args: argparse.Namespace) -> int:
    """Validate the ordered composition of all files as one set. No API access."""
    if args.fix:
        print(
            "error: --composed cannot be combined with --fix; a composed set "
            "can't be written back into separate files. Run --fix per file "
            "first, then --composed to check cross-file ordering.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    source = parse_files([Path(p) for p in args.files])
    findings = validate(source)
    label = " + ".join(args.files)
    if findings:
        print(f"\ncomposed ({label}):", file=sys.stderr)
        _print_findings(findings, file=sys.stderr)
        if any(f.severity == "error" for f in findings):
            return EXIT_VALIDATION
    else:
        print(f"composed ({label}): ok", file=sys.stderr)
    return EXIT_OK


def _cmd_simulate(args: argparse.Namespace, *, client_factory: ClientFactory) -> int:
    """Replay URLs through the before and after rule sets. No RtD API access."""
    if not args.file and not (args.before_file and args.after_file):
        print(
            "error: pass --file (read at --base and --head), or both "
            "--before-file and --after-file",
            file=sys.stderr,
        )
        return EXIT_USAGE

    before = _rules_side(args.before_file, args.file, args.base, args.repo)
    after = _rules_side(args.after_file, args.file, args.head, args.repo)

    versions = _version_matrix(args)
    if not versions:
        print("error: pass at least one --pages VERSION=SOURCE", file=sys.stderr)
        return EXIT_USAGE
    if len(versions) < 3:
        print(
            f"note: simulating {len(versions)} version(s). Include master, latest, "
            "and at least one older release: a rule repointed at a moved page "
            "breaks only on versions that don't have the move.",
            file=sys.stderr,
        )

    renames = RenameMap()
    for path in args.rename_map:
        renames.update(load_rename_map(Path(path)))
    if args.git_renames:
        renames.update(renames_from_git(
            args.git_renames, base_ref=args.base, head_ref=args.head, repo_path=args.repo,
        ))

    tests = set()
    for url in args.url:
        tests.add(parse_test_url(url, language_prefix=args.language_prefix))
    for path in args.urls_file:
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                tests.add(parse_test_url(line.strip(), language_prefix=args.language_prefix))
    if not args.no_rule_sources:
        tests |= rule_source_tests([before, after], language_prefix=args.language_prefix)
    tests |= prefix_tests(versions, args.prefix)
    tests |= {parse_test_url(old) for old in renames.exact}
    if not tests:
        print("error: no test URLs; pass --url, --urls-file, or --prefix", file=sys.stderr)
        return EXIT_USAGE

    report = simulate(
        before, after, versions, tests,
        renames=renames,
        hop_budget=args.hop_budget,
        language_prefix=args.language_prefix,
        max_hops=args.max_hops,
    )
    if args.format == "json":
        json.dump(summarize(report), sys.stdout, indent=2)
        print()
    else:
        print(format_report(report, limit=args.limit, language_prefix=args.language_prefix))
    return EXIT_VALIDATION if report.failures else EXIT_OK


def _rules_side(
    files: list[str] | None,
    ref_files: list[str] | None,
    ref: str,
    repo: str | None,
) -> RedirectSet:
    """Load one side's rules: explicit on-disk files, else ``ref_files`` at ``ref``."""
    if files:
        return parse_files([Path(f) for f in files])
    assert ref_files  # checked by the caller
    if ref == "WORKTREE":
        root = Path(repo or ".")
        return parse_files([root / f for f in ref_files])
    return compose_at_ref([Path(f) for f in ref_files], ref, repo)


def _version_matrix(args: argparse.Namespace) -> dict[str, VersionPages]:
    """Build each version's before and after page sets from the --pages flags."""
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    names: list[str] = []
    overrides = []
    for side, value in args.page_specs:
        version, sep, spec = value.partition("=")
        if not sep or not version or not spec:
            flag = "--pages" if side == "both" else f"--pages-{side}"
            raise SimulateError(f"{flag} expects VERSION=SOURCE, got {value!r}")
        if version not in names:
            names.append(version)
        if side == "both":
            before.setdefault(version, spec)
            after.setdefault(version, spec)
        else:
            overrides.append((side, version, spec))
    # Side-specific flags override --pages regardless of argument order.
    for side, version, spec in overrides:
        (before if side == "before" else after)[version] = spec

    missing = [v for v in names if v not in before or v not in after]
    if missing:
        raise SimulateError(
            f"version(s) {', '.join(missing)} need a page set on both sides; "
            "pass --pages, or both --pages-before and --pages-after"
        )

    # A git tree has no build-generated pages; built sources do. Comparing the
    # two makes every generated page look added or removed by the change.
    mixed = [
        v for v in names if (spec_kind(before[v]) == "git") != (spec_kind(after[v]) == "git")
    ]
    if mixed:
        print(
            f"note: {', '.join(mixed)}: one side reads a git tree and the other a "
            "build. Generated pages, such as API stubs, are missing from the git "
            "tree, so they read as added or removed by the change.",
            file=sys.stderr,
        )

    cache: dict[str, frozenset[str]] = {}

    def load(spec: str) -> frozenset[str]:
        if spec not in cache:
            cache[spec] = load_pages(
                spec, repo_path=args.repo, language_prefix=args.language_prefix,
            )
        return cache[spec]

    return {v: VersionPages(before=load(before[v]), after=load(after[v])) for v in names}


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


def _print_duplicate_groups(
    groups: list[DuplicateGroup], *, file: TextIO | None = None
) -> None:
    """Warn about duplicate live identities. Empty input produces no output.

    RtD permits duplicate ``(from_url, type)`` records; the source-of-truth
    keeps one per identity. The read path serves the lowest-position record and
    treats the rest as drift. ``[DIFFERENT TARGET]`` flags the dangerous case
    where a shadowed duplicate intended a different destination, so the live
    redirect silently serves the wrong target.
    """
    if file is None:
        file = sys.stderr
    if not groups:
        return
    n = len(groups)
    print(
        f"\nwarning: {n} duplicate live "
        f"identit{'y' if n == 1 else 'ies'} on RtD "
        "(RtD permits these; the source-of-truth treats them as drift):",
        file=file,
    )
    for g in groups:
        from_url, type_ = g.identity
        flag = "" if g.same_target else "  [DIFFERENT TARGET]"
        shadowed = "; ".join(
            f"pk={s.pk} @pos {s.position} -> {s.to_url}" for s in g.shadowed
        )
        print(
            f"  ({from_url}, {type_}): serving pk={g.kept.pk} @pos "
            f"{g.kept.position} -> {g.kept.to_url}; shadowed {shadowed}{flag}",
            file=file,
        )


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
