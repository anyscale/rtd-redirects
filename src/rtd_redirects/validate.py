"""Rules-based validation for ``RedirectSet`` ordering and chain risks.

RtD applies the first redirect (by ``position``) whose ``from`` matches a
404'd request URL. There's no specificity adjudication: a generic catch-all
that lands at position 0 swallows every more-specific rule placed after it.
This module catches the two authoring mistakes that cause:

1. **Unreachable rule** — rule A's match set is a strict subset of rule B's,
   but A's position is higher than B's. B fires first; A never gets a chance.
2. **Chain candidate** — rule A's ``to_url`` could match rule B's ``from``.
   A request to A's source gets a 3xx to A.to, the browser follows, B fires,
   another 3xx. Browser-side chain rather than server-side single hop. RtD
   doesn't promise to collapse these.

Decidable in closed form because RtD's pattern surface is intentionally
narrow: each rule's match set is a ``(version, path_prefix, has_wildcard)``
triple. Subset and overlap reduce to literal prefix checks.

URL-style types (``clean_url_to_html`` / ``html_to_clean_url``) describe
project-wide URL transitions and have no ``from`` URL to compare; they're
excluded from both ordering and chain checks.

The chain detector uses literal-prefix matching on ``to_url`` (stripping
``:splat``), which is conservative: it can over-report when the splat
substitution would actually produce a URL outside the target rule's match
set. False positives are easy to dismiss; false negatives would silently
let chains slip through, which is the worse failure mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX, is_external
from rtd_redirects.model import URL_STYLE_TYPES, Redirect, RedirectSet

Severity = Literal["error", "warning"]
Kind = Literal["ordering", "chain"]


@dataclass(frozen=True)
class Finding:
    """One validation issue discovered in a ``RedirectSet``."""

    severity: Severity
    kind: Kind
    message: str
    rules: tuple[Redirect, ...] = field(default_factory=tuple)


def validate(
    rs: RedirectSet,
    *,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> list[Finding]:
    """Return all ordering / chain findings for the given set.

    Empty list means the set is well-ordered. Findings are emitted in a
    deterministic order so a CI run produces byte-stable output.
    """
    rules = sorted(rs, key=lambda r: (r.position, r.from_url, r.type))
    patterns = [_pattern_for(r, language_prefix) for r in rules]

    findings: list[Finding] = []
    findings.extend(_check_ordering(rules, patterns))
    findings.extend(_check_chains(rules, patterns, language_prefix))
    return findings


@dataclass(frozen=True)
class _Pattern:
    """Canonical match descriptor.

    ``version`` is a literal slug (``latest``, ``v2.55``, etc.) for ``exact``
    rules, ``*`` for ``page`` rules (any version), or ``None`` for rules
    that can't be analyzed (URL-style types, or malformed exact URLs).

    ``prefix`` is the literal portion of the path under the version segment.
    ``has_wildcard`` is True iff the original ``from`` ended in ``*``.
    """

    version: str | None
    prefix: str
    has_wildcard: bool


def _pattern_for(rule: Redirect, language_prefix: str) -> _Pattern | None:
    """Build a ``_Pattern`` for the rule's ``from`` URL, or ``None`` if not analyzable."""
    if rule.type in URL_STYLE_TYPES:
        return None

    from_url = rule.from_url
    has_wildcard = from_url.endswith("*")
    if has_wildcard:
        from_url = from_url[:-1]

    if rule.type == "page":
        return _Pattern(version="*", prefix=from_url, has_wildcard=has_wildcard)

    # exact: expect /<language_prefix>/<version>/<path>
    pattern = re.compile(rf"^{re.escape(language_prefix)}/([^/]+)(/.*)?$")
    match = pattern.match(from_url)
    if match:
        version = match.group(1)
        path = match.group(2) or "/"
        return _Pattern(version=version, prefix=path, has_wildcard=has_wildcard)

    # Exact rule without a language/version prefix — unusual but allowed.
    # Mark version as unknown so cross-type comparisons fall through safely.
    return _Pattern(version=None, prefix=from_url, has_wildcard=has_wildcard)


def _is_strict_subset(a: _Pattern, b: _Pattern) -> bool:
    """True iff every URL matched by ``a`` is also matched by ``b``, with a != b."""
    if a == b:
        return False
    if not _version_subset(a.version, b.version):
        return False
    return _path_subset(a.prefix, a.has_wildcard, b.prefix, b.has_wildcard)


