"""Tests for rtd_redirects.validate: ordering + chain detection."""

from __future__ import annotations

from rtd_redirects.model import Redirect, RedirectSet
from rtd_redirects.validate import (
    _is_strict_subset,
    _pattern_for,
    _patterns_overlap,
    fix_ordering,
    validate,
)


def _r(
    from_url: str,
    to_url: str = "/dest",
    *,
    type: str = "exact",
    position: int = 0,
    force: bool = False,
) -> Redirect:
    return Redirect(
        from_url=from_url, to_url=to_url, type=type,
        position=position, force=force,
    )


class TestPatternExtraction:
    def test_page_rule_yields_star_version(self):
        p = _pattern_for(_r("/api/foo.html", type="page"), "/en")
        assert p is not None
        assert p.version == "*"
        assert p.prefix == "/api/foo.html"
        assert p.has_wildcard is False

    def test_page_with_wildcard(self):
        p = _pattern_for(_r("/api/*", type="page"), "/en")
        assert p == _pattern_for(_r("/api/*", type="page"), "/en")  # equal
        assert p.has_wildcard is True
        assert p.prefix == "/api/"

    def test_exact_yields_literal_version(self):
        p = _pattern_for(_r("/en/latest/api/foo.html", type="exact"), "/en")
        assert p.version == "latest"
        assert p.prefix == "/api/foo.html"
        assert p.has_wildcard is False

    def test_exact_with_wildcard(self):
        p = _pattern_for(_r("/en/v2.40/api/*", type="exact"), "/en")
        assert p.version == "v2.40"
        assert p.prefix == "/api/"
        assert p.has_wildcard is True

    def test_url_style_types_return_none(self):
        assert _pattern_for(_r("", type="clean_url_to_html"), "/en") is None
        assert _pattern_for(_r("", type="html_to_clean_url"), "/en") is None

    def test_exact_without_language_prefix_marks_version_unknown(self):
        p = _pattern_for(_r("/api/foo.html", type="exact"), "/en")
        assert p.version is None


class TestSubset:
    def test_exact_under_page_is_subset(self):
        a = _pattern_for(_r("/en/latest/api/foo.html", type="exact"), "/en")
        b = _pattern_for(_r("/api/foo.html", type="page"), "/en")
        assert _is_strict_subset(a, b)

    def test_more_specific_wildcard_is_subset(self):
        a = _pattern_for(_r("/api/v1/*", type="page"), "/en")
        b = _pattern_for(_r("/api/*", type="page"), "/en")
        assert _is_strict_subset(a, b)

    def test_specific_path_is_subset_of_wildcard(self):
        a = _pattern_for(_r("/en/latest/api/foo.html", type="exact"), "/en")
        b = _pattern_for(_r("/en/latest/api/*", type="exact"), "/en")
        assert _is_strict_subset(a, b)

    def test_different_versions_not_subset(self):
        a = _pattern_for(_r("/en/latest/foo.html", type="exact"), "/en")
        b = _pattern_for(_r("/en/master/foo.html", type="exact"), "/en")
        assert not _is_strict_subset(a, b)
        assert not _is_strict_subset(b, a)

    def test_disjoint_paths_not_subset(self):
        a = _pattern_for(_r("/foo/*", type="page"), "/en")
        b = _pattern_for(_r("/bar/*", type="page"), "/en")
        assert not _is_strict_subset(a, b)
        assert not _is_strict_subset(b, a)

    def test_identical_patterns_not_strict_subset(self):
        a = _pattern_for(_r("/foo/*", type="page"), "/en")
        b = _pattern_for(_r("/foo/*", type="page"), "/en")
        assert not _is_strict_subset(a, b)


class TestOverlap:
    def test_disjoint_paths(self):
        a = _pattern_for(_r("/foo/*", type="page"), "/en")
        b = _pattern_for(_r("/bar/*", type="page"), "/en")
        assert not _patterns_overlap(a, b)

    def test_overlapping_via_subset(self):
        a = _pattern_for(_r("/api/v1/*", type="page"), "/en")
        b = _pattern_for(_r("/api/*", type="page"), "/en")
        assert _patterns_overlap(a, b)

    def test_overlapping_via_cross_type(self):
        a = _pattern_for(_r("/en/latest/api/foo.html", type="exact"), "/en")
        b = _pattern_for(_r("/api/*", type="page"), "/en")
        assert _patterns_overlap(a, b)

    def test_disjoint_versions(self):
        a = _pattern_for(_r("/en/latest/*", type="exact"), "/en")
        b = _pattern_for(_r("/en/master/*", type="exact"), "/en")
        assert not _patterns_overlap(a, b)


