"""Tests for rtd_redirects.apply: drive a Diff against RtdClient."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from rtd_redirects.apply import ApplyResult, apply, apply_converging
from rtd_redirects.client import RtdClient
from rtd_redirects.diff import Diff, Update, diff
from rtd_redirects.model import Redirect, RedirectSet

from ._fakes import FakeRtd, StuckRtd


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


def _page(from_url: str, to_url: str, position: int) -> Redirect:
    return Redirect(from_url=from_url, to_url=to_url, type="page", position=position)


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock(spec=RtdClient)
    # create_redirect returns the created record with a pk so apply can log it.
    c.create_redirect.side_effect = lambda r: _r(r.from_url, to_url=r.to_url, pk=99)
    c.update_redirect.side_effect = lambda pk, r: _r(r.from_url, to_url=r.to_url, pk=pk)
    return c


class TestEmpty:
    def test_empty_diff_no_calls(self, client: MagicMock):
        result = apply(Diff(), client, log=io.StringIO())
        assert result.total == 0
        assert client.method_calls == []


class TestPhases:
    def test_pure_adds(self, client: MagicMock):
        d = Diff(adds=[_r("/a"), _r("/b")])
        result = apply(d, client, log=io.StringIO())
        assert result == ApplyResult(added=2)
        assert client.create_redirect.call_count == 2

    def test_pure_deletes(self, client: MagicMock):
        d = Diff(deletes=[_r("/a", pk=1), _r("/b", pk=2)])
        result = apply(d, client, log=io.StringIO())
        assert result == ApplyResult(deleted=2)
        assert [c.args[0] for c in client.delete_redirect.call_args_list] == [1, 2]

    def test_pure_updates(self, client: MagicMock):
        d = Diff(updates=[
            Update(target=_r("/a", pk=42), source=_r("/a", to_url="/new")),
        ])
        result = apply(d, client, log=io.StringIO())
        assert result == ApplyResult(updated=1)
        assert client.update_redirect.call_args.args[0] == 42
        assert client.update_redirect.call_args.args[1].to_url == "/new"

    def test_pure_reorders(self, client: MagicMock):
        d = Diff(reorders=[
            Update(target=_r("/a", position=0, pk=42), source=_r("/a", position=5)),
        ])
        result = apply(d, client, log=io.StringIO())
        assert result == ApplyResult(reordered=1)
        assert client.update_redirect.call_args.args[0] == 42
        assert client.update_redirect.call_args.args[1].position == 5


class TestOrdering:
    def test_phases_run_deletes_adds_updates_reorders(self, client: MagicMock):
        d = Diff(
            adds=[_r("/add")],
            deletes=[_r("/del", pk=1)],
            updates=[Update(target=_r("/upd", pk=2), source=_r("/upd", to_url="/new"))],
            reorders=[Update(
                target=_r("/reord", position=0, pk=3),
                source=_r("/reord", position=5),
            )],
        )
        apply(d, client, log=io.StringIO())

        method_order = [c[0] for c in client.method_calls]
        assert method_order == [
            "delete_redirect",
            "create_redirect",
            "update_redirect",  # update
            "update_redirect",  # reorder
        ]


class TestPkValidation:
    def test_delete_without_pk_raises(self, client: MagicMock):
        with pytest.raises(ValueError, match="cannot delete"):
            apply(Diff(deletes=[_r("/a")]), client, log=io.StringIO())
        client.delete_redirect.assert_not_called()

    def test_update_without_pk_raises(self, client: MagicMock):
        d = Diff(updates=[Update(target=_r("/a"), source=_r("/a", to_url="/new"))])
        with pytest.raises(ValueError, match="cannot update"):
            apply(d, client, log=io.StringIO())
        client.update_redirect.assert_not_called()

    def test_reorder_without_pk_raises(self, client: MagicMock):
        d = Diff(reorders=[Update(target=_r("/a"), source=_r("/a", position=5))])
        with pytest.raises(ValueError, match="cannot reorder"):
            apply(d, client, log=io.StringIO())
        client.update_redirect.assert_not_called()


class TestLogging:
    def test_each_op_emits_one_line(self, client: MagicMock):
        d = Diff(
            adds=[_r("/add")],
            deletes=[_r("/del", pk=1)],
            updates=[Update(target=_r("/upd", pk=2), source=_r("/upd", to_url="/new"))],
            reorders=[Update(
                target=_r("/reord", position=0, pk=3),
                source=_r("/reord", position=5),
            )],
        )
        log = io.StringIO()
        apply(d, client, log=log)
        lines = log.getvalue().strip().splitlines()
        assert len(lines) == 4
        assert lines[0].startswith("DELETE /del")
        assert lines[1].startswith("CREATE /add")
        assert lines[2].startswith("UPDATE /upd")
        assert lines[3].startswith("REORDER /reord")
        assert "pk=1" in lines[0]
        assert "pk=99" in lines[1]   # the pk the mock returned from create
        assert "pk=2" in lines[2]
        assert "pk=3" in lines[3]
        assert "position=5" in lines[3]


class TestFailureBehavior:
    def test_delete_failure_stops_run(self, client: MagicMock):
        client.delete_redirect.side_effect = RuntimeError("boom")
        d = Diff(deletes=[_r("/del", pk=1)], adds=[_r("/add")])
        with pytest.raises(RuntimeError, match="boom"):
            apply(d, client, log=io.StringIO())
        client.create_redirect.assert_not_called()

    def test_add_failure_stops_updates_and_reorders(self, client: MagicMock):
        client.create_redirect.side_effect = RuntimeError("boom")
        d = Diff(
            adds=[_r("/add")],
            updates=[Update(target=_r("/upd", pk=2), source=_r("/upd", to_url="/new"))],
        )
        with pytest.raises(RuntimeError, match="boom"):
            apply(d, client, log=io.StringIO())
        client.update_redirect.assert_not_called()


class TestConvergence:
    """apply_converging reconciles whatever placement RtD gives new records.

    The slot RtD assigns on create isn't fixed (the bulk API apply landed
    records at the tail; the UI inserts at the top), so these tests run the
    converging driver against several create placements and assert it always
    reaches the source ordering. A single apply can't: its diff is computed
    before the adds exist, so it enumerates no reorders for them.
    """

    def _source(self) -> RedirectSet:
        # /a/sub/* (specific) must precede /a/* (general), but it sorts AFTER it
        # by identity, so an identity-ordered add can place them out of order.
        return RedirectSet([
            _page("/keep", "/dest", 0),
            _page("/a/sub/*", "/sub", 1),
            _page("/a/*", "/general", 2),
        ])

    def test_single_apply_can_leave_a_shadowed_rule(self):
        fake = FakeRtd(create_mode="append")
        source = self._source()
        # The one-shot diff has no reorders — the new records don't exist yet.
        apply(diff(source, RedirectSet(fake.list_redirects())), fake, log=io.StringIO())
        residual = diff(source, RedirectSet(fake.list_redirects()))
        assert not residual.is_empty
        order = [r.from_url for r in fake.list_redirects()]
        assert order.index("/a/*") < order.index("/a/sub/*")  # general shadows specific

    @pytest.mark.parametrize("create_mode", ["honor", "append", "prepend"])
    def test_converges_regardless_of_create_placement(self, create_mode: str):
        fake = FakeRtd(create_mode=create_mode)
        source = self._source()
        outcome = apply_converging(source, fake, log=io.StringIO())
        assert outcome.converged
        assert diff(source, RedirectSet(fake.list_redirects())).is_empty
        order = [r.from_url for r in fake.list_redirects()]
        assert order == ["/keep", "/a/sub/*", "/a/*"]

    def test_reports_residual_when_it_cannot_settle(self):
        fake = StuckRtd()
        source = self._source()
        outcome = apply_converging(source, fake, max_passes=3, log=io.StringIO())
        assert not outcome.converged
        assert outcome.passes == 3
        assert not outcome.residual.is_empty


class _DoubleCreateRtd(FakeRtd):
    """At-least-once POST: ``create_redirect`` inserts twice, as a retried POST
    would after a lost response — the duplicate-creation hazard DOC-946 names."""

    def create_redirect(self, r: Redirect) -> Redirect:
        created = super().create_redirect(r)
        super().create_redirect(r)  # the retry lands a second copy
        return created


class _UndeletableRtd(FakeRtd):
    """``delete_redirect`` is a no-op, so a live duplicate can never be healed."""

    def delete_redirect(self, pk: int) -> None:
        pass


class TestDuplicateHeal:
    """apply_converging heals the duplicate identities RtD permits but our
    source-of-truth doesn't: shadowed extras are deleted and live converges to
    one record per identity.
    """

    def test_heals_seeded_live_duplicate(self):
        # Two live records for one identity (insertion order = RtD position).
        fake = FakeRtd(initial=[
            _r("/dep.html", to_url="/deps", pk=7454),  # kept (position 0)
            _r("/dep.html", to_url="/deps", pk=7289),  # shadowed
        ])
        source = RedirectSet([_r("/dep.html", to_url="/deps")])
        outcome = apply_converging(source, fake, log=io.StringIO())
        assert outcome.converged
        live = fake.list_redirects()
        assert len(live) == 1
        assert outcome.result.deleted == 1  # the shadowed extra only

    def test_heals_duplicate_with_different_target(self):
        # The /serialization.html discovery shape: the record RtD serves fires
        # the WRONG target; source wants the shadowed one's target. Heal deletes
        # the extra and updates the survivor.
        fake = FakeRtd(initial=[
            _r("/serialization.html", to_url="/configure.html", pk=7486),        # kept
            _r("/serialization.html", to_url="/ray-core/serial.html", pk=7450),  # shadowed
        ])
        source = RedirectSet([_r("/serialization.html", to_url="/ray-core/serial.html")])
        outcome = apply_converging(source, fake, log=io.StringIO())
        assert outcome.converged
        live = fake.list_redirects()
        assert len(live) == 1
        assert live[0].to_url == "/ray-core/serial.html"

    def test_botched_retry_twin_is_healed_next_pass(self):
        fake = _DoubleCreateRtd()
        source = RedirectSet([_r("/new.html", to_url="/dest")])
        outcome = apply_converging(source, fake, log=io.StringIO())
        assert outcome.converged
        assert len(fake.list_redirects()) == 1
        assert outcome.result.added == 1
        assert outcome.result.deleted == 1  # the retry's twin, removed on a later pass

    def test_unhealed_duplicate_surfaces_in_residual(self):
        fake = _UndeletableRtd(initial=[
            _r("/x", to_url="/dest", pk=1),  # kept
            _r("/x", to_url="/dest", pk=2),  # shadowed, undeletable
        ])
        source = RedirectSet([_r("/x", to_url="/dest")])
        outcome = apply_converging(source, fake, max_passes=3, log=io.StringIO())
        assert not outcome.converged
        assert outcome.passes == 3
        assert any(r.pk == 2 for r in outcome.residual.deletes)

    def test_clean_data_is_unaffected(self):
        # No duplicates: behavior identical to the pre-heal converging driver.
        fake = FakeRtd(initial=[_r("/a", to_url="/dest", position=0, pk=1)])
        source = RedirectSet([
            _r("/a", to_url="/dest", position=0),
            _r("/b", to_url="/dest2", position=1),
        ])
        outcome = apply_converging(source, fake, log=io.StringIO())
        assert outcome.converged
        assert outcome.result.deleted == 0
        assert outcome.result.added == 1
