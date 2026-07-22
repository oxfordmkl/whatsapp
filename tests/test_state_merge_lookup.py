"""
Phase 1.5.5E — unit tests for the merged is_new_lead lookup (Phase 4).

state.py is loaded by file path; every collaborator it imports lazily
(app.models, app.extensions, app.services.log_service, app.flags, and flask) is
injected as a fake via monkeypatch. A minimal fake `flask` supplies the
request-context / g behavior state.py's cache relies on — this keeps the test
independent of other suites that stub `flask` in sys.modules, and lets us toggle
"inside a request" precisely. No app bootstrap; no permanent sys.modules changes.

Central assertion: with STATE_MERGE_LOOKUP ON, resolve_is_new_lead() +
get_or_create_state() together issue exactly ONE row SELECT (the second call is
served from the request cache); with the flag OFF, behavior is the legacy
phone_exists() count followed by an independent load.
"""
import importlib.util
import os
import sys
import types
from contextlib import contextmanager

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


state = _load("_p4_state", "app/state.py")

_TO_DICT_KEYS = ("name", "stage", "course", "goal",
                 "batch_time", "offer_course", "last_msg", "last_text")


class _FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self):
        return {k: self.__dict__.get(k, "") for k in _TO_DICT_KEYS}


@pytest.fixture
def env(monkeypatch):
    """Install fakes; return handles for driving existence + counting queries."""
    counters = {"first": 0, "count": 0}
    existing = {"row": None}  # None → phone is new

    class _Query:
        def filter_by(self, **kw):
            return self

        def first(self):
            counters["first"] += 1
            return existing["row"]

        def count(self):
            counters["count"] += 1
            return 1 if existing["row"] is not None else 0

    class _CS:
        query = _Query()

        def __init__(self, **kw):
            self.__dict__.update(kw)

        def to_dict(self):
            return {k: self.__dict__.get(k, "") for k in _TO_DICT_KEYS}

    models = types.ModuleType("app.models")
    models.ConversationState = _CS
    monkeypatch.setitem(sys.modules, "app.models", models)

    class _Session:
        def __init__(self):
            self.commits = 0

        def add(self, row):
            pass

        def commit(self):
            self.commits += 1

    ext = types.ModuleType("app.extensions")
    ext.db = types.SimpleNamespace(session=_Session())
    monkeypatch.setitem(sys.modules, "app.extensions", ext)

    log = types.ModuleType("app.services.log_service")
    log.resolve_tenant_id = lambda tid=None: tid or "t1"
    monkeypatch.setitem(sys.modules, "app.services.log_service", log)

    flag = {"merge": False}
    flags = types.ModuleType("app.flags")
    flags.state_merge_lookup_enabled = lambda: flag["merge"]
    flags.state_engine_v2_enabled = lambda: False  # use V1 proxy in these tests
    monkeypatch.setitem(sys.modules, "app.flags", flags)

    # Minimal fake `flask` for request-context + g, controllable per test.
    fake_flask = types.ModuleType("flask")
    holder = {"active": False}
    fake_flask.has_request_context = lambda: holder["active"]
    fake_flask.g = None
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    @contextmanager
    def request_ctx():
        holder["active"] = True
        fake_flask.g = types.SimpleNamespace()  # fresh g per request
        try:
            yield
        finally:
            holder["active"] = False
            fake_flask.g = None

    return types.SimpleNamespace(
        counters=counters, existing=existing, flag=flag, request_ctx=request_ctx,
    )


class TestFlagOff:
    def test_resolve_uses_count_not_first(self, env):
        env.flag["merge"] = False
        env.existing["row"] = None
        with env.request_ctx():
            assert state.resolve_is_new_lead("+1", "Alice", tenant_id="t1") is True
        assert env.counters["count"] == 1  # phone_exists count
        assert env.counters["first"] == 0  # no load in the OFF path

    def test_resolve_existing_returns_false(self, env):
        env.flag["merge"] = False
        env.existing["row"] = _FakeRow(stage="goal_selection")
        with env.request_ctx():
            assert state.resolve_is_new_lead("+1", "Alice", tenant_id="t1") is False
        assert env.counters["count"] == 1


class TestFlagOnMergesLoad:
    def test_new_lead_single_select_reused(self, env):
        env.flag["merge"] = True
        env.existing["row"] = None  # new phone
        with env.request_ctx():
            is_new = state.resolve_is_new_lead("+1", "Alice", tenant_id="t1")
            assert is_new is True
            # smart_reply's later call must be served from cache (no new SELECT):
            proxy = state.get_or_create_state("+1", "Alice", tenant_id="t1")
            assert proxy["stage"] == "new"

        assert env.counters["count"] == 0     # phone_exists NOT used
        assert env.counters["first"] == 1     # exactly one load for both calls

    def test_existing_lead_single_select_reused(self, env):
        env.flag["merge"] = True
        env.existing["row"] = _FakeRow(name="Bob", stage="demo_booked")
        with env.request_ctx():
            is_new = state.resolve_is_new_lead("+1", "Bob", tenant_id="t1")
            assert is_new is False
            proxy = state.get_or_create_state("+1", "Bob", tenant_id="t1")
            assert proxy["stage"] == "demo_booked"

        assert env.counters["count"] == 0
        assert env.counters["first"] == 1     # one SELECT serves both

    def test_cache_hit_returns_same_proxy(self, env):
        env.flag["merge"] = True
        env.existing["row"] = _FakeRow(stage="new")
        with env.request_ctx():
            state.resolve_is_new_lead("+1", "Alice", tenant_id="t1")
            p1 = state.get_or_create_state("+1", "Alice", tenant_id="t1")
            p2 = state.get_or_create_state("+1", "Alice", tenant_id="t1")
            assert p1 is p2               # same cached proxy identity
        assert env.counters["first"] == 1


class TestFlagOnSafety:
    def test_get_or_create_without_resolve_still_loads(self, env):
        """Cache miss (resolve not called first) → normal single load."""
        env.flag["merge"] = True
        env.existing["row"] = _FakeRow(stage="new")
        with env.request_ctx():
            proxy = state.get_or_create_state("+1", "Alice", tenant_id="t1")
            assert proxy["stage"] == "new"
        assert env.counters["first"] == 1

    def test_no_request_context_skips_cache(self, env):
        """Outside a request (scheduler/CLI) → no caching, each call loads."""
        env.flag["merge"] = True
        env.existing["row"] = _FakeRow(stage="new")
        # No request_ctx() → has_request_context() is False.
        state.resolve_is_new_lead("+1", "Alice", tenant_id="t1")
        state.get_or_create_state("+1", "Alice", tenant_id="t1")
        assert env.counters["first"] == 2  # no cache → two independent loads
