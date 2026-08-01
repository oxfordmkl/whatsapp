"""
Phase 8.2E.9-A — Audience resolver tests (ADR-025 D3/D4).

Covers:
  - segment enumeration and the drift guard against the live V1 definitions
  - tenant_id required (ADR-021)
  - unknown segment raises rather than resolving empty
  - D3: the resolver's own tenant filter contains an over-broad segment source
  - D3: a non-impersonating SUPER_ADMIN style unscoped source cannot leak
    another tenant's contacts (explicit regression case)
  - D4: opt-out excluded with IS NOT TRUE — NULL rows are RETAINED, which
    `== False` would silently drop
  - (phone, name) shape, name authority, ordering, read-only contract

`ConversationState` is re-declared locally against in-memory SQLite (the
pattern used by test_campaign_service.py and test_campaign_dispatch.py) so the
opt-out predicate is evaluated by a real engine rather than asserted against a
mock — the NULL-handling in D4 is the whole point and a mock would not exercise
it. `app.routes.admin` is never imported: it pulls app.config, which raises
without a DATABASE_URL. The segment source is injected instead.
"""
import importlib.util
import os
import re
import sys
import types
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESOLVER_PATH = os.path.join(_ROOT, "app", "marketing", "audience_resolver.py")
_ADMIN_PATH = os.path.join(_ROOT, "app", "routes", "admin.py")


def _load(unique_name, path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _code_only(src: str) -> str:
    """Strip docstrings and comments, preserving code layout.

    The structural guards below assert that certain constructs are ABSENT from
    the resolver. Those same names legitimately appear in its prose, which
    explains why they are avoided — asserting against raw source would make the
    guards fail on their own documentation, and the fix for that must not be to
    delete the explanation.
    """
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    src = re.sub(r"(?m)#.*$", "", src)
    return src


def _resolver_code() -> str:
    with open(_RESOLVER_PATH, encoding="utf-8") as fh:
        return _code_only(fh.read())


for _n in ["app", "app.marketing", "app.models", "app.extensions",
           "app.routes", "app.routes.admin"]:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

res = _load("_p82e9a_resolver", _RESOLVER_PATH)


# ── Local ConversationState mirroring the production columns used here ───────

_Base = declarative_base()


class _ConversationState(_Base):
    __tablename__ = "conversation_state"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(36), nullable=False)
    phone = Column(String(20), nullable=False)
    name = Column(String(200), default="")
    # nullable=True is the whole point of D4 — do not "tidy" this.
    is_opted_out = Column(Boolean, nullable=True, default=False)
    last_msg = Column(String(50), default="")


T1, T2 = "tenant-one", "tenant-two"


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


def _add(session, phone, name="Lead", tenant_id=T1, is_opted_out=False, last_msg=""):
    session.add(_ConversationState(
        tenant_id=tenant_id, phone=phone, name=name,
        is_opted_out=is_opted_out, last_msg=last_msg,
    ))
    session.commit()


def _source(**segments):
    """Build an injected segment source returning the given {segment: phones}."""
    def _fn(tenant_id):
        return {k: set(v) for k, v in segments.items()}
    return _fn


def _resolve(session, segment="All Leads", tenant_id=T1, source=None):
    return res.resolve(
        tenant_id, segment,
        session=session, state_model=_ConversationState,
        segment_source=source if source is not None else _source(**{segment: []}),
    )


# ── Segment enumeration ──────────────────────────────────────────────────────

