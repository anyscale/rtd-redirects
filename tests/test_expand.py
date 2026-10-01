"""Tests for rtd_redirects.expand: multi-source fan-out."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtd_redirects.exceptions import ParseError
from rtd_redirects.expand import expand_entry, is_external

FILE = Path("test.yaml")
INDEX = 0


def _expand(entry: dict):
    return expand_entry(FILE, INDEX, entry)


class TestMultiSource:
    def test_two_sources_one_target(self):
        records = _expand({"from": ["/a.html", "/b.html"], "to": "/c.html", "type": "page"})
        assert [(r.from_url, r.to_url, r.type) for r in records] == [
            ("/a.html", "/c.html", "page"),
            ("/b.html", "/c.html", "page"),
        ]

    def test_fully_qualified_exact_sources_pass_through(self):
        records = _expand({
            "from": ["/en/latest/a.html", "/en/master/a.html"],
            "to": "/en/latest/b.html",
            "type": "exact",
        })
        assert {r.from_url for r in records} == {"/en/latest/a.html", "/en/master/a.html"}
        assert {r.to_url for r in records} == {"/en/latest/b.html"}

    def test_paths_are_not_qualified(self):
        # Multi-version qualification was removed: a path stays a path.
        records = _expand({"from": ["/a.html"], "to": "/b.html", "type": "exact"})
        assert (records[0].from_url, records[0].to_url) == ("/a.html", "/b.html")

    def test_single_string_from(self):
        records = _expand({"from": "/a.html", "to": "/b.html", "type": "page"})
        assert len(records) == 1

    def test_wildcards_pass_through(self):
        records = _expand({"from": ["/api/*", "/old/*"], "to": "/api/v1/:splat", "type": "page"})
        assert {r.from_url for r in records} == {"/api/*", "/old/*"}
        assert {r.to_url for r in records} == {"/api/v1/:splat"}

    @pytest.mark.parametrize("type_name", ["clean_url_to_html", "html_to_clean_url"])
    def test_url_style_types_no_fields_required(self, type_name: str):
        """URL-style types describe project-wide transitions; from/to optional."""
        records = _expand({"type": type_name})
        assert len(records) == 1
        assert records[0].type == type_name
        assert records[0].from_url == ""
        assert records[0].to_url == ""


class TestFieldDefaults:
    def test_status_force_enabled_position_description(self):
        records = _expand({
            "from": ["/a.html"], "to": "/b.html", "type": "exact",
            "status": 302, "force": True, "enabled": False,
            "position": 7, "description": "op note",
        })
        r = records[0]
        assert r.http_status == 302
        assert r.force is True
        assert r.enabled is False
        assert r.position == 7
        assert r.description == "op note"

    def test_position_inherits_entry_index_when_unset(self):
        records = expand_entry(FILE, 5, {"from": ["/a.html"], "to": "/b.html", "type": "exact"})
        assert records[0].position == 5

    def test_null_description_becomes_empty(self):
        records = _expand({
            "from": ["/a.html"], "to": "/b.html", "type": "exact", "description": None,
        })
        assert records[0].description == ""


class TestRequiredFields:
    def test_missing_from_raises(self):
        with pytest.raises(ParseError, match="missing required field 'from'"):
            _expand({"to": "/b.html", "type": "exact"})

    def test_missing_to_raises(self):
        with pytest.raises(ParseError, match="missing required field 'to'"):
            _expand({"from": ["/a.html"], "type": "exact"})

    def test_missing_type_raises(self):
        with pytest.raises(ParseError, match="missing required field 'type'"):
            _expand({"from": ["/a.html"], "to": "/b.html"})

    def test_type_wrong_type(self):
        with pytest.raises(ParseError, match="'type' must be a string"):
            _expand({"from": ["/a.html"], "to": "/b.html", "type": 3})

    def test_invalid_type(self):
        with pytest.raises(ParseError, match="invalid type 'bogus'"):
            _expand({"from": ["/a.html"], "to": "/b.html", "type": "bogus"})

    def test_from_list_with_non_string_item(self):
        with pytest.raises(ParseError, match="'from' list items must be strings"):
            _expand({"from": ["/a.html", 42], "to": "/b.html", "type": "exact"})

    def test_empty_from_list(self):
        with pytest.raises(ParseError, match="'from' list cannot be empty"):
            _expand({"from": [], "to": "/b.html", "type": "exact"})

    def test_from_wrong_type(self):
        with pytest.raises(ParseError, match="'from' must be a string or list"):
            _expand({"from": 42, "to": "/b.html", "type": "exact"})

    def test_to_wrong_type(self):
        with pytest.raises(ParseError, match="'to' must be a string"):
            _expand({"from": ["/a.html"], "to": ["/b.html"], "type": "exact"})


class TestExternalUrls:
    @pytest.mark.parametrize(
        "external_to",
        [
            "https://docs.anyscale.com/platform",
            "//cdn.example.com/assets/page",
            "mailto:docs@anyscale.com",
        ],
    )
    def test_external_to_passes_through(self, external_to: str):
        records = _expand({"from": ["/a.html", "/b.html"], "to": external_to, "type": "page"})
        assert {r.to_url for r in records} == {external_to}

    def test_external_in_from_list_rejected(self):
        with pytest.raises(ParseError, match="'from' must be a project path"):
            _expand({"from": ["https://x.com/a", "/b.html"], "to": "/c.html", "type": "exact"})

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/x", True),
            ("//cdn.example.com/x", True),
            ("mailto:a@example.com", True),
            ("/en/latest/x.html", False),
            ("/x.html", False),
        ],
    )
    def test_is_external(self, url: str, expected: bool):
        assert is_external(url) is expected


class TestErrorContext:
    def test_error_includes_file_and_index(self):
        with pytest.raises(ParseError, match=r"test\.yaml.*redirects\[0\]"):
            _expand({"from": ["/a.html"], "to": "/b.html", "type": "bogus"})

    def test_model_error_wrapped_with_context(self):
        with pytest.raises(ParseError, match=r"test\.yaml.*redirects\[0\].*http_status"):
            _expand({"from": ["/a.html"], "to": "/b.html", "type": "exact", "status": 200})
