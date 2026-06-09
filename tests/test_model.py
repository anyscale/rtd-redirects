"""Tests for rtd_redirects.model: Redirect dataclass and RedirectSet."""

from __future__ import annotations

import pytest

from rtd_redirects.model import (
    REDIRECT_TYPES,
    DuplicateGroup,
    Redirect,
    RedirectSet,
)


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


class TestFromApi:
    """RedirectSet.from_api tolerates the duplicate identities RtD permits."""

    @staticmethod
    def _r(
        from_url: str,
        *,
        to_url: str = "/dest",
        type: str = "exact",
        position: int = 0,
        pk: int | None = None,
    ) -> Redirect:
        return Redirect(
            from_url=from_url, to_url=to_url, type=type, position=position, pk=pk,
        )

    def test_clean_data_returns_no_groups(self):
        rs, groups = RedirectSet.from_api([
            self._r("/a", position=0, pk=1),
            self._r("/b", position=1, pk=2),
        ])
        assert groups == []
        assert rs.identities() == {("/a", "exact"), ("/b", "exact")}

    def test_keeps_lowest_position_record(self):
        # The anyscale-ray discovery shape: same identity, two positions.
        rs, groups = RedirectSet.from_api([
            self._r("/dep.html", position=235, pk=7289),
            self._r("/dep.html", position=70, pk=7454),
        ])
        assert len(rs) == 1
        assert rs.get(("/dep.html", "exact")).pk == 7454  # the pos-70 record fires
        assert len(groups) == 1
        assert groups[0].kept.pk == 7454
        assert [s.pk for s in groups[0].shadowed] == [7289]

    def test_shadowed_records_retain_pks_for_deletion(self):
        _, groups = RedirectSet.from_api([
            self._r("/x", position=0, pk=10),
            self._r("/x", position=5, pk=20),
            self._r("/x", position=9, pk=30),
        ])
        assert [s.pk for s in groups[0].shadowed] == [20, 30]

    def test_never_drops_a_record(self):
        records = [
            self._r("/x", position=0, pk=1),
            self._r("/x", position=1, pk=2),
            self._r("/y", position=2, pk=3),
        ]
        rs, groups = RedirectSet.from_api(records)
        accounted = len(rs) + sum(len(g.shadowed) for g in groups)
        assert accounted == len(records)

    def test_groups_sorted_by_identity(self):
        _, groups = RedirectSet.from_api([
            self._r("/z", position=0, pk=1),
            self._r("/z", position=1, pk=2),
            self._r("/a", position=2, pk=3),
            self._r("/a", position=3, pk=4),
        ])
        assert [g.identity for g in groups] == [("/a", "exact"), ("/z", "exact")]

    def test_position_tie_breaks_on_pk(self):
        rs, groups = RedirectSet.from_api([
            self._r("/x", position=0, pk=99),
            self._r("/x", position=0, pk=12),
        ])
        assert rs.get(("/x", "exact")).pk == 12
        assert [s.pk for s in groups[0].shadowed] == [99]

    def test_same_from_url_different_type_is_not_a_duplicate(self):
        rs, groups = RedirectSet.from_api([
            self._r("/a", type="exact", pk=1),
            self._r("/a", type="page", pk=2),
        ])
        assert groups == []
        assert len(rs) == 2

    def test_result_set_round_trips_clean(self):
        # The deduped set has one record per identity, so it never trips add().
        rs, _ = RedirectSet.from_api([
            self._r("/x", position=0, pk=1),
            self._r("/x", position=1, pk=2),
        ])
        assert RedirectSet(list(rs)) == rs  # constructor accepts it without raising


class TestDuplicateGroup:
    @staticmethod
    def _r(from_url: str, to_url: str, *, position: int = 0, pk: int = 0) -> Redirect:
        return Redirect(
            from_url=from_url, to_url=to_url, type="exact", position=position, pk=pk,
        )

    def test_same_target_true(self):
        g = DuplicateGroup(
            identity=("/x", "exact"),
            kept=self._r("/x", "/dest", pk=1),
            shadowed=[self._r("/x", "/dest", pk=2)],
        )
        assert g.same_target is True

    def test_same_target_false_is_the_dangerous_case(self):
        # /serialization.html fired the wrong target in the discovery data.
        g = DuplicateGroup(
            identity=("/serialization.html", "exact"),
            kept=self._r("/serialization.html", "/configure.html", position=38, pk=7486),
            shadowed=[
                self._r("/serialization.html", "/ray-core/serial.html", position=74, pk=7450),
            ],
        )
        assert g.same_target is False

    def test_same_target_false_when_any_shadowed_differs(self):
        g = DuplicateGroup(
            identity=("/x", "exact"),
            kept=self._r("/x", "/dest", pk=1),
            shadowed=[self._r("/x", "/dest", pk=2), self._r("/x", "/other", pk=3)],
        )
        assert g.same_target is False
