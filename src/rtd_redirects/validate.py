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

Chain candidates are tiered by whether the author can act on them. ``force``
defaults to ``false`` on RtD, so B only fires when A's target would itself
404. When B is a broad wildcard catch-all that lands on a fixed page (its
``to`` has no ``:splat``), the overlap is **benign**: on versions where A's
target exists there's no chain, and on versions where it 404s the catch-all
is the intended graceful-degradation fallback — a prefix catch-all can't be
pointed past, so there's no rewrite that removes the overlap. These emit at
``info``. A candidate where B is a *specific* rule, or a path-preserving
wildcard move (B's ``to`` carries ``:splat``), would send A's target to a
*different* destination the author should route to directly; those stay
``warning``. The exception is a *preempted splat*: when A is a ``/P/* ->
/Q/:splat`` move and B is a specific ``/Q/<tail>``, the only source that drives
A into B is ``/P/<tail>``; if a lower-position rule already matches that
source, A never fires there, so the overlap can't chain and emits ``info``.
The tiering is structural and offline; it doesn't prove A's target resolves to
a live page, so a ``warning`` is the actionable signal and ``info`` is a note,
not a guarantee.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Literal

from rtd_redirects.expand import DEFAULT_LANGUAGE_PREFIX, is_external
from rtd_redirects.model import URL_STYLE_TYPES, Redirect, RedirectSet

Severity = Literal["error", "warning", "info"]
Kind = Literal["ordering", "chain"]


@dataclass(frozen=True)
class Finding:
    """One validation issue discovered in a ``RedirectSet``."""

    severity: Severity
    kind: Kind
    message: str
    rules: tuple[Redirect, ...] = field(default_factory=tuple)


def fix_ordering(
    rs: RedirectSet,
    *,
    language_prefix: str = DEFAULT_LANGUAGE_PREFIX,
) -> RedirectSet:
    """Reorder rules so every (A ⊊ B) pair has ``A.position < B.position``.

    Deterministic: sorts by ``(-superset_count, original_position, from_url, type)``.
    A rule with more supersets is more specific and needs to come first;
    original position breaks ties so disjoint groups stay near where the
    author placed them; from_url and type are final tiebreakers for
    byte-stable output.

    URL-style types (no comparable ``from`` URL) sort by original position
    only and keep their relative order.

    Only ``position`` fields change. Identity and data fields are preserved.
    """
    rules = list(rs)
    patterns = [_pattern_for(r, language_prefix) for r in rules]

    counts = [
        sum(
            1 for j, pj in enumerate(patterns)
            if i != j and pi is not None and pj is not None
            and _is_strict_subset(pi, pj)
        )
        for i, pi in enumerate(patterns)
    ]

    indexed = sorted(
        range(len(rules)),
        key=lambda i: (
            -counts[i],
            rules[i].position,
            rules[i].from_url,
            rules[i].type,
        ),
    )

    new_rules: list[Redirect] = []
    for new_position, original_index in enumerate(indexed):
        rule = rules[original_index]
        if rule.position == new_position:
            new_rules.append(rule)
        else:
            new_rules.append(dataclasses.replace(rule, position=new_position))
    return RedirectSet(new_rules)


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
            if not _patterns_overlap(target, pb):
                continue
            if _is_benign_catchall_overlap(target, b, pb):
                findings.append(Finding(
                    severity="info",
                    kind="chain",
                    message=(
                        f"'{a.from_url}' redirects to '{a.to_url}', which falls "
                        f"under the fixed-page catch-all '{b.from_url}' "
                        f"({b.type}). Benign: force=false means the catch-all "
                        f"fires only if '{a.to_url}' 404s, and a prefix "
                        f"catch-all can't be pointed past. It chains only on "
                        f"versions where '{a.to_url}' itself 404s, where the "
                        f"catch-all is the intended fallback."
                    ),
                    rules=(a, b),
                ))
                continue
            source = _preempted_splat_source(a, pa, target, pb, rules, patterns)
            if source is not None:
                findings.append(Finding(
                    severity="info",
                    kind="chain",
                    message=(
                        f"'{a.from_url}' redirects to '{a.to_url}', whose splat "
                        f"could reach '{b.from_url}' ({b.type}) for source "
                        f"'{source}'. Benign: a lower-position rule preempts "
                        f"'{a.from_url}' for '{source}', so '{a.from_url}' never "
                        f"produces that target and the chain can't fire."
                    ),
                    rules=(a, b),
                ))
                continue
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


def _preempted_splat_source(
    a: Redirect,
    pa: _Pattern,
    target: _Pattern,
    pb: _Pattern,
    rules: list[Redirect],
    patterns: list[_Pattern | None],
) -> str | None:
    """Source path for an A→B splat overlap that a lower-position rule preempts.

    When A is a splat move (``/P/* -> /Q/:splat``) and B is a *specific* rule
    ``/Q/<tail>``, the only request that would drive A into B is ``/P/<tail>``.
    A fires there only if no earlier rule matches it. If a rule at a lower
    ``position`` than A already matches ``/P/<tail>`` across all versions A
    covers, A never produces B's ``from`` and the chain can't fire.

    Returns the reconstructed source path when preempted, else ``None``. Only
    handles the page-wildcard case; ``exact`` (single-version) preemptors are
    ignored so a per-version gap isn't hidden.
    """
    if not pa.has_wildcard or ":splat" not in a.to_url:
        return None
    if pb.has_wildcard:
        return None  # B is itself broad; not a specific-target overlap
    if not pb.prefix.startswith(target.prefix):
        return None
    splat_value = pb.prefix[len(target.prefix):]
    source = _Pattern(version="*", prefix=pa.prefix + splat_value, has_wildcard=False)
    for c, pc in zip(rules, patterns, strict=True):
        if pc is None or c.identity == a.identity or c.position >= a.position:
            continue
        if source == pc or _is_strict_subset(source, pc):
            return source.prefix
    return None


def _is_benign_catchall_overlap(
    target: _Pattern,
    b: Redirect,
    pb: _Pattern,
) -> bool:
    """True iff A's ``target`` overlaps B only as a fixed-page catch-all.

    Benign means: B is a broad wildcard rule (``pb.has_wildcard``) that lands
    every match on one fixed page (B's ``to`` has no ``:splat``), and B's match
    set contains all of A's target. Because ``force`` defaults to ``false``, B
    fires only when A's target would 404, and a prefix catch-all is
    unavoidable, so there's no rewrite of A that removes the overlap — on
    versions where A's target exists there's no chain, and where it 404s the
    catch-all is the intended fallback.

    A specific (non-wildcard) B, or a path-preserving wildcard move whose
    ``to`` carries ``:splat``, is *not* benign: it would route A's target to a
    different destination, which the author should point at directly.
    """
    if not pb.has_wildcard:
        return False
    if ":splat" in b.to_url:
        return False
    return _version_subset(target.version, pb.version) and _path_subset(
        target.prefix, target.has_wildcard, pb.prefix, pb.has_wildcard
    )


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
