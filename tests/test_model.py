"""Tests for rtd_redirects.model: Redirect dataclass and RedirectSet."""

from __future__ import annotations

import pytest

from rtd_redirects.model import REDIRECT_TYPES, Redirect, RedirectSet


class TestRedirect:
    def test_defaults(self):
        r = Redirect(from_url="/a", to_url="/b", type="exact")
        assert r.from_url == "/a"
        assert r.to_url == "/b"
        assert r.type == "exact"
        assert r.http_status == 301
        assert r.force is False
        assert r.enabled is True
        assert r.position == 0
        assert r.description == ""
        assert r.pk is None

    def test_identity(self):
        r = Redirect(from_url="/a", to_url="/b", type="exact")
        assert r.identity == ("/a", "exact")

    @pytest.mark.parametrize("type_name", sorted(REDIRECT_TYPES))
    def test_valid_types_accepted(self, type_name: str):
        r = Redirect(from_url="/a", to_url="/b", type=type_name)
        assert r.type == type_name

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid redirect type"):
            Redirect(from_url="/a", to_url="/b", type="bogus")

    @pytest.mark.parametrize("status", [199, 299, 400, 500])
    def test_non_3xx_status_raises(self, status: int):
        with pytest.raises(ValueError, match="http_status must be a 3xx"):
            Redirect(from_url="/a", to_url="/b", type="exact", http_status=status)

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_3xx_status_accepted(self, status: int):
        r = Redirect(from_url="/a", to_url="/b", type="exact", http_status=status)
        assert r.http_status == status

    def test_negative_position_raises(self):
        with pytest.raises(ValueError, match="position must be non-negative"):
            Redirect(from_url="/a", to_url="/b", type="exact", position=-1)

    def test_equality_includes_data_fields(self):
        a = Redirect(from_url="/a", to_url="/b", type="exact")
        b = Redirect(from_url="/a", to_url="/b", type="exact")
        assert a == b

        assert a != Redirect(from_url="/a", to_url="/c", type="exact")
        assert a != Redirect(from_url="/a", to_url="/b", type="exact", force=True)
        assert a != Redirect(from_url="/a", to_url="/b", type="exact", position=5)
        assert a != Redirect(from_url="/a", to_url="/b", type="exact", description="x")

    def test_equality_excludes_pk(self):
        """Records sharing data but holding different RtD pks compare equal."""
        a = Redirect(from_url="/a", to_url="/b", type="exact", pk=1)
        b = Redirect(from_url="/a", to_url="/b", type="exact", pk=999)
        assert a == b

    def test_repr_omits_pk(self):
        r = Redirect(from_url="/a", to_url="/b", type="exact", pk=42)
        assert "pk" not in repr(r)


class TestRedirectSet:
    @staticmethod
    def _r(from_url: str, *, position: int = 0, type: str = "exact") -> Redirect:
        return Redirect(from_url=from_url, to_url="/dest", type=type, position=position)

    def test_empty(self):
        rs = RedirectSet()
        assert len(rs) == 0
        assert list(rs) == []
        assert rs.identities() == set()

    def test_construct_from_iterable(self):
        rs = RedirectSet([self._r("/a"), self._r("/b")])
        assert len(rs) == 2
        assert {r.from_url for r in rs} == {"/a", "/b"}

    def test_duplicate_identity_on_add_raises(self):
        rs = RedirectSet([self._r("/a")])
        with pytest.raises(ValueError, match="Duplicate identity"):
            rs.add(self._r("/a"))

    def test_duplicate_identity_in_constructor_raises(self):
        with pytest.raises(ValueError, match="Duplicate identity"):
            RedirectSet([self._r("/a"), self._r("/a")])

    def test_same_from_url_different_type_is_two_records(self):
        rs = RedirectSet()
        rs.add(self._r("/a", type="exact"))
        rs.add(self._r("/a", type="page"))
        assert len(rs) == 2

    def test_replace_overwrites(self):
        rs = RedirectSet([self._r("/a", position=0)])
        rs.replace(self._r("/a", position=5))
        assert len(rs) == 1
        assert next(iter(rs)).position == 5

    def test_replace_inserts_when_missing(self):
        rs = RedirectSet()
        rs.replace(self._r("/a"))
        assert len(rs) == 1

    def test_remove_returns_the_record(self):
        rs = RedirectSet([self._r("/a"), self._r("/b")])
        removed = rs.remove(("/a", "exact"))
        assert removed.from_url == "/a"
        assert len(rs) == 1
        assert ("/a", "exact") not in rs

    def test_remove_missing_raises(self):
        rs = RedirectSet()
        with pytest.raises(KeyError):
            rs.remove(("/missing", "exact"))

    def test_get_returns_record_or_none(self):
        rs = RedirectSet([self._r("/a")])
        assert rs.get(("/a", "exact")) is not None
        assert rs.get(("/missing", "exact")) is None

    def test_contains(self):
        rs = RedirectSet([self._r("/a")])
        assert ("/a", "exact") in rs
        assert ("/b", "exact") not in rs

    def test_identities_returns_set_copy(self):
        rs = RedirectSet([self._r("/a"), self._r("/b")])
        ids = rs.identities()
        assert ids == {("/a", "exact"), ("/b", "exact")}
        # Mutating the returned set must not affect the collection.
        ids.add(("/c", "exact"))
        assert ("/c", "exact") not in rs

    def test_iteration_is_position_sorted(self):
        rs = RedirectSet([
            self._r("/c", position=2),
            self._r("/a", position=0),
            self._r("/b", position=1),
        ])
        assert [r.from_url for r in rs] == ["/a", "/b", "/c"]

    def test_equality_ignores_insertion_order(self):
        a = RedirectSet([self._r("/a"), self._r("/b")])
        b = RedirectSet([self._r("/b"), self._r("/a")])
        assert a == b

    def test_inequality_on_data_change(self):
        a = RedirectSet([self._r("/a", position=0)])
        b = RedirectSet([self._r("/a", position=5)])
        assert a != b

    def test_equality_against_non_redirect_set_is_not_implemented(self):
        rs = RedirectSet()
        assert (rs == 42) is False
        assert (rs == "not a set") is False
