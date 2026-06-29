"""Tests for rtd_redirects.parse: YAML reading and schema validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from rtd_redirects.model import Redirect, RedirectSet
from rtd_redirects.parse import ParseError, compose, parse_file, parse_files


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(content).lstrip("\n"))
    return p


def test_minimal_canonical_entry(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects:
          - from: /old.html
            to:   /new.html
            type: exact
    """)
    rs = parse_file(f)
    assert len(rs) == 1
    r = next(iter(rs))
    assert (r.from_url, r.to_url, r.type) == ("/old.html", "/new.html", "exact")
    assert r.http_status == 301
    assert r.force is False
    assert r.enabled is True
    assert r.description == ""
    assert r.position == 0


def test_full_entry_fields(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects:
          - from: /a
            to:   /b
            type: page
            status: 302
            force: true
            enabled: false
            position: 7
            description: "operator note"
    """)
    r = next(iter(parse_file(f)))
    assert r.http_status == 302
    assert r.force is True
    assert r.enabled is False
    assert r.position == 7
    assert r.description == "operator note"


def test_position_defaults_to_entry_index(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects:
          - from: /a
            to:   /b
            type: exact
          - from: /c
            to:   /d
            type: exact
          - from: /e
            to:   /f
            type: exact
    """)
    rs = parse_file(f)
    positions = [r.position for r in rs]  # iter is position-sorted
    assert positions == [0, 1, 2]


def test_explicit_position_overrides_index(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects:
          - from: /a
            to:   /b
            type: exact
            position: 10
          - from: /c
            to:   /d
            type: exact
            position: 5
    """)
    rs = parse_file(f)
    from_urls = [r.from_url for r in rs]  # position-sorted iteration
    assert from_urls == ["/c", "/a"]


def test_null_description_becomes_empty(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects:
          - from: /a
            to:   /b
            type: exact
            description: null
    """)
    r = next(iter(parse_file(f)))
    assert r.description == ""


