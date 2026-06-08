"""Tests for rtd_redirects.diff: source-vs-target diff over RedirectSets."""

from __future__ import annotations

import pytest

from rtd_redirects.diff import diff
from rtd_redirects.model import Redirect, RedirectSet


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
    pk: int | None = None,
) -> Redirect:
    return Redirect(
        from_url=from_url, to_url=to_url, type=type,
        http_status=status, force=force, enabled=enabled,
        description=description, position=position, pk=pk,
    )


class TestEmptyAndNoOp:
    def test_empty_sets_diff_is_empty(self):
        d = diff(RedirectSet(), RedirectSet())
        assert d.is_empty
        assert len(d) == 0
        assert d.adds == []
        assert d.updates == []
        assert d.deletes == []
        assert d.reorders == []

    def test_identical_sets_diff_is_empty(self):
        records = [_r("/a", "/b"), _r("/c", "/d", position=1)]
        d = diff(RedirectSet(records), RedirectSet(records))
        assert d.is_empty

    def test_pk_difference_alone_is_no_op(self):
        """Source carries no pk; target has API-side pk. Equality ignores it."""
        s = RedirectSet([_r("/a", "/b")])
        t = RedirectSet([_r("/a", "/b", pk=42)])
        assert diff(s, t).is_empty


class TestAdds:
    def test_single_add(self):
        s = RedirectSet([_r("/a", "/b")])
        d = diff(s, RedirectSet())
        assert len(d.adds) == 1
        assert d.adds[0].from_url == "/a"
        assert not d.updates and not d.deletes and not d.reorders

    def test_multiple_adds_sorted_by_identity(self):
        s = RedirectSet([_r("/c"), _r("/a"), _r("/b")])
        d = diff(s, RedirectSet())
        assert [r.from_url for r in d.adds] == ["/a", "/b", "/c"]

    def test_same_from_different_type_both_added(self):
        s = RedirectSet([_r("/a", type="exact"), _r("/a", type="page")])
        d = diff(s, RedirectSet())
        assert len(d.adds) == 2


class TestDeletes:
    def test_single_delete(self):
        t = RedirectSet([_r("/a", "/b", pk=1)])
        d = diff(RedirectSet(), t)
        assert len(d.deletes) == 1
        assert d.deletes[0].pk == 1  # so apply can DELETE by pk

    def test_multiple_deletes_sorted_by_identity(self):
        t = RedirectSet([_r("/c", pk=3), _r("/a", pk=1), _r("/b", pk=2)])
        d = diff(RedirectSet(), t)
        assert [r.from_url for r in d.deletes] == ["/a", "/b", "/c"]


class TestUpdates:
    @pytest.mark.parametrize(
        "field_kwargs",
        [
            {"to_url": "/changed"},
            {"status": 302},
            {"force": True},
            {"enabled": False},
            {"description": "now annotated"},
        ],
    )
    def test_single_non_position_field_change_is_update(self, field_kwargs: dict):
        t = RedirectSet([_r("/a", pk=1)])
        s = RedirectSet([_r("/a", **field_kwargs)])
        d = diff(s, t)
        assert len(d.updates) == 1
        assert d.updates[0].target.pk == 1
        assert not d.reorders

    def test_update_carries_target_pk_and_source_fields(self):
        t = RedirectSet([_r("/a", "/old", pk=42)])
        s = RedirectSet([_r("/a", "/new")])
        d = diff(s, t)
        u = d.updates[0]
        assert u.target.pk == 42
        assert u.target.to_url == "/old"
        assert u.source.to_url == "/new"

    def test_position_plus_other_field_is_update_not_reorder(self):
        t = RedirectSet([_r("/a", "/old", position=0, pk=1)])
        s = RedirectSet([_r("/a", "/new", position=5)])
        d = diff(s, t)
        assert len(d.updates) == 1
        assert not d.reorders


class TestReorders:
    def test_position_only_change_is_reorder(self):
        t = RedirectSet([_r("/a", position=0, pk=1)])
        s = RedirectSet([_r("/a", position=5)])
        d = diff(s, t)
        assert not d.updates
        assert len(d.reorders) == 1
        assert d.reorders[0].target.pk == 1
        assert d.reorders[0].source.position == 5

    def test_multiple_reorders_sorted_by_target_position(self):
        # Identity order would be /a, /b, /c; the target positions invert that.
        # Reorders must come out by ascending target position so insert-and-shift
        # position writes settle the lowest slot first.
        t = RedirectSet([
            _r("/a", position=0, pk=1),
            _r("/b", position=1, pk=2),
            _r("/c", position=2, pk=3),
        ])
        s = RedirectSet([
            _r("/a", position=1),
            _r("/b", position=2),
            _r("/c", position=0),
        ])
        d = diff(s, t)
        assert [u.source.from_url for u in d.reorders] == ["/c", "/a", "/b"]


class TestMixed:
    def test_one_of_each(self):
        t = RedirectSet([
            _r("/keep", "/old", pk=1),   # will update
            _r("/remove", "/x", pk=2),    # will delete
            _r("/shift", position=0, pk=3),  # will reorder
        ])
        s = RedirectSet([
            _r("/keep", "/new"),          # update
            _r("/add", "/y"),             # add
            _r("/shift", position=5),     # reorder
        ])
        d = diff(s, t)
        assert [r.from_url for r in d.adds] == ["/add"]
        assert [r.from_url for r in d.deletes] == ["/remove"]
        assert [u.source.from_url for u in d.updates] == ["/keep"]
        assert [u.source.from_url for u in d.reorders] == ["/shift"]
        assert len(d) == 4

    def test_is_empty_property(self):
        records = [_r("/a", "/b")]
        assert diff(RedirectSet(records), RedirectSet(records)).is_empty
        assert not diff(RedirectSet([_r("/x")]), RedirectSet()).is_empty


class TestDeterminism:
    def test_same_inputs_produce_same_output(self):
        s = RedirectSet([_r("/b"), _r("/a"), _r("/c")])
        t = RedirectSet([_r("/d", pk=1)])
        d1 = diff(s, t)
        d2 = diff(s, t)
        assert d1.adds == d2.adds
        assert d1.deletes == d2.deletes
        assert d1.updates == d2.updates
        assert d1.reorders == d2.reorders
