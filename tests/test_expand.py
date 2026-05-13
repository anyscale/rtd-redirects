"""Tests for rtd_redirects.expand: multi-source and multi-version fan-out."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import expand_entry

FILE = Path("test.yaml")
INDEX = 0


def _expand(entry: dict, defaults_versions: list[str] | None = None):
    return expand_entry(FILE, INDEX, entry, defaults_versions)


class TestSingleSourceSingleVersion:
    def test_path_only_with_defaults(self):
        records = _expand(
            {"from": "/a.html", "to": "/b.html", "type": "exact"},
            defaults_versions=["latest"],
        )
        assert len(records) == 1
        r = records[0]
        assert r.from_url == "/en/latest/a.html"
        assert r.to_url == "/en/latest/b.html"
        assert r.type == "exact"

    def test_path_only_with_explicit_versions(self):
        records = _expand(
            {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
        )
        assert len(records) == 1
        assert records[0].from_url == "/en/latest/a.html"

    def test_fully_qualified_no_versions_passes_through(self):
        records = _expand(
            {"from": "/en/v2.55/a.html", "to": "/en/v2.55/b.html", "type": "exact"},
        )
        assert len(records) == 1
        assert records[0].from_url == "/en/v2.55/a.html"
        assert records[0].to_url == "/en/v2.55/b.html"


class TestMultiVersion:
    def test_defaults_fan_out(self):
        records = _expand(
            {"from": "/a.html", "to": "/b.html", "type": "exact"},
            defaults_versions=["latest", "master"],
        )
        assert {r.from_url for r in records} == {"/en/latest/a.html", "/en/master/a.html"}
        assert {r.to_url for r in records} == {"/en/latest/b.html", "/en/master/b.html"}

    def test_explicit_versions_override_defaults(self):
        records = _expand(
            {
                "from": "/a.html", "to": "/b.html", "type": "exact",
                "versions": ["v2.55"],
            },
            defaults_versions=["latest", "master"],
        )
        assert [r.from_url for r in records] == ["/en/v2.55/a.html"]

    def test_three_versions(self):
        records = _expand(
            {
                "from": "/a.html", "to": "/b.html", "type": "exact",
                "versions": ["latest", "master", "v2.55"],
            },
        )
        assert len(records) == 3
        assert {r.from_url for r in records} == {
            "/en/latest/a.html",
            "/en/master/a.html",
            "/en/v2.55/a.html",
        }


class TestMultiSource:
    def test_two_sources_one_target_one_version(self):
        records = _expand(
            {
                "from": ["/en/latest/old1.html", "/en/latest/old2.html"],
                "to": "/en/latest/new.html",
                "type": "exact",
            },
        )
        assert len(records) == 2
        assert {r.from_url for r in records} == {
            "/en/latest/old1.html",
            "/en/latest/old2.html",
        }
        assert {r.to_url for r in records} == {"/en/latest/new.html"}

    def test_path_only_multi_source_with_defaults(self):
        records = _expand(
            {
                "from": ["/old1.html", "/old2.html"],
                "to": "/new.html",
                "type": "exact",
            },
            defaults_versions=["latest"],
        )
        assert {r.from_url for r in records} == {
            "/en/latest/old1.html",
            "/en/latest/old2.html",
        }

    def test_cross_product_sources_times_versions(self):
        records = _expand(
            {
                "from": ["/a.html", "/b.html"],
                "to": "/c.html",
                "type": "exact",
                "versions": ["latest", "master"],
            },
        )
        assert len(records) == 4
        assert {r.from_url for r in records} == {
            "/en/latest/a.html", "/en/latest/b.html",
            "/en/master/a.html", "/en/master/b.html",
        }

    def test_to_passes_through_when_fully_qualified(self):
        """A fully-qualified ``to`` stays exactly as written, even with multi-version."""
        records = _expand(
            {
                "from": "/a.html",
                "to": "/en/stable/permanent-home.html",
                "type": "exact",
                "versions": ["latest", "master"],
            },
        )
        for r in records:
            assert r.to_url == "/en/stable/permanent-home.html"


class TestFieldDefaults:
    def test_status_force_enabled_position_description(self):
        records = _expand(
            {
                "from": "/a.html", "to": "/b.html", "type": "exact",
                "status": 302, "force": True, "enabled": False,
                "position": 7, "description": "op note",
                "versions": ["latest"],
            },
        )
        r = records[0]
        assert r.http_status == 302
        assert r.force is True
        assert r.enabled is False
        assert r.position == 7
        assert r.description == "op note"

    def test_position_inherits_entry_index_when_unset(self):
        records = expand_entry(
            FILE,
            5,
            {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
            None,
        )
        assert records[0].position == 5

    def test_null_description_becomes_empty(self):
        records = _expand(
            {
                "from": "/a.html", "to": "/b.html", "type": "exact",
                "description": None, "versions": ["latest"],
            },
        )
        assert records[0].description == ""


class TestRequiredFields:
    def test_missing_from_raises(self):
        with pytest.raises(ParseError, match="missing required field 'from'"):
            _expand({"to": "/b.html", "type": "exact", "versions": ["latest"]})

    def test_missing_to_raises(self):
        with pytest.raises(ParseError, match="missing required field 'to'"):
            _expand({"from": "/a.html", "type": "exact", "versions": ["latest"]})

    def test_missing_type_raises(self):
        with pytest.raises(ParseError, match="missing required field 'type'"):
            _expand({"from": "/a.html", "to": "/b.html", "versions": ["latest"]})

    def test_invalid_type(self):
        with pytest.raises(ParseError, match="invalid type 'bogus'"):
            _expand({"from": "/a.html", "to": "/b.html", "type": "bogus", "versions": ["latest"]})

    def test_from_list_with_non_string_item(self):
        with pytest.raises(ParseError, match="'from' list items must be strings"):
            _expand(
                {
                    "from": ["/a.html", 42], "to": "/b.html",
                    "type": "exact", "versions": ["latest"],
                },
            )

    def test_empty_from_list(self):
        with pytest.raises(ParseError, match="'from' list cannot be empty"):
            _expand({"from": [], "to": "/b.html", "type": "exact", "versions": ["latest"]})

    def test_from_wrong_type(self):
        with pytest.raises(ParseError, match="'from' must be a string or list"):
            _expand({"from": 42, "to": "/b.html", "type": "exact", "versions": ["latest"]})

    def test_to_wrong_type(self):
        with pytest.raises(ParseError, match="'to' must be a string"):
            _expand({"from": "/a.html", "to": 42, "type": "exact", "versions": ["latest"]})


class TestVersionResolution:
    def test_explicit_versions_with_fully_qualified_from_raises(self):
        with pytest.raises(ParseError, match="cannot mix fully-qualified 'from'"):
            _expand(
                {
                    "from": "/en/latest/a.html",
                    "to": "/en/latest/b.html",
                    "type": "exact",
                    "versions": ["master"],
                },
            )

    def test_mixed_qualified_and_path_only_raises(self):
        with pytest.raises(ParseError, match="cannot mix fully-qualified and path-only"):
            _expand(
                {
                    "from": ["/en/latest/a.html", "/b.html"],
                    "to": "/c.html",
                    "type": "exact",
                },
                defaults_versions=["latest"],
            )

    def test_path_only_without_versions_or_defaults_raises(self):
        with pytest.raises(ParseError, match="path-only 'from' requires"):
            _expand({"from": "/a.html", "to": "/b.html", "type": "exact"})

    def test_versions_must_be_list(self):
        with pytest.raises(ParseError, match="'versions' must be a list"):
            _expand({"from": "/a.html", "to": "/b.html", "type": "exact", "versions": "latest"})

    def test_versions_cannot_be_empty(self):
        with pytest.raises(ParseError, match="'versions' list cannot be empty"):
            _expand({"from": "/a.html", "to": "/b.html", "type": "exact", "versions": []})

    def test_version_identifier_non_string(self):
        with pytest.raises(ParseError, match="version identifier must be a string"):
            _expand(
                {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": [42]},
            )

    @pytest.mark.parametrize(
        "pattern",
        ["v2.5*", ">=v2.50", "<v3.0", "!v2.54", "@active"],
    )
    def test_pattern_identifiers_rejected(self, pattern: str):
        with pytest.raises(ParseError, match="not yet supported"):
            _expand(
                {
                    "from": "/a.html",
                    "to": "/b.html",
                    "type": "exact",
                    "versions": [pattern],
                },
            )


class TestLanguagePrefix:
    def test_default_prefix_is_en(self):
        records = _expand(
            {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
        )
        assert records[0].from_url == "/en/latest/a.html"

    def test_custom_prefix(self):
        records = expand_entry(
            FILE,
            INDEX,
            {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
            None,
            language_prefix="/de",
        )
        assert records[0].from_url == "/de/latest/a.html"
        assert records[0].to_url == "/de/latest/b.html"

    def test_custom_prefix_detects_qualified_correctly(self):
        """A URL under the custom prefix is detected as fully-qualified."""
        records = expand_entry(
            FILE,
            INDEX,
            {"from": "/de/latest/a.html", "to": "/de/latest/b.html", "type": "exact"},
            None,
            language_prefix="/de",
        )
        assert records[0].from_url == "/de/latest/a.html"

    def test_custom_prefix_rejects_mixing(self):
        """``/en/...`` URLs are NOT fully-qualified when prefix is ``/de``."""
        # /en/latest/a.html doesn't start with /de/, so it's path-only here.
        # Mixed list (one starts with /de/, one with /en/) -> conflict.
        with pytest.raises(ParseError, match="cannot mix"):
            expand_entry(
                FILE,
                INDEX,
                {
                    "from": ["/de/latest/a.html", "/en/latest/b.html"],
                    "to": "/c.html",
                    "type": "exact",
                },
                defaults_versions=["latest"],
                language_prefix="/de",
            )

    def test_empty_prefix_rejected(self):
        with pytest.raises(ValueError, match="languageless"):
            expand_entry(
                FILE, INDEX,
                {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
                None,
                language_prefix="",
            )

    def test_prefix_must_start_with_slash(self):
        with pytest.raises(ValueError, match="must start with"):
            expand_entry(
                FILE, INDEX,
                {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
                None,
                language_prefix="en",
            )

    def test_prefix_must_not_end_with_slash(self):
        with pytest.raises(ValueError, match="must not end with"):
            expand_entry(
                FILE, INDEX,
                {"from": "/a.html", "to": "/b.html", "type": "exact", "versions": ["latest"]},
                None,
                language_prefix="/en/",
            )


class TestErrorContext:
    def test_error_includes_file_and_index(self):
        with pytest.raises(ParseError, match=r"test\.yaml.*redirects\[0\]"):
            _expand({"from": "/a.html", "to": "/b.html", "type": "bogus", "versions": ["latest"]})

    def test_model_error_wrapped_with_context(self):
        with pytest.raises(ParseError, match=r"test\.yaml.*redirects\[0\].*http_status"):
            _expand(
                {
                    "from": "/a.html", "to": "/b.html", "type": "exact",
                    "status": 200, "versions": ["latest"],
                },
            )