def test_empty_redirects_list(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", """
        schema_version: 1
        redirects: []
    """)
    assert len(parse_file(f)) == 0


def test_missing_redirects_key(tmp_path: Path):
    f = _write(tmp_path, "redirects.yaml", "schema_version: 1\n")
    assert len(parse_file(f)) == 0


def test_empty_file(tmp_path: Path):
    f = _write(tmp_path, "empty.yaml", "")
    assert len(parse_file(f)) == 0


class TestSchemaVersion:
    def test_missing_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "redirects: []\n")
        with pytest.raises(ParseError, match="schema_version' is required"):
            parse_file(f)

    def test_unsupported_value_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "schema_version: 2\nredirects: []\n")
        with pytest.raises(ParseError, match="unsupported schema_version"):
            parse_file(f)

    def test_non_integer_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "schema_version: '1'\nredirects: []\n")
        with pytest.raises(ParseError, match="unsupported schema_version"):
            parse_file(f)


class TestStructureValidation:
    def test_top_level_not_mapping(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "- just\n- a\n- list\n")
        with pytest.raises(ParseError, match="top-level must be a mapping"):
            parse_file(f)

    def test_redirects_not_list(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "schema_version: 1\nredirects: 'oops'\n")
        with pytest.raises(ParseError, match="'redirects' must be a list"):
            parse_file(f)

    def test_entry_not_mapping(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - "just a string"
        """)
        with pytest.raises(ParseError, match=r"redirects\[0\] must be a mapping"):
            parse_file(f)

    def test_invalid_yaml(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", "schema_version: 1\nredirects:\n  - {unbalanced\n")
        with pytest.raises(ParseError, match="YAML parse error"):
            parse_file(f)

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ParseError, match="cannot read"):
            parse_file(tmp_path / "does-not-exist.yaml")


class TestEntryValidation:
    def test_missing_from_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - to: /b
                type: exact
        """)
        with pytest.raises(ParseError, match="missing required field 'from'"):
            parse_file(f)

    def test_missing_to_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                type: exact
        """)
        with pytest.raises(ParseError, match="missing required field 'to'"):
            parse_file(f)

    def test_missing_type_raises(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
        """)
        with pytest.raises(ParseError, match="missing required field 'type'"):
            parse_file(f)

    def test_from_must_be_string(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: 42
                to: /b
                type: exact
        """)
        with pytest.raises(ParseError, match="'from' must be a string"):
            parse_file(f)

    def test_external_from_rejected(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: https://elsewhere.example.com/x
                to:   /new.html
                type: exact
        """)
        with pytest.raises(ParseError, match="'from' must be a project path"):
            parse_file(f)

    def test_external_to_accepted_in_canonical(self, tmp_path: Path):
        """``to:`` with a scheme is a valid cross-host redirect target."""
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /en/latest/old.html
                to:   https://docs.anyscale.com/new
                type: exact
        """)
        rs = parse_file(f)
        r = next(iter(rs))
        assert r.from_url == "/en/latest/old.html"
        assert r.to_url == "https://docs.anyscale.com/new"

    def test_invalid_type_value(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: bogus
        """)
        with pytest.raises(ParseError, match="invalid type 'bogus'"):
            parse_file(f)

    def test_invalid_status_surfaces_model_error(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: exact
                status: 200
        """)
        with pytest.raises(ParseError, match="http_status must be a 3xx"):
            parse_file(f)

    def test_error_message_includes_file_and_index(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: exact
              - from: /c
                to: /d
                type: bogus
        """)
        with pytest.raises(ParseError, match=r"r\.yaml.*redirects\[1\]"):
            parse_file(f)


class TestVersionAgnosticTypes:
    """page and URL-style types skip version expansion at the parse layer."""

    def test_page_with_defaults_versions_stays_path_only(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults:
              versions: [latest, master]
            redirects:
              - from: /old.html
                to:   /new.html
                type: page
        """)
        rs = parse_file(f)
        assert len(rs) == 1
        r = next(iter(rs))
        assert r.from_url == "/old.html"
        assert r.to_url == "/new.html"

    def test_mixed_page_and_exact_with_defaults(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults:
              versions: [latest, master]
            redirects:
              - from: /old.html
                to:   /new.html
                type: page
              - from: /api.html
                to:   /api-v2.html
                type: exact
        """)
        rs = parse_file(f)
        # page stays path-only; exact fans out to 2 records
        assert len(rs) == 3
        page_records = [r for r in rs if r.type == "page"]
        exact_records = [r for r in rs if r.type == "exact"]
        assert len(page_records) == 1
        assert page_records[0].from_url == "/old.html"
        assert len(exact_records) == 2
        assert {r.from_url for r in exact_records} == {
            "/en/latest/api.html", "/en/master/api.html",
        }

    def test_clean_url_to_html_no_from_to_required(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - type: clean_url_to_html
        """)
        rs = parse_file(f)
        r = next(iter(rs))
        assert r.type == "clean_url_to_html"
        assert r.from_url == ""
        assert r.to_url == ""

    def test_html_to_clean_url_no_from_to_required(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - type: html_to_clean_url
        """)
        rs = parse_file(f)
        r = next(iter(rs))
        assert r.type == "html_to_clean_url"

    def test_wildcard_round_trip_through_canonical_page(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /api/*
                to:   /api/v1/:splat
                type: page
        """)
        r = next(iter(parse_file(f)))
        assert r.from_url == "/api/*"
        assert r.to_url == "/api/v1/:splat"


class TestExpansionIntegration:
    """Smoke tests that parse routes expansion-shaped entries through expand.py.

    Detailed expansion semantics live in tests/test_expand.py.
    """

    def test_top_level_defaults_drives_multi_version(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults:
              versions: [latest, master]
            redirects:
              - from: /a.html
                to:   /b.html
                type: exact
        """)
        rs = parse_file(f)
        assert {r.from_url for r in rs} == {"/en/latest/a.html", "/en/master/a.html"}

    def test_per_entry_versions_drives_multi_version(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a.html
                to:   /b.html
                type: exact
                versions: [latest, master]
        """)
        rs = parse_file(f)
        assert {r.from_url for r in rs} == {"/en/latest/a.html", "/en/master/a.html"}

    def test_list_from_drives_multi_source(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from:
                  - /en/latest/old1.html
                  - /en/latest/old2.html
                to: /en/latest/new.html
                type: exact
        """)
        rs = parse_file(f)
        assert {r.from_url for r in rs} == {
            "/en/latest/old1.html",
            "/en/latest/old2.html",
        }
        assert {r.to_url for r in rs} == {"/en/latest/new.html"}

    def test_fully_qualified_entries_stay_canonical_with_defaults(self, tmp_path: Path):
        """If from is fully-qualified, defaults.versions does not fan out."""
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults:
              versions: [latest, master]
            redirects:
              - from: /en/latest/a.html
                to:   /en/latest/b.html
                type: exact
        """)
        rs = parse_file(f)
        assert len(rs) == 1
        assert next(iter(rs)).from_url == "/en/latest/a.html"

    def test_defaults_must_be_mapping(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults: oops
            redirects: []
        """)
        with pytest.raises(ParseError, match="'defaults' must be a mapping"):
            parse_file(f)

    def test_defaults_versions_must_be_list(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            defaults:
              versions: latest
            redirects: []
        """)
        with pytest.raises(ParseError, match="'defaults.versions' must be a list"):
            parse_file(f)

    def test_custom_language_prefix(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            language_prefix: /de
            defaults:
              versions: [latest]
            redirects:
              - from: /a.html
                to:   /b.html
                type: exact
        """)
        rs = parse_file(f)
        r = next(iter(rs))
        assert r.from_url == "/de/latest/a.html"
        assert r.to_url == "/de/latest/b.html"

    def test_language_prefix_must_be_string(self, tmp_path: Path):
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            language_prefix: 42
            redirects: []
        """)
        with pytest.raises(ParseError, match="'language_prefix' must be a string"):
            parse_file(f)