def _version_subset(av: str | None, bv: str | None) -> bool:
    if av is None or bv is None:
        return av == bv
    if bv == "*":
        return True
    return av == bv


def _path_subset(ap: str, aw: bool, bp: str, bw: bool) -> bool:
    if bw:
        return ap.startswith(bp)
    # B is an exact path; A must also be exact and equal.
    if aw:
        return False
    return ap == bp


def _patterns_overlap(a: _Pattern, b: _Pattern) -> bool:
    """True iff ``a`` and ``b`` share at least one URL."""
    if not _versions_overlap(a.version, b.version):
        return False
    return _paths_overlap(a.prefix, a.has_wildcard, b.prefix, b.has_wildcard)


def _versions_overlap(av: str | None, bv: str | None) -> bool:
    if av is None or bv is None:
        return False
    if av == "*" or bv == "*":
        return True
    return av == bv


def _paths_overlap(ap: str, aw: bool, bp: str, bw: bool) -> bool:
    if aw and bw:
        return ap.startswith(bp) or bp.startswith(ap)
    if aw:
        return bp.startswith(ap)
    if bw:
        return ap.startswith(bp)
    return ap == bp


def _check_ordering(
    rules: list[Redirect],
    patterns: list[_Pattern | None],
) -> list[Finding]:
    findings: list[Finding] = []
    for i, (a, pa) in enumerate(zip(rules, patterns, strict=True)):
        if pa is None:
            continue
        for b, pb in zip(rules[i + 1:], patterns[i + 1:], strict=True):
            if pb is None:
                continue
            # rules are sorted by (position, from_url, type), so a comes
            # before b in apply order. A "more specific" rule must come
            # first; if b is strictly more specific than a, b is unreachable.
            if _is_strict_subset(pb, pa):
                findings.append(Finding(
                    severity="error",
                    kind="ordering",
                    message=(
                        f"'{b.from_url}' ({b.type}, position {b.position}) is "
                        f"strictly more specific than '{a.from_url}' "
                        f"({a.type}, position {a.position}) and is unreachable; "
                        f"give the specific rule a lower position so it fires first"
                    ),
                    rules=(b, a),
                ))
    return findings


def _check_chains(
    rules: list[Redirect],
    patterns: list[_Pattern | None],
    language_prefix: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for a, pa in zip(rules, patterns, strict=True):
        if pa is None or not a.to_url or is_external(a.to_url):
            continue
        target = _target_pattern(a.to_url, language_prefix)
        if target is None:
            continue
        for b, pb in zip(rules, patterns, strict=True):
            if pb is None or b.identity == a.identity:
                continue
            if _patterns_overlap(target, pb):
                findings.append(Finding(
                    severity="warning",
                    kind="chain",
                    message=(
                        f"'{a.from_url}' redirects to '{a.to_url}' which may "
                        f"match '{b.from_url}' ({b.type}) — request would "
                        f"chain client-side. Rewrite '{a.from_url}' to point "
                        f"directly at the final destination."
                    ),
                    rules=(a, b),
                ))
    return findings


def _target_pattern(to_url: str, language_prefix: str) -> _Pattern | None:
    """Best-effort pattern describing the URLs a redirect's ``to`` could yield.

    ``:splat`` in the target makes the result a wildcard pattern over the
    literal prefix preceding the placeholder. Without ``:splat`` the target
    is a single literal URL.
    """
    splat_idx = to_url.find(":splat")
    if splat_idx >= 0:
        prefix = to_url[:splat_idx]
        has_wildcard = True
    else:
        prefix = to_url
        has_wildcard = False

    # Reuse the from-side parsing so versioned vs page-shaped targets line up.
    pattern_re = re.compile(rf"^{re.escape(language_prefix)}/([^/]+)(/.*)?$")
    match = pattern_re.match(prefix)
    if match:
        version = match.group(1)
        path = match.group(2) or "/"
        return _Pattern(version=version, prefix=path, has_wildcard=has_wildcard)

    # Path-only target (page-style, or a target authored without /en/<v>/)
    return _Pattern(version="*", prefix=prefix, has_wildcard=has_wildcard)
