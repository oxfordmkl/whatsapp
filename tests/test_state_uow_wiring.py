"""
Phase 1.5.5D — unit tests for the flag-aware UnitOfWork wiring (Phase 3).

The real unit_of_work module is path-loaded and registered in sys.modules under
its true dotted name so scope.py's lazy import binds to the SAME module (and thus
the same contextvar). app.flags and app.extensions are injected as fakes via
monkeypatch (auto-reverted). No app bootstrap; no permanent sys.modules changes.

Covered:
  * state_unit_of_work(): flag OFF → no unit active; flag ON → unit active,
    committed once on exit (after the block body, i.e. after send_reply)
  * flush_state_writes(): flushes the active unit; no-op when none
  * reset_active_unit_of_work(): clears a leaked unit (teardown net primitive)
  * re-entrancy through the scope commits exactly once
"""
import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath, monkeypatch=None):
    """Load a module by path. If monkeypatch is given, register it under
    `unique_name` in sys.modules FIRST (auto-reverted on teardown) so lazy
    imports elsewhere resolve to this exact instance."""
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, unique_name, mod)
    spec.loader.exec_module(mod)
    return mod


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        self.flushes += 1


@pytest.fixture
def wired(monkeypatch):
    """Load real uow + scope sharing one contextvar; inject fake flags/extensions.

    Returns a namespace with the modules and a controllable flag + session.
    """
    session = _FakeSession()
    flag = {"on": False}

    # Fake app.extensions so UnitOfWork(session=None).session resolves to our fake.
    ext = types.ModuleType("app.extensions")
    ext.db = types.SimpleNamespace(session=session)
    monkeypatch.setitem(sys.modules, "app.extensions", ext)

    # Fake app.flags with a controllable STATE_UOW_CONTEXT.
    flags = types.ModuleType("app.flags")
    flags.state_uow_context_enabled = lambda: flag["on"]
    monkeypatch.setitem(sys.modules, "app.flags", flags)

    # Real unit_of_work, registered under its true name so scope binds to it
    # (auto-reverted via monkeypatch).
    uow_mod = _load("app.persistence.unit_of_work", "app/persistence/unit_of_work.py",
                    monkeypatch=monkeypatch)
    scope = _load("_p3_scope", "app/persistence/scope.py")

    return types.SimpleNamespace(uow=uow_mod, scope=scope, session=session, flag=flag)


class TestStateUnitOfWorkScope:
    def test_flag_off_no_unit(self, wired):
        wired.flag["on"] = False
        with wired.scope.state_unit_of_work() as u:
            assert u is None
            assert wired.uow.current_unit_of_work() is None
        assert wired.session.commits == 0  # nothing opened, nothing committed

    def test_flag_on_opens_and_commits_once(self, wired):
        wired.flag["on"] = True
        with wired.scope.state_unit_of_work() as u:
            assert u is not None
            assert wired.uow.current_unit_of_work() is u
            assert wired.session.commits == 0  # not yet — commit is on exit
        assert wired.session.commits == 1      # committed after the block body
        assert wired.uow.current_unit_of_work() is None

    def test_flag_on_rolls_back_on_error(self, wired):
        wired.flag["on"] = True
        with pytest.raises(RuntimeError):
            with wired.scope.state_unit_of_work():
                raise RuntimeError("boom")
        assert wired.session.commits == 0
        assert wired.session.rollbacks == 1
        assert wired.uow.current_unit_of_work() is None


class TestFlushStateWrites:
    def test_flush_noop_without_unit(self, wired):
        wired.flag["on"] = False
        wired.scope.flush_state_writes()  # must not raise
        assert wired.session.flushes == 0

    def test_flush_flushes_active_unit(self, wired):
        wired.flag["on"] = True
        with wired.scope.state_unit_of_work():
            wired.scope.flush_state_writes()
            assert wired.session.flushes == 1
            assert wired.session.commits == 0  # flush is not commit
        assert wired.session.commits == 1


class TestReentrancyThroughScope:
    def test_nested_scope_commits_once(self, wired):
        wired.flag["on"] = True
        with wired.scope.state_unit_of_work() as outer:
            with wired.scope.state_unit_of_work() as inner:
                assert inner is outer  # inner joins outer unit
            assert wired.session.commits == 0  # inner must not commit
        assert wired.session.commits == 1


class TestSafetyNetReset:
    def test_reset_clears_leaked_unit(self, wired):
        # Simulate a leak: set an active unit without the context manager.
        u = wired.uow.UnitOfWork(session=wired.session)
        wired.uow._active_uow.set(u)
        assert wired.uow.current_unit_of_work() is u

        wired.uow.reset_active_unit_of_work()
        assert wired.uow.current_unit_of_work() is None
