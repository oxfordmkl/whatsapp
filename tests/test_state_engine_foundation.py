"""
Phase 1.5.5B — unit tests for the isolated State Engine foundation (Phase 0 + 1).

These modules are UNWIRED, so the tests load them directly by file path and
inject collaborators (a fake or in-memory session). Nothing here imports the
`app` package, so:
  - no DATABASE_URL / app bootstrap is required, and
  - no sys.modules entries are replaced (zero contamination of the wider suite).

Covered:
  * app/flags.py                 — dynamic, per-call env reads; default OFF
  * app/context/tenant_context.py — contextvars set/get/reset + scope nesting
  * app/persistence/unit_of_work.py — commit/rollback boundary + re-entrancy
  * app/persistence/conversation_state_repository.py — CRUD, tenant isolation,
                                     managed rows, and the no-commit contract
"""
import importlib.util
import os

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    """Load a module straight from its file, bypassing package import.

    A unique (non-`app.*`) name is used so the real package namespace and
    sys.modules are never touched.
    """
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


flags = _load("_fnd_flags", "app/flags.py")
tenant_context = _load("_fnd_tenant_context", "app/context/tenant_context.py")
uow_mod = _load("_fnd_uow", "app/persistence/unit_of_work.py")
repo_mod = _load("_fnd_repo", "app/persistence/conversation_state_repository.py")


# ─────────────────────────────── flags ────────────────────────────────────
class TestFlags:
    def test_default_off(self, monkeypatch):
        for name in (flags.STATE_ENGINE_V2, flags.STATE_UOW_CONTEXT, flags.STATE_MERGE_LOOKUP):
            monkeypatch.delenv(name, raising=False)
        assert flags.state_engine_v2_enabled() is False
        assert flags.state_uow_context_enabled() is False
        assert flags.state_merge_lookup_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv(flags.STATE_ENGINE_V2, value)
        assert flags.state_engine_v2_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_falsey_spellings(self, monkeypatch, value):
        monkeypatch.setenv(flags.STATE_ENGINE_V2, value)
        assert flags.state_engine_v2_enabled() is False

    def test_dynamic_reread(self, monkeypatch):
        """Instant rollback: a live env change is honored without re-import."""
        monkeypatch.delenv(flags.STATE_UOW_CONTEXT, raising=False)
        assert flags.state_uow_context_enabled() is False
        monkeypatch.setenv(flags.STATE_UOW_CONTEXT, "true")
        assert flags.state_uow_context_enabled() is True
        monkeypatch.setenv(flags.STATE_UOW_CONTEXT, "false")
        assert flags.state_uow_context_enabled() is False

    def test_flags_are_independent(self, monkeypatch):
        monkeypatch.setenv(flags.STATE_ENGINE_V2, "true")
        monkeypatch.delenv(flags.STATE_UOW_CONTEXT, raising=False)
        monkeypatch.delenv(flags.STATE_MERGE_LOOKUP, raising=False)
        assert flags.state_engine_v2_enabled() is True
        assert flags.state_uow_context_enabled() is False
        assert flags.state_merge_lookup_enabled() is False


# ────────────────────────── tenant context ────────────────────────────────
class TestTenantContext:
    def test_default_none(self):
        assert tenant_context.get_current_tenant_id() is None

    def test_set_get_reset(self):
        token = tenant_context.set_current_tenant_id("tenant-a")
        try:
            assert tenant_context.get_current_tenant_id() == "tenant-a"
        finally:
            tenant_context.reset_current_tenant_id(token)
        assert tenant_context.get_current_tenant_id() is None

    def test_scope_restores_previous(self):
        with tenant_context.tenant_context("outer"):
            assert tenant_context.get_current_tenant_id() == "outer"
            with tenant_context.tenant_context("inner"):
                assert tenant_context.get_current_tenant_id() == "inner"
            assert tenant_context.get_current_tenant_id() == "outer"
        assert tenant_context.get_current_tenant_id() is None

    def test_scope_restores_on_exception(self):
        with pytest.raises(ValueError):
            with tenant_context.tenant_context("boom"):
                raise ValueError("x")
        assert tenant_context.get_current_tenant_id() is None


# ─────────────────────────── unit of work ─────────────────────────────────
class _FakeSession:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0
        self.flushed = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def flush(self):
        self.flushed += 1


