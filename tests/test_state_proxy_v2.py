"""
Phase 1.5.5C — unit tests for StateProxy V2 (managed-row proxy, flag-gated).

state.py is loaded by file path (its module top-level only needs `datetime`),
and every app-level dependency it imports lazily is injected as a fake via
sys.modules using monkeypatch (auto-reverted). No DATABASE_URL / app bootstrap,
no permanent sys.modules mutation → zero contamination of the wider suite.

Covered:
  * StateProxyV2 read snapshot + dict API preserved
  * writes route through the model setter (setattr on the managed row)
  * persisted-key write → immediate commit when no UnitOfWork (fallback)
  * persisted-key write → flush (not commit) when a UnitOfWork is active
  * non-persisted key → snapshot updated, row untouched, no commit
  * get_or_create_state flag branch: OFF → V1 StateProxy, ON → StateProxyV2
"""
import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


state = _load("_p2_state", "app/state.py")


class _FakeRow:
    """ConversationState-shaped stand-in. setattr on a persisted business field
    is recorded (emulating the model's own setter surface)."""

    _PERSIST = set(state._PERSISTED_KEYS)

    def __init__(self, data):
        object.__setattr__(self, "_data", dict(data))
        object.__setattr__(self, "writes", [])

    def to_dict(self):
        return dict(self._data)

    def __setattr__(self, key, value):
        if key in _FakeRow._PERSIST:
            self.writes.append((key, value))
            self._data[key] = value
        else:
            object.__setattr__(self, key, value)


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.flushes = 0

    def commit(self):
        self.commits += 1

    def flush(self):
        self.flushes += 1


def _install_fakes(monkeypatch, *, active_uow, session):
    """Inject the lazily-imported collaborators state.py's V2 path resolves."""
    uow_mod = types.ModuleType("app.persistence.unit_of_work")
    uow_mod.current_unit_of_work = lambda: active_uow
    monkeypatch.setitem(sys.modules, "app.persistence.unit_of_work", uow_mod)

    ext_mod = types.ModuleType("app.extensions")
    db_obj = types.SimpleNamespace(session=session)
    ext_mod.db = db_obj
    monkeypatch.setitem(sys.modules, "app.extensions", ext_mod)


def _seed():
    return {
        "name": "Alice", "stage": "new", "course": "", "goal": "",
        "batch_time": "", "offer_course": "", "last_msg": "", "last_text": "",
    }


class TestStateProxyV2Reads:
    def test_is_dict_and_reads_snapshot(self):
        p = state.StateProxyV2("+1", _FakeRow(_seed()), tenant_id="t1")
        assert isinstance(p, dict)
        assert p["stage"] == "new"
        assert p["name"] == "Alice"
        assert p.get("goal") == ""
        assert p.get("missing", "d") == "d"
        assert "course" in p


class TestStateProxyV2Writes:
    def test_persisted_write_routes_through_setter_and_commits(self, monkeypatch):
        session = _FakeSession()
        _install_fakes(monkeypatch, active_uow=None, session=session)
        row = _FakeRow(_seed())
        p = state.StateProxyV2("+1", row, tenant_id="t1")

        p["stage"] = "goal_selection"

        assert p["stage"] == "goal_selection"          # snapshot updated
        assert ("stage", "goal_selection") in row.writes  # model setter used
        assert session.commits == 1                    # immediate-flush fallback
        assert session.flushes == 0

    def test_multiple_writes_commit_each_without_uow(self, monkeypatch):
        session = _FakeSession()
        _install_fakes(monkeypatch, active_uow=None, session=session)
        row = _FakeRow(_seed())
        p = state.StateProxyV2("+1", row, tenant_id="t1")

        p["last_msg"] = "2026-01-01"
        p["last_text"] = "hi"

        assert session.commits == 2  # fallback preserves per-assignment durability
        assert ("last_msg", "2026-01-01") in row.writes
        assert ("last_text", "hi") in row.writes

    def test_active_uow_defers_commit_to_flush(self, monkeypatch):
        session = _FakeSession()
        active = types.SimpleNamespace(flush=lambda: None)  # active unit, no-op flush
        _install_fakes(monkeypatch, active_uow=active, session=session)
        row = _FakeRow(_seed())
        p = state.StateProxyV2("+1", row, tenant_id="t1")

        p["stage"] = "demo_time_ask"

        assert session.commits == 0  # UoW owns the commit
        # flush routes to the UoW, not the fake session, so session.flush stays 0;
        # the point is: no commit happened here.
        assert ("stage", "demo_time_ask") in row.writes

    def test_active_uow_flush_called(self, monkeypatch):
        flushes = {"n": 0}
        active = types.SimpleNamespace(flush=lambda: flushes.__setitem__("n", flushes["n"] + 1))
        _install_fakes(monkeypatch, active_uow=active, session=_FakeSession())
        p = state.StateProxyV2("+1", _FakeRow(_seed()), tenant_id="t1")

        p["course"] = "PGDCA"

        assert flushes["n"] == 1  # deferred to the UoW's flush

    def test_non_persisted_key_updates_snapshot_only(self, monkeypatch):
        session = _FakeSession()
        _install_fakes(monkeypatch, active_uow=None, session=session)
        row = _FakeRow(_seed())
        p = state.StateProxyV2("+1", row, tenant_id="t1")

        p["transient"] = "x"

        assert p["transient"] == "x"     # snapshot holds it
        assert row.writes == []          # row untouched
        assert session.commits == 0      # no persistence for non-column keys


class TestGetOrCreateStateFlagBranch:
    """Verify the flag selects the proxy class; DB access is fully faked."""

    def _install_full(self, monkeypatch, v2_enabled):
        row = _FakeRow(_seed())

        # app.models.ConversationState.query.filter_by(...).first() → row
        first = types.SimpleNamespace(first=lambda: row)
        query = types.SimpleNamespace(filter_by=lambda **kw: first)
        ConversationState = types.SimpleNamespace(query=query)
        models = types.ModuleType("app.models")
        models.ConversationState = ConversationState
        monkeypatch.setitem(sys.modules, "app.models", models)

        ext = types.ModuleType("app.extensions")
        ext.db = types.SimpleNamespace(session=_FakeSession())
        monkeypatch.setitem(sys.modules, "app.extensions", ext)

        log = types.ModuleType("app.services.log_service")
        log.resolve_tenant_id = lambda tid=None: tid or "t1"
        monkeypatch.setitem(sys.modules, "app.services.log_service", log)

        flags = types.ModuleType("app.flags")
        flags.state_engine_v2_enabled = lambda: v2_enabled
        flags.state_merge_lookup_enabled = lambda: False  # Phase 4 path inert here
        monkeypatch.setitem(sys.modules, "app.flags", flags)
        return row

    def test_flag_off_returns_v1_proxy(self, monkeypatch):
        self._install_full(monkeypatch, v2_enabled=False)
        st = state.get_or_create_state("+1", "Alice", tenant_id="t1")
        assert isinstance(st, state.StateProxy)
        assert not isinstance(st, state.StateProxyV2)

    def test_flag_on_returns_v2_proxy(self, monkeypatch):
        self._install_full(monkeypatch, v2_enabled=True)
        st = state.get_or_create_state("+1", "Alice", tenant_id="t1")
        assert isinstance(st, state.StateProxyV2)
        assert st["stage"] == "new"
