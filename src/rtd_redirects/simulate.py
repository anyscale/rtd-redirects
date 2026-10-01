"""Simulate a redirect change: where does every old URL land, before and after?

``validate`` checks a rule set in isolation. A page-rename change can pass it
with no errors and still send readers to a landing page, a 404, or a loop,
because the outcome depends on which pages exist on each version. This module
replays a set of test URLs through the before and after rule sets, on every
version in a matrix, with ``resolve.Resolver``, and classifies each pair.

Each version carries two page sets, before and after the change. A version
the change doesn't touch, such as an older release, uses the same set on both
sides. Including such versions matters: a rule repointed at a moved page's new
path breaks on every version that doesn't have the move.

Verdicts, per URL and version:

- ``regression``: resolved before, doesn't after (404, loop, or neither).
- ``wrong-landing``: resolves on both sides, but after lands somewhere other
  than the expected page. The expected page is the before landing if it still
  exists after the change, else that landing mapped through the rename map.
- ``new-loop``: unresolved before, loops after.
- ``unmapped``: resolves on both sides, but the before landing is gone and no
  rename says where it went, so the after landing can't be judged.
- ``fixed``: unresolved before, resolves after.
- ``unresolved``: unresolved on both sides.
- ``unverified``: a redirect jumps to a version outside the matrix.
- ``ok``: lands on the expected page.

Independently of the verdict, an after resolution needing more hops than the
hop budget is flagged. Regressions, wrong landings, new loops, and budget
overruns count as failures.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml

from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX
from rtd_redirects.model import URL_STYLE_TYPES, RedirectSet
from rtd_redirects.pages import SOURCE_SUFFIXES, source_to_page
from rtd_redirects.resolve import DEFAULT_MAX_HOPS, Resolution, Resolver

Verdict = Literal[
    "ok", "regression", "wrong-landing", "new-loop", "unmapped",
    "fixed", "unresolved", "unverified",
]
FAILURE_VERDICTS: frozenset[str] = frozenset({"regression", "wrong-landing", "new-loop"})

# Stands in for "any page under this prefix" when a wildcard rule source is
# turned into a test URL.
WILDCARD_SAMPLE = "__simulate-sample__.html"

_MAX_RENAME_STEPS = 10


class SimulateError(Exception):
    """Raised for malformed simulation inputs, such as a bad rename map."""


@dataclass(frozen=True)
class VersionPages:
    """The pages one version serves before and after the change."""

    before: Set[str]
    after: Set[str]


@dataclass(frozen=True)
class TestUrl:
    """A URL to replay. ``version`` of ``None`` means every version in the matrix."""

    path: str
    version: str | None = None

    __test__ = False  # not a pytest test class


class RenameMap:
    """Where a page that existed before the change lives after it.

    Exact entries map one page path to another. Prefix entries map a ``from``
    ending in ``*`` to a ``to`` containing ``:splat``, like a wildcard
    redirect. Exact entries win over prefixes, and the longest prefix wins.
    Mapping repeats until the path stops changing, so a directory move and a
    file rename inside it compose.
    """

    def __init__(
        self,
        exact: Mapping[str, str] | None = None,
        prefixes: Iterable[tuple[str, str]] = (),
    ) -> None:
        self.exact: dict[str, str] = dict(exact or {})
        self.prefixes = sorted(prefixes, key=lambda p: len(p[0]), reverse=True)

    def __bool__(self) -> bool:
        return bool(self.exact or self.prefixes)

    def update(self, other: RenameMap) -> None:
        self.exact.update(other.exact)
        self.prefixes = sorted(
            [*self.prefixes, *other.prefixes], key=lambda p: len(p[0]), reverse=True,
        )

    def apply(self, path: str) -> str:
        for _ in range(_MAX_RENAME_STEPS):
            new = self._step(path)
            if new == path:
                break
            path = new
        return path

    def _step(self, path: str) -> str:
        if path in self.exact:
            return self.exact[path]
        for prefix, to in self.prefixes:
            if path.startswith(prefix):
                return to.replace(":splat", path[len(prefix):])
        return path


def load_rename_map(path: Path) -> RenameMap:
    """Read a rename map from YAML.

    Accepts either a mapping of ``old: new`` or a list of ``{from, to}``
    entries. A key ending in ``*`` is a prefix entry; its value should use
    ``:splat``.
    """
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise SimulateError(f"couldn't read rename map {path}: {e}") from e
    if isinstance(doc, dict) and "renames" in doc:
        doc = doc["renames"]
    pairs: list[tuple[Any, Any]]
    if isinstance(doc, dict):
        pairs = list(doc.items())
    elif isinstance(doc, list) and all(isinstance(e, dict) for e in doc):
        pairs = [(e.get("from"), e.get("to")) for e in doc]
    else:
        raise SimulateError(
            f"{path}: expected a mapping of old: new paths or a list of {{from, to}}"
        )
    rm = RenameMap()
    for old, new in pairs:
        if not isinstance(old, str) or not isinstance(new, str):
            raise SimulateError(f"{path}: rename entries must be strings: {old!r}: {new!r}")
        if old.endswith("*"):
            rm.update(RenameMap(prefixes=[(old[:-1], new)]))
        else:
            rm.exact[old] = new
    return rm


def renames_from_git(
    srcdir: str,
    *,
    base_ref: str,
    head_ref: str,
    repo_path: str | Path | None = None,
) -> RenameMap:
    """Derive exact page renames from the git renames between two refs.

    ``head_ref`` may be ``WORKTREE`` to compare against the working tree.
    Only renames whose both sides are Sphinx sources under ``srcdir`` count.
    """
    srcdir = srcdir.rstrip("/")
    cmd = ["git"]
    if repo_path is not None:
        cmd.extend(["-C", str(repo_path)])
    cmd.extend(["diff", "-M", "--name-status", "--diff-filter=R", base_ref])
    if head_ref != "WORKTREE":
        cmd.append(head_ref)
    cmd.extend(["--", srcdir])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SimulateError(f"git diff {base_ref} {head_ref} failed: {result.stderr.strip()}")
    exact = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        _, old, new = parts
        if all(p.startswith(srcdir + "/") and p.endswith(SOURCE_SUFFIXES) for p in (old, new)):
            exact[source_to_page(old, srcdir)] = source_to_page(new, srcdir)
    return RenameMap(exact)


@dataclass(frozen=True)
class Outcome:
    """One test URL on one version, before and after the change."""

    url: str
    version: str
    before: Resolution
    after: Resolution
    expected: tuple[str, str] | None
    verdict: Verdict
    over_budget: bool = False

    @property
    def failed(self) -> bool:
        return self.verdict in FAILURE_VERDICTS or self.over_budget


@dataclass(frozen=True)
class Report:
    outcomes: tuple[Outcome, ...]
    versions: tuple[str, ...]
    hop_budget: int | None

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.failed]

    def for_version(self, version: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.version == version]


def parse_test_url(url: str, *, language_prefix: str = DEFAULT_LANGUAGE_PREFIX) -> TestUrl:
    """Turn a URL or path into a ``TestUrl``.

    A full ``https://host/<lang>/<version>/...`` URL or a ``/<lang>/<version>/``
    path pins the version. Any other path is tested on every version.
    """
    path = urlparse(url).path if "://" in url else url
    path = path.split("#", 1)[0]
    m = re.match(rf"^{re.escape(language_prefix)}/([^/]+)(/.*)?$", path)
    if m:
        return TestUrl(m.group(2) or "/", m.group(1))
    return TestUrl(path)


def rule_source_tests(
    rule_sets: Iterable[RedirectSet],
    *,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> set[TestUrl]:
    """One test URL per rule ``from``, with wildcards instantiated by a sample.

    Fragment sources are skipped, since they never match server-side.
    """
    tests = set()
    for rs in rule_sets:
        for rule in rs:
            frm = rule.from_url
            if rule.type in URL_STYLE_TYPES or not frm or "#" in frm:
                continue
            if frm.endswith("*"):
                frm = frm[:-1] + WILDCARD_SAMPLE
            if rule.type == "exact":
                test = parse_test_url(frm, language_prefix=language_prefix)
                if test.version is None:
                    continue
            else:
                test = TestUrl(frm)
            tests.add(test)
    return tests


def prefix_tests(versions: Mapping[str, VersionPages], prefixes: Iterable[str]) -> set[TestUrl]:
    """Every page under any of ``prefixes`` in any version's before set."""
    prefixes = tuple(prefixes)
    if not prefixes:
        return set()
    return {
        TestUrl(p)
        for vp in versions.values()
        for p in vp.before
        if p.startswith(prefixes)
    }