class TestSegments:
    def test_list_segments_returns_eight(self):
        assert len(res.list_segments()) == 8

    def test_expected_segment_names(self):
        assert res.list_segments() == (
            "HOT Leads", "WARM Leads", "Demo Requested", "Fees Requested",
            "Placement Interested", "Needs Reply", "Critical Leads", "All Leads",
        )

    def test_segments_match_live_v1_definitions(self):
        """Drift guard: SEGMENTS is mirrored from _calculate_audiences(), which
        cannot be imported here. Assert against its source instead."""
        with open(_ADMIN_PATH, encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("def _calculate_audiences(")
        body = src[start:start + 4000]
        block = body[body.index("audiences = {"):body.index("}", body.index("audiences = {"))]
        live = set(re.findall(r'"([^"]+)":\s*set\(\)', block))
        assert live == set(res.SEGMENTS), (
            f"segment drift — live V1 has {live ^ set(res.SEGMENTS)} not in sync"
        )


# ── Guards ───────────────────────────────────────────────────────────────────

class TestGuards:
    @pytest.mark.parametrize("tenant", [None, "", 0, False])
    def test_falsy_tenant_refused(self, session, tenant):
        with pytest.raises(res.AudienceResolutionError):
            res.resolve(tenant, "All Leads", session=session,
                        state_model=_ConversationState,
                        segment_source=_source(**{"All Leads": ["p1"]}))

    def test_unknown_segment_raises_not_empty(self, session):
        """A typo must not be indistinguishable from a genuinely empty segment —
        D2 refuses empty audiences at launch, so the two must differ here."""
        with pytest.raises(res.AudienceResolutionError) as e:
            res.resolve(T1, "Hot Leads", session=session,   # wrong case
                        state_model=_ConversationState,
                        segment_source=_source(**{"HOT Leads": ["p1"]}))
        assert "unknown audience segment" in str(e.value)

    def test_error_lists_valid_segments(self, session):
        with pytest.raises(res.AudienceResolutionError) as e:
            res.resolve(T1, "Nope", session=session,
                        state_model=_ConversationState, segment_source=_source())
        assert "All Leads" in str(e.value)

    def test_empty_segment_returns_empty_list(self, session):
        _add(session, "p1")
        assert _resolve(session, source=_source(**{"All Leads": []})) == []

    def test_missing_segment_key_returns_empty_list(self, session):
        _add(session, "p1")
        assert _resolve(session, source=lambda t: {}) == []

    def test_none_source_result_returns_empty_list(self, session):
        _add(session, "p1")
        assert _resolve(session, source=lambda t: None) == []


# ── D4 — opt-out semantics ───────────────────────────────────────────────────

class TestOptOutSemantics:
    def test_opted_out_true_excluded(self, session):
        _add(session, "p_ok", is_opted_out=False)
        _add(session, "p_out", is_opted_out=True)
        out = _resolve(session, source=_source(**{"All Leads": ["p_ok", "p_out"]}))
        assert [r["phone"] for r in out] == ["p_ok"]

    def test_opted_out_null_is_RETAINED(self, session):
        """The core D4 case: NULL means 'never set', not 'opted out'.

        `is_opted_out == False` would drop this row. In production 17 of 49
        rows are NULL, so getting this wrong silently loses 35% of the audience.
        """
        _add(session, "p_null", is_opted_out=None)
        out = _resolve(session, source=_source(**{"All Leads": ["p_null"]}))
        assert [r["phone"] for r in out] == ["p_null"]

    def test_all_three_states_together(self, session):
        _add(session, "p_null", is_opted_out=None)
        _add(session, "p_false", is_opted_out=False)
        _add(session, "p_true", is_opted_out=True)
        out = _resolve(
            session, source=_source(**{"All Leads": ["p_null", "p_false", "p_true"]})
        )
        assert [r["phone"] for r in out] == ["p_false", "p_null"]

    def test_resolver_uses_isnot_not_equality(self):
        """Structural guard against a future 'simplification' back into the trap."""
        code = _resolver_code()
        assert "isnot(True)" in code
        assert "== False" not in code


# ── D3 — tenant isolation ────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_only_this_tenants_contacts_returned(self, session):
        _add(session, "p_a", tenant_id=T1)
        _add(session, "p_b", tenant_id=T2)
        out = _resolve(session, tenant_id=T1,
                       source=_source(**{"All Leads": ["p_a", "p_b"]}))
        assert [r["phone"] for r in out] == ["p_a"]

    def test_unscoped_segment_source_cannot_leak_contacts(self, session):
        """ADR-025 P3 regression case.

        Simulates `tenant_filter()`'s SUPER_ADMIN branch returning every
        tenant's rows. The resolver's own filter must contain it — this is the
        property that makes reusing _calculate_audiences() safe.
        """
        _add(session, "p_mine", tenant_id=T1)
        for p in ("p_other1", "p_other2", "p_other3"):
            _add(session, p, tenant_id=T2)

        unscoped = _source(**{"All Leads": [
            "p_mine", "p_other1", "p_other2", "p_other3"
        ]})
        out = _resolve(session, tenant_id=T1, source=unscoped)

        assert [r["phone"] for r in out] == ["p_mine"]

    def test_segment_source_receives_tenant_id(self, session):
        seen = {}

        def _spy(tenant_id):
            seen["tenant_id"] = tenant_id
            return {"All Leads": {"p1"}}

        _add(session, "p1")
        _resolve(session, tenant_id=T1, source=_spy)
        assert seen["tenant_id"] == T1

    def test_empty_tenant_resolves_empty(self, session):
        _add(session, "p_other", tenant_id=T2)
        out = _resolve(session, tenant_id=T1,
                       source=_source(**{"All Leads": ["p_other"]}))
        assert out == []

    def test_resolver_does_not_use_tenant_filter(self):
        assert "tenant_filter(" not in _resolver_code()


# ── Result shape and contract ────────────────────────────────────────────────

class TestResultShape:
    def test_returns_phone_and_name_dicts(self, session):
        _add(session, "p1", name="Alice")
        out = _resolve(session, source=_source(**{"All Leads": ["p1"]}))
        assert out == [{"phone": "p1", "name": "Alice"}]

    def test_shape_matches_add_recipients_input(self, session):
        """add_recipients() reads item['phone'] / item['name'] from dicts."""
        _add(session, "p1", name="Alice")
        item = _resolve(session, source=_source(**{"All Leads": ["p1"]}))[0]
        assert set(item) == {"phone", "name"}

    def test_name_comes_from_tenant_scoped_row(self, session):
        """The DB row is the authority for name, not the segment source."""
        _add(session, "p1", name="RealName", tenant_id=T1)
        out = _resolve(session, tenant_id=T1,
                       source=_source(**{"All Leads": ["p1"]}))
        assert out[0]["name"] == "RealName"

    def test_results_ordered_by_phone(self, session):
        for p in ("p3", "p1", "p2"):
            _add(session, p)
        out = _resolve(session, source=_source(**{"All Leads": ["p1", "p2", "p3"]}))
        assert [r["phone"] for r in out] == ["p1", "p2", "p3"]

    def test_classified_but_absent_phone_is_dropped(self, session):
        """A phone the source classified but which this tenant does not have."""
        _add(session, "p1")
        out = _resolve(session, source=_source(**{"All Leads": ["p1", "ghost"]}))
        assert [r["phone"] for r in out] == ["p1"]

    def test_named_segment_other_than_all_leads(self, session):
        _add(session, "p_hot")
        _add(session, "p_cold")
        out = res.resolve(
            T1, "HOT Leads", session=session, state_model=_ConversationState,
            segment_source=_source(**{"HOT Leads": ["p_hot"],
                                      "All Leads": ["p_hot", "p_cold"]}),
        )
        assert [r["phone"] for r in out] == ["p_hot"]


# ── Read-only contract (8.2E.9-A scope) ──────────────────────────────────────

class TestReadOnlyContract:
    def test_no_writes_or_commits_in_source(self):
        code = _resolver_code()
        for forbidden in (".commit()", ".rollback()", ".add(", ".add_all(",
                          ".delete(", ".flush("):
            assert forbidden not in code, f"resolver must not {forbidden}"

    def test_session_never_committed(self, session):
        _add(session, "p1")
        spy = MagicMock(wraps=session)
        res.resolve(T1, "All Leads", session=spy,
                    state_model=_ConversationState,
                    segment_source=_source(**{"All Leads": ["p1"]}))
        spy.commit.assert_not_called()
        spy.add.assert_not_called()

    def test_does_not_import_admin_at_module_level(self):
        src = _resolver_code()
        top = [l for l in src.splitlines()
               if l.startswith("import ") or l.startswith("from ")]
        for line in top:
            assert "routes.admin" not in line, (
                f"admin.py must be imported lazily, not at module level: {line!r}"
            )

    def test_does_not_import_campaign_service(self):
        assert "campaign_service" not in _resolver_code()

    def test_module_loads_without_app_package(self):
        assert callable(res.resolve) and callable(res.list_segments)


# ── preview() — ADR-025 D6.1 ──────────────────────────────────────────────────

from datetime import datetime, timedelta

_NOW = datetime(2026, 7, 25, 12, 0, 0)
_INSIDE_WINDOW = (_NOW - timedelta(hours=1)).isoformat()
_OUTSIDE_WINDOW = (_NOW - timedelta(hours=25)).isoformat()


def _preview(session, segment="All Leads", tenant_id=T1, source=None, now=_NOW):
    return res.preview(
        tenant_id, segment,
        session=session, state_model=_ConversationState,
        segment_source=source if source is not None else _source(**{segment: []}),
        now=now,
    )


class TestPreviewGuards:
    def test_falsy_tenant_refused(self, session):
        with pytest.raises(res.AudienceResolutionError):
            res.preview(None, "All Leads", session=session,
                        state_model=_ConversationState,
                        segment_source=_source(**{"All Leads": ["p1"]}))

    def test_unknown_segment_raises(self, session):
        with pytest.raises(res.AudienceResolutionError):
            res.preview(T1, "Not A Segment", session=session,
                        state_model=_ConversationState, segment_source=_source())

    def test_empty_segment_returns_zeroed_breakdown(self, session):
        out = _preview(session, source=_source(**{"All Leads": []}))
        assert out == {
            "segment": "All Leads", "total_audience": 0,
            "opted_out_excluded": 0, "reachable_now": 0, "template_required": 0,
        }

    def test_zeroed_breakdown_does_not_touch_db(self, session):
        """An empty classification must short-circuit before any query."""
        spy = MagicMock(wraps=session)
        res.preview(T1, "All Leads", session=spy, state_model=_ConversationState,
                    segment_source=_source(**{"All Leads": []}))
        spy.query.assert_not_called()


class TestPreviewBreakdown:
    def test_total_audience_counts_before_opt_out_exclusion(self, session):
        """total_audience per D6.1 is the FULL tenant-scoped classified count,
        opted-out contacts included — opted_out_excluded is reported
        separately so the two numbers together explain the funnel."""
        _add(session, "p1", is_opted_out=False, last_msg=_INSIDE_WINDOW)
        _add(session, "p2", is_opted_out=True,  last_msg=_INSIDE_WINDOW)
        out = _preview(session, source=_source(**{"All Leads": ["p1", "p2"]}))
        assert out["total_audience"] == 2
        assert out["opted_out_excluded"] == 1

    def test_reachable_now_vs_template_required_split(self, session):
        _add(session, "p_open",   is_opted_out=False, last_msg=_INSIDE_WINDOW)
        _add(session, "p_closed", is_opted_out=False, last_msg=_OUTSIDE_WINDOW)
        out = _preview(session, source=_source(**{"All Leads": ["p_open", "p_closed"]}))
        assert out["reachable_now"] == 1
        assert out["template_required"] == 1

    def test_opted_out_excluded_from_reachability_split(self, session):
        """An opted-out contact must not ALSO land in reachable_now or
        template_required — the three buckets must be mutually exclusive."""
        _add(session, "p1", is_opted_out=True, last_msg=_INSIDE_WINDOW)
        out = _preview(session, source=_source(**{"All Leads": ["p1"]}))
        assert out["opted_out_excluded"] == 1
        assert out["reachable_now"] == 0
        assert out["template_required"] == 0

    def test_no_last_msg_counts_as_template_required(self, session):
        _add(session, "p1", is_opted_out=False, last_msg="")
        out = _preview(session, source=_source(**{"All Leads": ["p1"]}))
        assert out["template_required"] == 1
        assert out["reachable_now"] == 0

    def test_null_opted_out_reaches_the_window_split_not_excluded(self, session):
        """D4 again, in preview's three-way classification: NULL is not True,
        so it must fall through to the window check, not opted_out_excluded."""
        _add(session, "p1", is_opted_out=None, last_msg=_INSIDE_WINDOW)
        out = _preview(session, source=_source(**{"All Leads": ["p1"]}))
        assert out["opted_out_excluded"] == 0
        assert out["reachable_now"] == 1

    def test_production_shape_reproduction(self, session):
        """Reproduces the Phase 8.2E.9 measured baseline at small scale:
        1 opted out, most outside the window, a few inside."""
        _add(session, "p_out", is_opted_out=True, last_msg=_INSIDE_WINDOW)
        for i in range(3):
            _add(session, f"p_open{i}", is_opted_out=False, last_msg=_INSIDE_WINDOW)
        for i in range(6):
            _add(session, f"p_closed{i}", is_opted_out=False, last_msg=_OUTSIDE_WINDOW)

        all_phones = (["p_out"] + [f"p_open{i}" for i in range(3)]
                     + [f"p_closed{i}" for i in range(6)])
        out = _preview(session, source=_source(**{"All Leads": all_phones}))

        assert out["total_audience"] == 10
        assert out["opted_out_excluded"] == 1
        assert out["reachable_now"] == 3
        assert out["template_required"] == 6

    def test_classified_but_absent_phone_ignored(self, session):
        _add(session, "p1", is_opted_out=False, last_msg=_INSIDE_WINDOW)
        out = _preview(session, source=_source(**{"All Leads": ["p1", "ghost"]}))
        assert out["total_audience"] == 1

    def test_only_this_tenants_contacts_counted(self, session):
        _add(session, "p_mine", tenant_id=T1, last_msg=_INSIDE_WINDOW)
        _add(session, "p_other", tenant_id=T2, last_msg=_INSIDE_WINDOW)
        out = _preview(session, tenant_id=T1,
                       source=_source(**{"All Leads": ["p_mine", "p_other"]}))
        assert out["total_audience"] == 1

    def test_segment_name_echoed_in_result(self, session):
        _add(session, "p1", last_msg=_INSIDE_WINDOW)
        out = res.preview(T1, "HOT Leads", session=session, state_model=_ConversationState,
                          segment_source=_source(**{"HOT Leads": ["p1"]}), now=_NOW)
        assert out["segment"] == "HOT Leads"


class TestWindowOpenAt:
    def test_recent_message_is_open(self):
        assert res._window_open_at(_INSIDE_WINDOW, _NOW) is True

    def test_old_message_is_closed(self):
        assert res._window_open_at(_OUTSIDE_WINDOW, _NOW) is False

    def test_missing_last_msg_is_closed(self):
        assert res._window_open_at("", _NOW) is False
        assert res._window_open_at(None, _NOW) is False

    def test_unparseable_last_msg_is_closed(self):
        assert res._window_open_at("not-a-date", _NOW) is False

    def test_exactly_at_boundary_is_closed(self):
        """< 86400 strictly — exactly 24h is closed, matching campaign_worker."""
        boundary = (_NOW - timedelta(hours=24)).isoformat()
        assert res._window_open_at(boundary, _NOW) is False

    def test_matches_campaign_worker_formula(self):
        """Structural guard: preview and dispatch must apply the identical
        window formula, or their counts and behaviour silently diverge."""
        worker_src = open(
            os.path.join(_ROOT, "app", "marketing", "campaign_worker.py"),
            encoding="utf-8",
        ).read()
        assert "total_seconds() < 86400" in worker_src
        assert "total_seconds() < 86400" in _resolver_code()


# ── D6.3 — no silent narrowing (ADR-025 8.2E.9-E) ────────────────────────────

class TestNoSilentNarrowing:
    """ADR-025 D6.3 / R3: contacts outside the 24-hour window MUST be
    materialised, attempted, and — absent an approved template — recorded as
    explicitly failed. They must never be quietly dropped from the audience.

    This is the property that distinguishes ADR-025 from the pre-ADR-024
    behaviour it replaced: the operator asked for a segment and must receive
    that segment, with any delivery limitation surfaced as a counted failure
    rather than an invisible omission.
    """

    def test_window_closed_contacts_are_resolved(self, session):
        _add(session, "p_closed", is_opted_out=False, last_msg=_OUTSIDE_WINDOW)
        out = _resolve(session, source=_source(**{"All Leads": ["p_closed"]}))
        assert [r["phone"] for r in out] == ["p_closed"]

    def test_contacts_with_no_last_msg_are_resolved(self, session):
        """Never-messaged contacts are the most unreachable case of all."""
        _add(session, "p_never", is_opted_out=False, last_msg="")
        out = _resolve(session, source=_source(**{"All Leads": ["p_never"]}))
        assert [r["phone"] for r in out] == ["p_never"]

    def test_mixed_audience_resolves_reachable_and_unreachable_alike(self, session):
        _add(session, "p_open", is_opted_out=False, last_msg=_INSIDE_WINDOW)
        _add(session, "p_closed", is_opted_out=False, last_msg=_OUTSIDE_WINDOW)
        out = _resolve(session,
                       source=_source(**{"All Leads": ["p_open", "p_closed"]}))
        assert [r["phone"] for r in out] == ["p_closed", "p_open"]

    def test_resolved_count_equals_preview_total_minus_opted_out(self, session):
        """The set that gets materialised must equal what preview promised —
        preview's reachable/template_required split is a DESCRIPTION of the
        audience, never a filter applied to it."""
        _add(session, "p_open", is_opted_out=False, last_msg=_INSIDE_WINDOW)
        for i in range(4):
            _add(session, f"p_closed{i}", is_opted_out=False, last_msg=_OUTSIDE_WINDOW)
        _add(session, "p_out", is_opted_out=True, last_msg=_INSIDE_WINDOW)

        phones = ["p_open", "p_out"] + [f"p_closed{i}" for i in range(4)]
        src = _source(**{"All Leads": phones})

        resolved = _resolve(session, source=src)
        breakdown = _preview(session, source=src)

        assert breakdown["total_audience"] == 6
        assert breakdown["opted_out_excluded"] == 1
        assert breakdown["reachable_now"] == 1
        assert breakdown["template_required"] == 4
        # 5 materialised = 1 reachable + 4 template-required; the 4 unreachable
        # are present, not narrowed away.
        assert len(resolved) == 5
        assert len(resolved) == (
            breakdown["total_audience"] - breakdown["opted_out_excluded"]
        )
        assert len(resolved) == (
            breakdown["reachable_now"] + breakdown["template_required"]
        )

    def test_resolver_never_reads_last_msg(self):
        """Structural: resolve() cannot narrow by reachability because it does
        not consult last_msg at all. Guards against a future 'optimisation'
        that filters unreachable contacts out at materialisation."""
        code = _resolver_code()
        resolve_src = code[code.index("def resolve("):code.index("def preview(")]
        assert "last_msg" not in resolve_src