class TestMultiFile:
    def test_files_compose_in_argument_order(self, tmp_path: Path):
        """File order is meaningful: earlier files position before later ones.

        The argument order — not the alphabetical path order — decides the
        composed sequence. Here ``b.yaml`` is passed first, so its rule lands
        at position 0 ahead of ``a.yaml``'s.
        """
        a = _write(tmp_path, "a.yaml", """
            schema_version: 1
            redirects:
              - from: /a1
                to: /b1
                type: exact
        """)
        b = _write(tmp_path, "b.yaml", """
            schema_version: 1
            redirects:
              - from: /a2
                to: /b2
                type: exact
        """)
        rs = parse_files([b, a])
        assert [r.from_url for r in rs] == ["/a2", "/a1"]
        assert [r.position for r in rs] == [0, 1]

    def test_composition_reindexes_local_positions(self, tmp_path: Path):
        """Each file's local positions start at zero; composition reindexes
        globally so the second file's records can't tie the first file's.

        Without a global reindex both files would contribute a position-0 and a
        position-1 record and the composed order would be an artifact of how
        Python's stable sort broke the ties. The reindex makes file order
        decide it.
        """
        first = _write(tmp_path, "first.yaml", """
            schema_version: 1
            redirects:
              - from: /first/a
                to:   /x
                type: exact
              - from: /first/b
                to:   /y
                type: exact
        """)
        second = _write(tmp_path, "second.yaml", """
            schema_version: 1
            redirects:
              - from: /second/a
                to:   /p
                type: exact
              - from: /second/b
                to:   /q
                type: exact
        """)
        rs = parse_files([first, second])
        assert [r.from_url for r in rs] == [
            "/first/a", "/first/b", "/second/a", "/second/b",
        ]
        assert [r.position for r in rs] == [0, 1, 2, 3]

    def test_master_specific_exact_composes_before_current_wildcard(
        self, tmp_path: Path,
    ):
        """Ray's load-bearing case: a ``master``-scoped exact rule must position
        before the broad ``current.yaml`` wildcard/page rules so it matches
        first under RtD strict first-match.
        """
        master = _write(tmp_path, "master.yaml", """
            schema_version: 1
            redirects:
              - from: /en/master/api/special_case.html
                to:   /en/master/api/its_new_home.html
                type: exact
        """)
        current = _write(tmp_path, "current.yaml", """
            schema_version: 1
            redirects:
              - from: /api/old/*
                to:   /api/new/:splat
                type: page
              - from: /en/master/*
                to:   /en/latest/:splat
                type: exact
        """)
        rs = parse_files([master, current])
        ordered = [(r.from_url, r.position) for r in rs]
        assert ordered == [
            ("/en/master/api/special_case.html", 0),
            ("/api/old/*", 1),
            ("/en/master/*", 2),
        ]
        # The specific master rule sits at the lowest position, ahead of the
        # broad page and wildcard rules it would otherwise be shadowed by.
        positions = {r.from_url: r.position for r in rs}
        assert positions["/en/master/api/special_case.html"] < positions["/en/master/*"]

    def test_single_file_keeps_authored_positions(self, tmp_path: Path):
        """A lone file is not reindexed — its authored positions are the source
        of truth, matching ``parse_file``.
        """
        f = _write(tmp_path, "r.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to:   /b
                type: exact
                position: 5
              - from: /c
                to:   /d
                type: exact
                position: 9
        """)
        rs = parse_files([f])
        assert [r.position for r in rs] == [5, 9]

    def test_duplicate_identity_across_files_raises(self, tmp_path: Path):
        a = _write(tmp_path, "a.yaml", """
            schema_version: 1
            redirects:
              - from: /dup
                to: /one
                type: exact
        """)
        b = _write(tmp_path, "b.yaml", """
            schema_version: 1
            redirects:
              - from: /dup
                to: /two
                type: exact
        """)
        with pytest.raises(ParseError, match="duplicate redirect identity"):
            parse_files([a, b])

    def test_cross_file_duplicate_message_names_both_files(self, tmp_path: Path):
        """A cross-file collision points at both files and the offending identity."""
        a = _write(tmp_path, "a.yaml", """
            schema_version: 1
            redirects:
              - from: /dup
                to: /one
                type: exact
        """)
        b = _write(tmp_path, "b.yaml", """
            schema_version: 1
            redirects:
              - from: /dup
                to: /two
                type: exact
        """)
        with pytest.raises(ParseError) as exc:
            parse_files([a, b])
        msg = str(exc.value)
        assert "('/dup', 'exact')" in msg
        assert str(a) in msg and str(b) in msg
        assert "merge or remove" in msg

    def test_duplicate_identity_message_names_resolution(self, tmp_path: Path):
        """AC-1: authored-YAML duplicates fail with a clear remediation message."""
        f = _write(tmp_path, "dup.yaml", """
            schema_version: 1
            redirects:
              - from: /dup
                to: /one
                type: exact
              - from: /dup
                to: /two
                type: exact
        """)
        with pytest.raises(ParseError) as exc:
            parse_file(f)
        msg = str(exc.value)
        assert "('/dup', 'exact')" in msg   # names the offending identity
        assert "merge or remove" in msg     # the resolution path
        assert "Live RtD data" in msg       # notes live duplicates are tolerated

    def test_same_from_different_type_across_files_ok(self, tmp_path: Path):
        a = _write(tmp_path, "a.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: exact
        """)
        b = _write(tmp_path, "b.yaml", """
            schema_version: 1
            redirects:
              - from: /a
                to: /b
                type: page
        """)
        rs = parse_files([a, b])
        assert len(rs) == 2


