"""Tests for rtd_redirects.simulate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rtd_redirects.model import Redirect, RedirectSet
from rtd_redirects.simulate import (
    WILDCARD_SAMPLE,
    RenameMap,
    SimulateError,
    TestUrl,
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


def _rs(*rules: tuple[str, str, str]) -> RedirectSet:
    return RedirectSet(
        Redirect(from_url=f, to_url=t, type=ty, position=i) for i, (f, t, ty) in enumerate(rules)
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


# A move of /old/ to /new/ that has landed on master only. latest and an older
# release still serve /old/.
OLD = {"/index.html", "/old/a.html", "/old/b.html"}
NEW = {"/index.html", "/new/a.html", "/new/b.html"}
MATRIX = {
    "master": VersionPages(before=OLD, after=NEW),
    "latest": VersionPages(before=OLD, after=OLD),
    "v1": VersionPages(before=OLD, after=OLD),
}
MOVE = RenameMap(prefixes=[("/old/", "/new/:splat")])


def _verdicts(report, version: str) -> dict[str, str]:
    return {o.url: o.verdict for o in report.for_version(version)}


class TestRenameMap:
    def test_exact_wins_over_prefix(self):
        rm = RenameMap({"/old/a.html": "/special.html"}, [("/old/", "/new/:splat")])
        assert rm.apply("/old/a.html") == "/special.html"
        assert rm.apply("/old/b.html") == "/new/b.html"

    def test_longest_prefix_wins(self):
        rm = RenameMap(prefixes=[("/a/", "/x/:splat"), ("/a/b/", "/y/:splat")])
        assert rm.apply("/a/b/c.html") == "/y/c.html"

    def test_steps_compose(self):
        rm = RenameMap({"/core/actors.html": "/core/actors/index.html"},
                       [("/ray-core/", "/core/:splat")])
        assert rm.apply("/ray-core/actors.html") == "/core/actors/index.html"

    def test_unmapped_path_is_unchanged(self):
        assert RenameMap().apply("/a.html") == "/a.html"
        assert not RenameMap()

    def test_cycle_terminates(self):
        rm = RenameMap({"/a.html": "/b.html", "/b.html": "/a.html"})
        assert rm.apply("/a.html") in ("/a.html", "/b.html")


class TestLoadRenameMap:
    def test_mapping(self, tmp_path: Path):
        f = tmp_path / "renames.yaml"
        f.write_text("/old/a.html: /new/a.html\n/old/*: /new/:splat\n")
        rm = load_rename_map(f)
        assert rm.exact == {"/old/a.html": "/new/a.html"}
        assert rm.apply("/old/z.html") == "/new/z.html"

    def test_list_under_renames_key(self, tmp_path: Path):
        f = tmp_path / "renames.yaml"
        f.write_text("renames:\n  - from: /a.html\n    to: /b.html\n")
        assert load_rename_map(f).exact == {"/a.html": "/b.html"}

    def test_wrong_shape(self, tmp_path: Path):
        f = tmp_path / "renames.yaml"
        f.write_text("- /a.html\n")
        with pytest.raises(SimulateError, match="expected a mapping"):
            load_rename_map(f)

    def test_non_string_entry(self, tmp_path: Path):
        f = tmp_path / "renames.yaml"
        f.write_text("/a.html: 3\n")
        with pytest.raises(SimulateError, match="must be strings"):
            load_rename_map(f)

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(SimulateError, match="couldn't read"):
            load_rename_map(tmp_path / "nope.yaml")


class TestRenamesFromGit:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "t")
        _git(tmp_path, "config", "commit.gpgsign", "false")
        src = tmp_path / "doc" / "source" / "ray-core"
        src.mkdir(parents=True)
        (src / "actors.md").write_text("actors\n" * 20)
        (tmp_path / "README.md").write_text("readme\n" * 20)
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-qm", "base")
        _git(tmp_path, "tag", "base")
        (tmp_path / "doc" / "source" / "core" / "actors").mkdir(parents=True)
        _git(tmp_path, "mv", "doc/source/ray-core/actors.md", "doc/source/core/actors/index.md")
        _git(tmp_path, "mv", "README.md", "doc/source/README.txt")
        return tmp_path

    def test_renames_against_worktree(self, repo: Path):
        rm = renames_from_git("doc/source", base_ref="base", head_ref="WORKTREE", repo_path=repo)
        assert rm.exact == {"/ray-core/actors.html": "/core/actors/index.html"}

    def test_renames_between_refs(self, repo: Path):
        _git(repo, "commit", "-qm", "move")
        rm = renames_from_git("doc/source/", base_ref="base", head_ref="HEAD", repo_path=repo)
        assert rm.exact == {"/ray-core/actors.html": "/core/actors/index.html"}

    def test_bad_ref(self, repo: Path):
        with pytest.raises(SimulateError, match="git diff"):
            renames_from_git("doc/source", base_ref="nope", head_ref="HEAD", repo_path=repo)


class TestTestUrls:
    def test_parse_full_url_pins_version(self):
        assert parse_test_url("https://docs.example.com/en/v1/a.html#x") == TestUrl("/a.html", "v1")

    def test_parse_versioned_path(self):
        assert parse_test_url("/en/latest") == TestUrl("/", "latest")

    def test_parse_plain_path_runs_everywhere(self):
        assert parse_test_url("/a.html") == TestUrl("/a.html")

    def test_rule_sources(self):
        rules = _rs(
            ("/a.html", "/b.html", "page"),
            ("/old/*", "/new/:splat", "page"),
            ("/a.html#frag", "/b.html", "page"),
            ("/en/v1/x.html", "/y.html", "exact"),
            ("/unversioned.html", "/y.html", "exact"),
        )
        rules.add(Redirect(from_url="", to_url="", type="html_to_clean_url", position=9))
        assert rule_source_tests([rules]) == {
            TestUrl("/a.html"),
            TestUrl(f"/old/{WILDCARD_SAMPLE}"),
            TestUrl("/x.html", "v1"),
        }

    def test_prefix_tests(self):
        assert prefix_tests(MATRIX, ["/old/"]) == {TestUrl("/old/a.html"), TestUrl("/old/b.html")}
        assert prefix_tests(MATRIX, []) == set()


class TestSimulate:
    def test_splat_move_is_clean_on_every_version(self):
        after = _rs(("/old/*", "/new/:splat", "page"))
        report = simulate(_rs(), after, MATRIX, prefix_tests(MATRIX, ["/old/"]), renames=MOVE)
        assert not report.failures
        assert _verdicts(report, "master") == {"/old/a.html": "ok", "/old/b.html": "ok"}
        # force: false leaves latest and v1 serving the original pages.
        assert {o.after.hops for o in report.for_version("v1")} == {0}

    def test_missing_redirect_is_a_regression(self):
        report = simulate(_rs(), _rs(), MATRIX, [TestUrl("/old/a.html")], renames=MOVE)
        assert _verdicts(report, "master") == {"/old/a.html": "regression"}
        assert _verdicts(report, "latest") == {"/old/a.html": "ok"}

    def test_catch_all_shadowing_is_a_wrong_landing(self):
        # An existing catch-all placed before the new splat fires first and
        # sends moved pages to a landing page.
        after = _rs(("/old/*", "/index.html", "page"), ("/old/a.html", "/new/a.html", "page"))
        report = simulate(_rs(), after, MATRIX, [TestUrl("/old/a.html")], renames=MOVE)
        outcome = report.for_version("master")[0]
        assert outcome.verdict == "wrong-landing"
        assert outcome.expected == ("master", "/new/a.html")
        assert outcome.after.landing == ("master", "/index.html")

    def test_repointed_target_regresses_on_older_versions(self):
        # /legacy.html has long redirected to /old/a.html. Repointing it at the
        # new path is right for master and breaks every version without the move.
        before = _rs(("/legacy.html", "/old/a.html", "page"))
        after = _rs(("/legacy.html", "/new/a.html", "page"), ("/old/*", "/new/:splat", "page"))
        report = simulate(before, after, MATRIX, [TestUrl("/legacy.html")], renames=MOVE)
        assert _verdicts(report, "master") == {"/legacy.html": "ok"}
        assert _verdicts(report, "latest") == {"/legacy.html": "regression"}
        assert _verdicts(report, "v1") == {"/legacy.html": "regression"}

    def test_keeping_the_old_target_chains_but_holds(self):
        before = _rs(("/legacy.html", "/old/a.html", "page"))
        after = _rs(("/legacy.html", "/old/a.html", "page"), ("/old/*", "/new/:splat", "page"))
        report = simulate(
            before, after, MATRIX, [TestUrl("/legacy.html")], renames=MOVE, hop_budget=2,
        )
        assert not report.failures
        master = report.for_version("master")[0]
        assert (master.after.path, master.after.hops) == ("/new/a.html", 2)

    def test_hop_budget(self):
        before = _rs(("/legacy.html", "/old/a.html", "page"))
        after = _rs(("/legacy.html", "/old/a.html", "page"), ("/old/*", "/new/:splat", "page"))
        report = simulate(
            before, after, MATRIX, [TestUrl("/legacy.html")], renames=MOVE, hop_budget=1,
        )
        assert [(o.version, o.over_budget) for o in report.failures] == [("master", True)]

    def test_new_loop(self):
        after = _rs(("/x/*", "/x/:splat", "page"))
        report = simulate(_rs(), after, MATRIX, [TestUrl("/x/a.html")])
        assert set(_verdicts(report, "master").values()) == {"new-loop"}
        assert report.failures

    def test_fixed_and_unresolved(self):
        after = _rs(("/gone.html", "/index.html", "page"))
        report = simulate(_rs(), after, MATRIX, [TestUrl("/gone.html"), TestUrl("/never.html")])
        assert _verdicts(report, "latest") == {"/gone.html": "fixed", "/never.html": "unresolved"}

    def test_moved_without_rename_is_unmapped(self):
        after = _rs(("/old/*", "/index.html", "page"))
        report = simulate(_rs(), after, MATRIX, [TestUrl("/old/a.html")])
        assert _verdicts(report, "master") == {"/old/a.html": "unmapped"}
        assert not report.failures

    def test_jump_to_version_outside_matrix_is_unverified(self):
        after = _rs(("/a.html", "/en/v9/a.html", "page"))
        report = simulate(_rs(), after, MATRIX, [TestUrl("/a.html")])
        assert set(_verdicts(report, "latest").values()) == {"unverified"}

    def test_version_switch_compares_on_the_landing_version(self):
        # A rule into /en/master/ makes a latest request land on master, where
        # the page moved. The rename applies there, so this isn't a wrong landing.
        before = _rs(("/legacy.html", "/en/master/old/a.html", "page"))
        after = _rs(
            ("/legacy.html", "/en/master/old/a.html", "page"),
            ("/old/*", "/new/:splat", "page"),
        )
        report = simulate(before, after, MATRIX, [TestUrl("/legacy.html")], renames=MOVE)
        latest = report.for_version("latest")[0]
        assert latest.verdict == "ok"
        assert latest.after.landing == ("master", "/new/a.html")

    def test_external_landing(self):
        rules = _rs(("/a.html", "https://example.com/", "page"))
        report = simulate(rules, rules, MATRIX, [TestUrl("/a.html")])
        assert set(_verdicts(report, "latest").values()) == {"ok"}

    def test_pinned_test_runs_on_its_version_only(self):
        report = simulate(_rs(), _rs(), MATRIX, [TestUrl("/index.html", "v1"), TestUrl("/x", "v9")])
        assert [(o.version, o.url) for o in report.outcomes] == [("v1", "/index.html")]

    def test_outcomes_follow_matrix_order(self):
        report = simulate(_rs(), _rs(), MATRIX, [TestUrl("/index.html")])
        assert [o.version for o in report.outcomes] == ["master", "latest", "v1"]


class TestOutput:
    @pytest.fixture
    def report(self):
        before = _rs(("/legacy.html", "/old/a.html", "page"))
        after = _rs(("/legacy.html", "/new/a.html", "page"), ("/old/*", "/new/:splat", "page"))
        return simulate(
            before, after, MATRIX,
            [TestUrl("/legacy.html"), TestUrl("/old/b.html"), TestUrl("/index.html")],
            renames=MOVE, hop_budget=1,
        )

    def test_summarize_is_json_serializable(self, report):
        summary = json.loads(json.dumps(summarize(report)))
        assert summary["failures"] == 2
        assert summary["versions"]["latest"]["verdicts"] == {"ok": 2, "regression": 1}
        assert summary["versions"]["master"]["hops_after"] == {"0": 1, "1": 2}
        regression = next(o for o in summary["outcomes"] if o["verdict"] == "regression")
        assert regression["after"] == {
            "version": "latest", "path": "/new/a.html", "hops": 1,
            "status": "404", "via": ["/legacy.html"],
        }

    def test_format_report(self, report):
        text = format_report(report)
        assert "/en/latest/: 3 URLs" in text
        assert "regressions: 1" in text
        assert "over hop budget (more than 1): 0" in text
        assert (
            "/legacy.html: before ok [latest] /old/a.html (1 hop) via /legacy.html; "
            "after 404 [latest] /new/a.html (1 hop) via /legacy.html"
        ) in text
        assert text.endswith("simulate: FAIL, 2 failing URL check(s)")

    def test_format_report_wrong_landing_and_limit(self):
        after = _rs(("/old/*", "/index.html", "page"))
        tests = [TestUrl("/old/a.html"), TestUrl("/old/b.html")]
        text = format_report(simulate(_rs(), after, MATRIX, tests, renames=MOVE), limit=1)
        assert "expected [master] /new/a.html" in text
        assert "... 1 more" in text

    def test_format_report_ok(self):
        text = format_report(simulate(_rs(), _rs(), MATRIX, [TestUrl("/index.html")]))
        assert "over hop budget" not in text
        assert text.endswith("simulate: ok, no regressions, wrong landings, or new loops")