class TestOrderingFindings:
    def test_well_ordered_set_has_no_findings(self):
        rs = RedirectSet([
            _r("/api/v1/foo.html", "/api/v1/new.html", type="page", position=0),
            _r("/api/*", "/api/v2/:splat", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        assert findings == []

    def test_more_specific_with_higher_position_flagged(self):
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "/api/v1/foo.html" in findings[0].message
        assert "more specific" in findings[0].message

    def test_disjoint_rules_no_ordering_finding(self):
        rs = RedirectSet([
            _r("/foo/*", "/new-foo/:splat", type="page", position=0),
            _r("/bar/*", "/new-bar/:splat", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        assert findings == []

    def test_exact_more_specific_than_page(self):
        """exact /en/latest/api/foo.html is unreachable after page /api/* fires."""
        rs = RedirectSet([
            _r("/api/*", "/new/:splat", type="page", position=0),
            _r("/en/latest/api/foo.html", "/new.html", type="exact", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        assert len(findings) == 1
        assert "/en/latest/api/foo.html" in findings[0].message

    def test_url_style_types_not_flagged(self):
        """URL-style types have no from URL to compare; they never cause ordering findings."""
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("", "", type="clean_url_to_html", position=1),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=2),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        # only the page/page mis-order is flagged; clean_url_to_html doesn't participate
        assert len(findings) == 1
        assert all(r.type != "clean_url_to_html" for r in findings[0].rules)


class TestChainFindings:
    def test_direct_chain_flagged(self):
        rs = RedirectSet([
            _r("/old.html", "/intermediate.html", type="page", position=0),
            _r("/intermediate.html", "/current.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "chain"]
        assert len(findings) >= 1
        # The /old.html -> /intermediate.html -> /current.html chain is flagged.
        chain_messages = [f.message for f in findings]
        assert any("/old.html" in m and "/intermediate.html" in m for m in chain_messages)

    def test_splat_target_creates_chain_candidate(self):
        rs = RedirectSet([
            _r("/old/*", "/new/:splat", type="page", position=0),
            _r("/new/something.html", "/final.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "chain"]
        assert any("/old/*" in f.message for f in findings)

    def test_external_target_no_chain(self):
        rs = RedirectSet([
            _r("/old.html", "https://docs.anyscale.com/new", type="page", position=0),
            _r("/something-else.html", "/x.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "chain"]
        assert findings == []

    def test_disjoint_targets_no_chain(self):
        rs = RedirectSet([
            _r("/old.html", "/new.html", type="page", position=0),
            _r("/other.html", "/elsewhere.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "chain"]
        assert findings == []


class TestDeterminism:
    def test_findings_are_byte_stable_across_runs(self):
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
            _r("/old.html", "/intermediate.html", type="page", position=2),
            _r("/intermediate.html", "/final.html", type="page", position=3),
        ])
        a = [(f.severity, f.kind, f.message) for f in validate(rs)]
        b = [(f.severity, f.kind, f.message) for f in validate(rs)]
        assert a == b


class TestFixOrdering:
    def test_swaps_unreachable_pair(self):
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
        ])
        fixed = fix_ordering(rs)
        positions = {r.from_url: r.position for r in fixed}
        assert positions["/api/v1/foo.html"] < positions["/api/*"]

    def test_already_correct_set_unchanged(self):
        rs = RedirectSet([
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=0),
            _r("/api/*", "/v2/:splat", type="page", position=1),
        ])
        fixed = fix_ordering(rs)
        # All positions stay the same.
        for original, after in zip(sorted(rs, key=lambda r: r.from_url),
                                    sorted(fixed, key=lambda r: r.from_url),
                                    strict=True):
            assert original.position == after.position

    def test_three_level_chain(self):
        """A ⊊ B ⊊ C should produce A.pos < B.pos < C.pos."""
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),  # most general
            _r("/api/v1/*", "/v2/v1/:splat", type="page", position=1),
            _r("/api/v1/foo.html", "/v2/v1/foo.html", type="page", position=2),  # most specific
        ])
        fixed = fix_ordering(rs)
        positions = {r.from_url: r.position for r in fixed}
        assert positions["/api/v1/foo.html"] < positions["/api/v1/*"]
        assert positions["/api/v1/*"] < positions["/api/*"]

    def test_disjoint_groups_kept_near_original_position(self):
        rs = RedirectSet([
            _r("/foo/*", "/new-foo/:splat", type="page", position=0),
            _r("/bar/*", "/new-bar/:splat", type="page", position=1),
        ])
        fixed = fix_ordering(rs)
        positions = {r.from_url: r.position for r in fixed}
        # Stable tiebreaker by original position
        assert positions["/foo/*"] == 0
        assert positions["/bar/*"] == 1

    def test_fix_is_deterministic(self):
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
            _r("/data/old.html", "/data/new.html", type="page", position=2),
        ])
        a = sorted(fix_ordering(rs), key=lambda r: r.position)
        b = sorted(fix_ordering(rs), key=lambda r: r.position)
        assert [(r.from_url, r.position) for r in a] == [(r.from_url, r.position) for r in b]

    def test_validate_after_fix_is_clean(self):
        """Round-trip: fix_ordering then validate should produce no ordering errors."""
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
            _r("/api/v1/*", "/v2/v1/:splat", type="page", position=2),
        ])
        fixed = fix_ordering(rs)
        ordering_findings = [f for f in validate(fixed) if f.kind == "ordering"]
        assert ordering_findings == []


class TestForceInteraction:
    def test_force_true_more_general_still_shadows_specific(self):
        """A force:true rule with lower position still shadows a more specific later rule."""
        rs = RedirectSet([
            _r("/api/*", "/v2/:splat", type="page", position=0, force=True),
            _r("/api/v1/foo.html", "/v2/foo.html", type="page", position=1),
        ])
        findings = [f for f in validate(rs) if f.kind == "ordering"]
        assert len(findings) == 1