class TestUnitOfWork:
    def test_commits_on_clean_exit(self):
        s = _FakeSession()
        with uow_mod.unit_of_work(session=s) as u:
            assert u.session is s
        assert s.committed == 1
        assert s.rolled_back == 0

    def test_rolls_back_and_reraises_on_error(self):
        s = _FakeSession()
        with pytest.raises(RuntimeError):
            with uow_mod.unit_of_work(session=s):
                raise RuntimeError("fail")
        assert s.committed == 0
        assert s.rolled_back == 1

    def test_current_unit_of_work_visibility(self):
        assert uow_mod.current_unit_of_work() is None
        s = _FakeSession()
        with uow_mod.unit_of_work(session=s) as u:
            assert uow_mod.current_unit_of_work() is u
        assert uow_mod.current_unit_of_work() is None

    def test_reentrant_join_commits_once(self):
        s = _FakeSession()
        with uow_mod.unit_of_work(session=s) as outer:
            with uow_mod.unit_of_work() as inner:
                assert inner is outer  # inner joins the outer unit
            assert s.committed == 0  # inner block must NOT commit
        assert s.committed == 1  # only the outer block commits

    def test_reentrant_inner_error_rolls_back_once(self):
        s = _FakeSession()
        with pytest.raises(ValueError):
            with uow_mod.unit_of_work(session=s):
                with uow_mod.unit_of_work():
                    raise ValueError("inner")
        assert s.committed == 0
        assert s.rolled_back == 1

    def test_flush_does_not_commit(self):
        s = _FakeSession()
        u = uow_mod.UnitOfWork(session=s)
        u.flush()
        assert s.flushed == 1
        assert s.committed == 0


# ─────────────────────────── repository ───────────────────────────────────
_Base = declarative_base()


class _FakeState(_Base):
    """Minimal ConversationState-shaped model for isolated repo testing."""
    __tablename__ = "conversation_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), nullable=False)
    tenant_id = Column(String(36), nullable=False)
    name = Column(String(100))
    stage = Column(String(50), default="new")
    course = Column(String(200), default="")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def repo(session):
    return repo_mod.SQLAlchemyConversationStateRepository(session=session, model=_FakeState)


class TestRepository:
    def test_get_missing_returns_none(self, repo):
        assert repo.get("t1", "+1") is None

    def test_get_or_create_creates_then_reuses(self, repo):
        row, created = repo.get_or_create("t1", "+1", "Alice")
        assert created is True
        assert row.id is not None  # flush assigned the PK
        assert row.stage == "new"  # model default applied
        assert row.name == "Alice"

        same, created2 = repo.get_or_create("t1", "+1", "Alice2")
        assert created2 is False
        assert same.id == row.id
        assert same.name == "Alice"  # existing row returned unchanged

    def test_get_returns_managed_row_after_create(self, repo):
        created_row, _ = repo.get_or_create("t1", "+1", "Alice")
        fetched = repo.get("t1", "+1")
        assert fetched is created_row  # same managed identity

    def test_exists(self, repo):
        assert repo.exists("t1", "+1") is False
        repo.get_or_create("t1", "+1", "Alice")
        assert repo.exists("t1", "+1") is True

    def test_tenant_isolation(self, repo):
        r1, _ = repo.get_or_create("t1", "+1", "Alice")
        r2, created = repo.get_or_create("t2", "+1", "Bob")
        assert created is True  # same phone, different tenant → distinct row
        assert r2.id != r1.id
        assert repo.get("t1", "+1").name == "Alice"
        assert repo.get("t2", "+1").name == "Bob"
        assert repo.exists("t1", "+2") is False

    def test_repository_never_commits(self, repo, session, monkeypatch):
        commits = {"n": 0}
        real_commit = session.commit

        def spy_commit():
            commits["n"] += 1
            return real_commit()

        monkeypatch.setattr(session, "commit", spy_commit)

        repo.get_or_create("t1", "+1", "Alice")
        repo.get("t1", "+1")
        repo.exists("t1", "+1")

        assert commits["n"] == 0  # transaction boundary belongs to the UoW, not the repo

    def test_created_row_not_persisted_until_commit(self, repo, session):
        """No-commit contract: a flushed-but-uncommitted row rolls back cleanly."""
        repo.get_or_create("t1", "+1", "Alice")
        session.rollback()  # UoW would roll back on error
        assert repo.get("t1", "+1") is None
