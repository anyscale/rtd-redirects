"""Tests for rtd_redirects.resolve."""

from __future__ import annotations

import pytest

from rtd_redirects.model import Redirect, RedirectSet
from rtd_redirects.resolve import Resolver, match


def _rs(*rules: tuple) -> RedirectSet:
    """Build a set from ``(from, to, type[, kwargs])`` tuples in position order."""
    out = []
    for i, rule in enumerate(rules):
        frm, to, type_, *rest = rule
        extra = rest[0] if rest else {}
        out.append(Redirect(from_url=frm, to_url=to, type=type_, position=i, **extra))
    return RedirectSet(out)


def _resolver(rules: RedirectSet, **pages: set[str]) -> Resolver:
    return Resolver(rules, pages)


class TestMatch:
    def test_page_rule_matches_version_relative_path(self):
        rule = Redirect(from_url="/a.html", to_url="/b.html", type="page")
        assert match(rule, "/a.html", "/en/latest/a.html") == ""

    def test_exact_rule_matches_full_path(self):
        rule = Redirect(from_url="/en/latest/a.html", to_url="/b.html", type="exact")
        assert match(rule, "/a.html", "/en/latest/a.html") == ""
        assert match(rule, "/a.html", "/en/master/a.html") is None

    def test_wildcard_returns_splat(self):
        rule = Redirect(from_url="/old/*", to_url="/new/:splat", type="page")
        assert match(rule, "/old/x/y.html", "/en/latest/old/x/y.html") == "x/y.html"
        assert match(rule, "/older/y.html", "/en/latest/older/y.html") is None

    def test_fragment_source_never_matches(self):
        rule = Redirect(from_url="/a.html#sec", to_url="/b.html", type="page")
        assert match(rule, "/a.html#sec", "/en/latest/a.html#sec") is None


class TestResolve:
    def test_existing_page_with_no_rule_is_ok(self):
        r = _resolver(_rs(), latest={"/a.html"}).resolve("latest", "/a.html")
        assert (r.status, r.path, r.hops) == ("ok", "/a.html", 0)

    def test_missing_page_with_no_rule_is_404(self):
        r = _resolver(_rs(), latest=set()).resolve("latest", "/a.html")
        assert (r.status, r.hops) == ("404", 0)
        assert not r.resolved

    def test_directory_url_resolves_to_its_index(self):
        r = _resolver(_rs(), latest={"/core/index.html"}).resolve("latest", "/core/")
        assert r.status == "ok"

    def test_first_match_wins_by_position(self):
        rules = _rs(
            ("/old/*", "/landing.html", "page"),
            ("/old/a.html", "/new/a.html", "page"),
        )
        resolver = _resolver(rules, latest={"/landing.html", "/new/a.html"})
        r = resolver.resolve("latest", "/old/a.html")
        assert r.path == "/landing.html"
        assert [x.from_url for x in r.rules] == ["/old/*"]

    def test_force_false_does_not_fire_on_existing_page(self):
        rules = _rs(("/a.html", "/b.html", "page"))
        r = _resolver(rules, latest={"/a.html", "/b.html"}).resolve("latest", "/a.html")
        assert (r.path, r.hops) == ("/a.html", 0)

    def test_force_true_fires_on_existing_page(self):
        rules = _rs(("/a.html", "/b.html", "page", {"force": True}))
        r = _resolver(rules, latest={"/a.html", "/b.html"}).resolve("latest", "/a.html")
        assert (r.path, r.hops, r.status) == ("/b.html", 1, "ok")

    def test_exact_rule_fires_only_on_its_version(self):
        rules = _rs(("/en/master/a.html", "/b.html", "exact"))
        resolver = _resolver(rules, master={"/b.html"}, latest={"/b.html"})
        assert resolver.resolve("master", "/a.html").path == "/b.html"
        assert resolver.resolve("latest", "/a.html").status == "404"

    def test_splat_substitution(self):
        rules = _rs(("/old/*", "/new/:splat", "page"))
        r = _resolver(rules, latest={"/new/x/y.html"}).resolve("latest", "/old/x/y.html")
        assert (r.path, r.status, r.hops) == ("/new/x/y.html", "ok", 1)

    def test_chain_counts_each_hop(self):
        rules = _rs(("/a.html", "/b.html", "page"), ("/b.html", "/c.html", "page"))
        r = _resolver(rules, latest={"/c.html"}).resolve("latest", "/a.html")
        assert (r.path, r.hops) == ("/c.html", 2)

    def test_versioned_target_switches_page_set(self):
        rules = _rs(("/a.html", "/en/master/b.html", "page"))
        resolver = _resolver(rules, latest=set(), master={"/b.html"})
        r = resolver.resolve("latest", "/a.html")
        assert (r.version, r.path, r.status) == ("master", "/b.html", "ok")
        assert r.landing == ("master", "/b.html")

    def test_target_on_unknown_version_is_unmodeled(self):
        rules = _rs(("/a.html", "/en/v9/b.html", "page"))
        r = _resolver(rules, latest=set()).resolve("latest", "/a.html")
        assert (r.status, r.version) == ("unmodeled", "v9")

    def test_target_fragment_is_ignored_for_lookup(self):
        rules = _rs(("/a.html", "/b.html#sec", "page"))
        r = _resolver(rules, latest={"/b.html"}).resolve("latest", "/a.html")
        assert (r.path, r.status) == ("/b.html", "ok")

    def test_external_target_ends_resolution(self):
        rules = _rs(("/a.html", "https://example.com/x", "page"))
        r = _resolver(rules, latest=set()).resolve("latest", "/a.html")
        assert (r.status, r.path) == ("external", "https://example.com/x")
        assert r.resolved
        assert r.landing == ("", "https://example.com/x")

    def test_two_rule_cycle_is_a_loop(self):
        rules = _rs(("/a.html", "/b.html", "page"), ("/b.html", "/a.html", "page"))
        r = _resolver(rules, latest=set()).resolve("latest", "/a.html")
        assert r.status == "loop"
        assert not r.resolved

    def test_same_path_splat_is_a_loop(self):
        rules = _rs(("/x/*", "/x/:splat", "page"))
        r = _resolver(rules, latest=set()).resolve("latest", "/x/a.html")
        assert (r.status, r.hops) == ("loop", 1)

    def test_hop_limit_counts_as_loop(self):
        rules = _rs(*[(f"/p{i}.html", f"/p{i + 1}.html", "page") for i in range(5)])
        resolver = Resolver(rules, {"latest": {"/p5.html"}}, max_hops=3)
        assert resolver.resolve("latest", "/p0.html").status == "loop"
        assert Resolver(rules, {"latest": {"/p5.html"}}).resolve("latest", "/p0.html").hops == 5

    def test_disabled_and_url_style_rules_are_ignored(self):
        rules = RedirectSet([
            Redirect(from_url="", to_url="", type="html_to_clean_url", position=0),
            Redirect(from_url="/a.html", to_url="/b.html", type="page", enabled=False, position=1),
        ])
        r = _resolver(rules, latest={"/b.html"}).resolve("latest", "/a.html")
        assert r.status == "404"

    def test_custom_language_prefix(self):
        rules = _rs(("/ja/latest/a.html", "/ja/master/b.html", "exact"))
        resolver = Resolver(
            rules, {"latest": set(), "master": {"/b.html"}}, language_prefix="/ja",
        )
        r = resolver.resolve("latest", "/a.html")
        assert (r.version, r.path, r.status) == ("master", "/b.html", "ok")

    def test_unknown_version_raises(self):
        with pytest.raises(KeyError):
            _resolver(_rs(), latest=set()).resolve("master", "/a.html")