def simulate(
    before: RedirectSet,
    after: RedirectSet,
    versions: Mapping[str, VersionPages],
    tests: Iterable[TestUrl],
    *,
    renames: RenameMap | None = None,
    hop_budget: int | None = None,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Report:
    """Replay ``tests`` through both rule sets on every version in ``versions``."""
    renames = renames or RenameMap()
    before_resolver = Resolver(
        before, {v: vp.before for v, vp in versions.items()},
        language_prefix=language_prefix, max_hops=max_hops,
    )
    after_resolver = Resolver(
        after, {v: vp.after for v, vp in versions.items()},
        language_prefix=language_prefix, max_hops=max_hops,
    )

    pairs = set()
    for t in tests:
        if t.version is None:
            pairs.update((v, t.path) for v in versions)
        elif t.version in versions:
            pairs.add((t.version, t.path))

    order = {v: i for i, v in enumerate(versions)}
    outcomes = []
    for version, path in sorted(pairs, key=lambda p: (order[p[0]], p[1])):
        b = before_resolver.resolve(version, path)
        a = after_resolver.resolve(version, path)
        expected = _expected(b, after_resolver, renames)
        outcomes.append(Outcome(
            url=path,
            version=version,
            before=b,
            after=a,
            expected=expected,
            verdict=_verdict(b, a, expected),
            over_budget=hop_budget is not None and a.resolved and a.hops > hop_budget,
        ))
    return Report(tuple(outcomes), tuple(versions), hop_budget)


def _expected(
    before: Resolution, after_resolver: Resolver, renames: RenameMap,
) -> tuple[str, str] | None:
    if before.status == "external":
        return before.landing
    if before.status != "ok":
        return None
    version, path = before.landing
    if after_resolver.exists(version, path):
        return (version, path)
    moved = renames.apply(path)
    if moved != path:
        return (version, moved)
    return None


def _verdict(b: Resolution, a: Resolution, expected: tuple[str, str] | None) -> Verdict:
    if "unmodeled" in (b.status, a.status):
        return "unverified"
    if b.resolved:
        if not a.resolved:
            return "regression"
        if expected is None:
            return "unmapped"
        return "ok" if a.landing == expected else "wrong-landing"
    if a.status == "loop" and b.status != "loop":
        return "new-loop"
    return "fixed" if a.resolved else "unresolved"


def summarize(report: Report) -> dict[str, Any]:
    """A JSON-serializable summary: per-version counts and every non-ok outcome."""
    per_version = {}
    for version in report.versions:
        outcomes = report.for_version(version)
        per_version[version] = {
            "urls": len(outcomes),
            "verdicts": dict(sorted(Counter(o.verdict for o in outcomes).items())),
            "hops_after": _hop_histogram(outcomes),
            "multi_hop_after": sum(1 for o in outcomes if o.after.resolved and o.after.hops >= 2),
            "over_budget": sum(1 for o in outcomes if o.over_budget),
        }
    return {
        "versions": per_version,
        "hop_budget": report.hop_budget,
        "failures": len(report.failures),
        "outcomes": [
            _outcome_dict(o) for o in report.outcomes
            if o.verdict != "ok" or o.over_budget or o.after.hops >= 2
        ],
    }


def _hop_histogram(outcomes: Iterable[Outcome]) -> dict[int, int]:
    return dict(sorted(Counter(o.after.hops for o in outcomes if o.after.resolved).items()))


def _resolution_dict(r: Resolution) -> dict[str, Any]:
    return {
        "version": r.version,
        "path": r.path,
        "hops": r.hops,
        "status": r.status,
        "via": [rule.from_url for rule in r.rules],
    }


def _outcome_dict(o: Outcome) -> dict[str, Any]:
    return {
        "url": o.url,
        "version": o.version,
        "verdict": o.verdict,
        "over_budget": o.over_budget,
        "expected": list(o.expected) if o.expected else None,
        "before": _resolution_dict(o.before),
        "after": _resolution_dict(o.after),
    }


def format_report(
    report: Report,
    *,
    limit: int = 40,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> str:
    """Render a report as text, one section per version."""
    lines: list[str] = []
    for version in report.versions:
        outcomes = report.for_version(version)
        counts = Counter(o.verdict for o in outcomes)
        lines.append(f"{language_prefix}/{version}/: {len(outcomes)} URLs")
        lines.append(f"  hops after (resolved URLs): {_hop_histogram(outcomes)}")

        def section(title: str, rows: list[Outcome], *, always: bool = False) -> None:
            if not rows and not always:
                return
            lines.append(f"  {title}: {len(rows)}")
            for o in rows[:limit]:
                lines.append(f"    {_describe(o)}")
            if len(rows) > limit:
                lines.append(f"    ... {len(rows) - limit} more")

        section("regressions", [o for o in outcomes if o.verdict == "regression"], always=True)
        section(
            "wrong landings", [o for o in outcomes if o.verdict == "wrong-landing"], always=True,
        )
        section("new loops", [o for o in outcomes if o.verdict == "new-loop"], always=True)
        if report.hop_budget is not None:
            section(
                f"over hop budget (more than {report.hop_budget})",
                [o for o in outcomes if o.over_budget], always=True,
            )
        section(
            "2+ hops after",
            [o for o in outcomes if o.after.resolved and o.after.hops >= 2],
        )
        section(
            "moved with no rename mapping",
            [o for o in outcomes if o.verdict == "unmapped"],
        )
        section("unverified", [o for o in outcomes if o.verdict == "unverified"])
        lines.append(
            f"  fixed: {counts['fixed']}, unresolved before and after: "
            f"{counts['unresolved']}, ok: {counts['ok']}"
        )
        lines.append("")

    failures = report.failures
    if failures:
        lines.append(f"simulate: FAIL, {len(failures)} failing URL check(s)")
    else:
        lines.append("simulate: ok, no regressions, wrong landings, or new loops")
    return "\n".join(lines)


def _describe(o: Outcome) -> str:
    text = f"{o.url}: before {_short(o.before)}; after {_short(o.after)}"
    if o.verdict == "wrong-landing" and o.expected:
        text += f"; expected {_where(o.expected)}"
    return text


def _short(r: Resolution) -> str:
    hops = f"{r.hops} hop{'' if r.hops == 1 else 's'}"
    where = _where(r.landing)
    via = f" via {' -> '.join(rule.from_url for rule in r.rules)}" if r.rules else ""
    return f"{r.status} {where} ({hops}){via}"


def _where(landing: tuple[str, str]) -> str:
    version, path = landing
    return path if not version else f"[{version}] {path}"