class TestCompose:
    """Direct unit tests for the compose primitive (parse.compose)."""

    def _set(self, *specs: tuple[str, int]) -> RedirectSet:
        return RedirectSet(
            Redirect(from_url=f, to_url="/dest", type="exact", position=p)
            for f, p in specs
        )

    def test_reindexes_dense_in_file_then_record_order(self):
        """Composed positions are dense 0..N-1 in file-then-record order,
        regardless of each file's local position values.
        """
        first = self._set(("/a", 0), ("/b", 1))
        second = self._set(("/c", 0), ("/d", 1))
        composed = compose([("first", first), ("second", second)])
        assert [(r.from_url, r.position) for r in composed] == [
            ("/a", 0), ("/b", 1), ("/c", 2), ("/d", 3),
        ]

    def test_within_file_order_follows_position(self):
        """A file's own position values decide its internal order before the
        global reindex flattens them.
        """
        only = self._set(("/late", 9), ("/early", 2))
        composed = compose([("only", only)])
        assert [(r.from_url, r.position) for r in composed] == [
            ("/early", 0), ("/late", 1),
        ]

    def test_idempotent_on_already_composed_set(self):
        """Re-composing a composed set is a no-op: positions are already dense
        and in order, so the output is byte-stable.
        """
        composed = compose([
            ("first", self._set(("/a", 0))),
            ("second", self._set(("/b", 0))),
        ])
        again = compose([("recomposed", composed)])
        assert [(r.from_url, r.position) for r in composed] == [("/a", 0), ("/b", 1)]
        assert [(r.from_url, r.position) for r in again] == [("/a", 0), ("/b", 1)]

    def test_cross_set_duplicate_raises_with_labels(self):
        dup = ("/x", 0)
        with pytest.raises(ParseError) as exc:
            compose([("left", self._set(dup)), ("right", self._set(dup))])
        msg = str(exc.value)
        assert "('/x', 'exact')" in msg
        assert "left" in msg and "right" in msg

    def test_empty_input_yields_empty_set(self):
        assert len(compose([])) == 0
