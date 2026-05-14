"""Tests for rtd_redirects.parse: YAML reading and schema validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from rtd_redirects.parse import ParseError, parse_file, parse_files


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
    def test_files_concatenate_sorted(self, tmp_path: Path):
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
        rs = parse_files([b, a])  # caller order shouldn't matter
        assert {r.from_url for r in rs} == {"/a1", "/a2"}

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
        with pytest.raises(ParseError, match="Duplicate identity"):
            parse_files([a, b])

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
