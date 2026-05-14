"""Tests for rtd_redirects.collapse: dump-time grouping of canonical Redirects."""

from __future__ import annotations

from pathlib import Path

import yaml

from rtd_redirects.collapse import collapse
from rtd_redirects.model import Redirect, RedirectSet
from rtd_redirects.parse import SCHEMA_VERSION, parse_file


def _r(
    from_url: str,
    to_url: str = "/dest",
    *,
    type: str = "exact",
    status: int = 301,
    force: bool = False,
    enabled: bool = True,
    description: str = "",
    position: int = 0,
) -> Redirect:
    return Redirect(
        from_url=from_url,
        to_url=to_url,
        type=type,
        http_status=status,
        force=force,
        enabled=enabled,
        description=description,
        position=position,
    )


class TestSingletons:
    def test_empty(self):
        assert collapse([]) == []

    def test_single_record(self):
        entries = collapse([_r("/a", "/b")])
        assert entries == [{"from": "/a", "to": "/b", "type": "exact"}]

    def test_non_default_fields_emitted(self):
        entries = collapse([_r(
            "/a", "/b",
            status=302, force=True, enabled=False,
            description="op note",
        )])
        assert entries[0] == {
            "from": "/a", "to": "/b", "type": "exact",
            "status": 302, "force": True, "enabled": False,
            "description": "op note",
        }

    def test_default_fields_omitted(self):
        entries = collapse([_r("/a", "/b")])
        e = entries[0]
        assert "status" not in e
        assert "force" not in e
        assert "enabled" not in e
        assert "description" not in e


class TestMultiSourceCollapse:
    def test_two_records_same_target_collapse(self):
        records = [_r("/old1", "/new"), _r("/old2", "/new")]
        entries = collapse(records)
        assert entries == [{
            "from": ["/old1", "/old2"],
            "to": "/new",
            "type": "exact",
        }]

    def test_three_records_same_target_collapse(self):
        records = [_r("/a", "/x"), _r("/b", "/x"), _r("/c", "/x")]
        entries = collapse(records)
        assert len(entries) == 1
        assert entries[0]["from"] == ["/a", "/b", "/c"]

    def test_different_targets_stay_separate(self):
        records = [_r("/a", "/x"), _r("/b", "/y")]
        entries = collapse(records)
        assert len(entries) == 2

    def test_same_target_different_type_stay_separate(self):
        records = [_r("/a", "/x", type="exact"), _r("/b", "/x", type="page")]
        entries = collapse(records)
        assert len(entries) == 2

    def test_same_target_different_status_stay_separate(self):
        records = [_r("/a", "/x", status=301), _r("/b", "/x", status=302)]
        entries = collapse(records)
        assert len(entries) == 2

    def test_same_target_different_force_stay_separate(self):
        records = [_r("/a", "/x", force=False), _r("/b", "/x", force=True)]
        entries = collapse(records)
        assert len(entries) == 2

    def test_same_target_different_description_stay_separate(self):
        records = [
            _r("/a", "/x", description="one"),
            _r("/b", "/x", description="two"),
        ]
        entries = collapse(records)
        assert len(entries) == 2

    def test_from_list_is_sorted(self):
        records = [_r("/c", "/x"), _r("/a", "/x"), _r("/b", "/x")]
        entries = collapse(records)
        assert entries[0]["from"] == ["/a", "/b", "/c"]


class TestOrdering:
    def test_entries_sorted_by_position(self):
        records = [
            _r("/c", "/z", position=2),
            _r("/a", "/x", position=0),
            _r("/b", "/y", position=1),
        ]
        entries = collapse(records)
        assert [e["to"] for e in entries] == ["/x", "/y", "/z"]

    def test_implicit_position_omitted_when_matching_index(self):
        records = [
            _r("/a", "/x", position=0),
            _r("/b", "/y", position=1),
        ]
        entries = collapse(records)
        for e in entries:
            assert "position" not in e

    def test_explicit_position_emitted_when_diverging(self):
        records = [
            _r("/a", "/x", position=5),
            _r("/b", "/y", position=7),
        ]
        entries = collapse(records)
        assert entries[0].get("position") == 5
        assert entries[1].get("position") == 7

    def test_collapsed_group_keeps_shared_position(self):
        records = [
            _r("/a", "/x", position=3),
            _r("/b", "/x", position=3),
        ]
        entries = collapse(records)
        assert len(entries) == 1
        assert entries[0].get("position") == 3


class TestRoundTrip:
    """collapse -> YAML -> parse must reproduce the original canonical records."""

    def _round_trip(self, tmp_path: Path, records: list[Redirect]) -> RedirectSet:
        doc = {"schema_version": SCHEMA_VERSION, "redirects": collapse(records)}
        path = tmp_path / "round.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        return parse_file(path)

    def test_singletons_round_trip(self, tmp_path: Path):
        original = [
            _r("/en/latest/a.html", "/en/latest/b.html", position=0),
            _r("/en/latest/c.html", "/en/latest/d.html", position=1),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_multi_source_collapse_round_trips(self, tmp_path: Path):
        original = [
            _r("/en/latest/old1.html", "/en/latest/new.html", position=0),
            _r("/en/latest/old2.html", "/en/latest/new.html", position=0),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_non_default_fields_round_trip(self, tmp_path: Path):
        original = [_r(
            "/en/latest/a.html",
            "/en/latest/b.html",
            status=302,
            force=True,
            enabled=False,
            description="legacy redirect",
            position=0,
        )]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_explicit_position_round_trips(self, tmp_path: Path):
        original = [
            _r("/en/latest/a.html", "/en/latest/x.html", position=5),
            _r("/en/latest/b.html", "/en/latest/y.html", position=7),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_external_to_round_trips(self, tmp_path: Path):
        original = [
            _r("/en/latest/old.html", "https://docs.anyscale.com/new"),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_wildcard_round_trip(self, tmp_path: Path):
        """`*` in from and `:splat` in to pass through dump -> parse cleanly."""
        original = [
            _r(
                "/en/latest/rllib/rllib/*",
                "/en/latest/rllib/:splat",
                position=0,
            ),
            _r(
                "/en/latest/api/old/*",
                "/en/latest/api/v2/:splat",
                position=1,
            ),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_page_redirect_round_trip(self, tmp_path: Path):
        """page-type records dump/parse cleanly without version fan-out."""
        original = [_r("/old.html", "/new.html", type="page", position=0)]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_url_style_redirect_round_trip(self, tmp_path: Path):
        """clean_url_to_html with empty from/to round-trips."""
        original = [Redirect(
            from_url="", to_url="",
            type="clean_url_to_html",
            position=0,
        )]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)

    def test_mixed_groups_round_trip(self, tmp_path: Path):
        original = [
            _r("/en/latest/a.html", "/en/latest/x.html", position=0),
            _r("/en/latest/b.html", "/en/latest/x.html", position=0),
            _r("/en/latest/c.html", "/en/latest/y.html", position=1),
            _r("/old.html", "/new.html", type="page", position=2),
        ]
        result = self._round_trip(tmp_path, original)
        assert result == RedirectSet(original)
