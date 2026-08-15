"""Phase RC2.3E-1 Batch 5a — the reader-migration contract, pinned.

Batch 5 discovery found NO defect and NO row that moves. Of the 24 pure
readers left after Batches 1a/1b/2/3/4:

    12  already key on normalize_staff_name()  -> correct, regime-independent
     8  render the name or pass it to a notification -> nothing to migrate TO
     3  are SQL "unassigned" filters           -> BLOCKED, see below
     1  list_tasks, a raw exact match          -> no application caller

So this phase changes no production code. It writes down the contract those
readers rely on, so a later phase cannot quietly break it — the same reason
the RC2.2/RC2.3 tripwires exist.

WHY is_unassigned() DOES NOT COVER THE THREE SQL SITES
------------------------------------------------------
My Batch 1 plan said is_unassigned() would "cover most" of Batch 5. That was
wrong and this file records why. is_unassigned(key) takes a PYTHON value and
returns a bool. All three sites are SQL WHERE clauses:

    or_(ConversationState.assigned_staff.is_(None),
        ConversationState.assigned_staff == '')

A bool cannot go into a SQLAlchemy filter. The helper's only SQL-returning
functions are owner_column() and owner_filter(), and neither expresses
"unassigned". Closing those three needs a NEW helper (unassigned_filter),
which is dormant infrastructure requiring its own approval — exactly as
owner_filter did in RC2.3E-0. Until then the three stay name-based, which is
correct: production has ZERO rows where the name and FK predicates disagree.

WHAT WOULD BREAK IF SOMEONE "TIDIED" THIS
-----------------------------------------
Each test below fails on a specific realistic edit, named in its docstring.
None of them assert "the code looks like X" for its own sake.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b5a_contract.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b5a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b5a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b5a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "app", "routes", "admin.py")
TASKSVC = os.path.join(ROOT, "app", "services", "task_service.py")
PIPESVC = os.path.join(ROOT, "app", "services", "sales_pipeline_service.py")
HELPER = os.path.join(ROOT, "app", "services", "staff_identity_service.py")

# ── the four buckets, as established by Batch 5 discovery ────────────────────

# Bucketed by MECHANISM, not by intent. Both mechanisms are case-insensitive
# and therefore regime-independent, but they are different code, and a test
# that conflates them fails on correct code — as the first version of this
# file did on crm_staff_allocation_check.
NORMALIZED = [                      # normalize_staff_name() in Python
    "calculate_action_center", "calculate_admission_analytics",
    "calculate_crm_health",
    "calculate_staff_performance", "calculate_staff_performance_fixed",
    "calculate_workload_scoring", "crm_staff_workload",
    "crm_staff_allocation",
]

CASE_INSENSITIVE_SQL = [            # lower(trim(col)) == <name>, in SQL
    "crm_staff_allocation_check",
]

# RC2.3E-3C reclassified calculate_operations OUT of NORMALIZED. It still
# normalizes for display, but it now also restricts its lead row-set by
# OWNERSHIP for a SESSION STAFF actor, which is a different contract: these
# readers are ALLOWED — required — to consult the regime, because that is how
# owner_filter() bridges name and FK. The assertion below is the inversion of
# test_does_not_consult_the_flag, not its deletion: it fails if the ownership
# filter is ever dropped, which is exactly the boundary Batch 5a was guarding
# in the other direction.
OWNERSHIP_FILTERED = [
    "calculate_operations",
    # RC2.3E-9 moved calculate_intelligence here for the same reason: it now
    # narrows its PRIORITY QUEUE (Module 4) by ownership for a SESSION STAFF
    # actor. Note the narrower contract — only that module is filtered; the
    # shared `leads` collection every other module aggregates from is
    # deliberately untouched, because crm_staff_dashboard derives the viewer's
    # RANK from the leaderboard built out of it.
    "calculate_intelligence",
    # RC2.3E-10A moved calculate_automation_intelligence here on the same
    # narrow contract: it filters ONLY the four customer-record lists
    # (unassigned_hot / stalled_admissions / recovery_queue /
    # recommendations). `aging` keeps its own unfiltered loop over the whole
    # tenant lead set, and `productivity` is derived from `events`, so
    # crm_staff_dashboard - which consumes productivity alone - is untouched.
    "calculate_automation_intelligence",
]

DISPLAY_ONLY = [
    "crm_lead_move_stage", "crm_lead_send", "crm_course_admissions",
    "crm_reassignment_preview", "get_all_tasks", "calculate_lead_health",
]

SQL_UNASSIGNED = [
    "crm_unassigned_leads", "crm_auto_assign_preview",
    "crm_staff_allocation_detail",
]

MIGRATED = {
    "crm_staff_management": "B3", "_build_leads_query": "B1a",
    "crm_my_leads": "B1a", "crm_staff_dashboard": "B1b",
    "crm_staff_performance_detail": "B4", "calculate_revenue_analytics": "B4",
}


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _src(path, name):
    """Function source with its docstring stripped.

    Docstrings mention assigned_staff, normalize_staff_name and the flag all
    over this codebase; matching raw text has produced a false positive in
    six separate phases. Every assertion here reads executable code only.
    """
    fn = next(n for n in ast.walk(_tree(path))
              if isinstance(n, ast.FunctionDef) and n.name == name)
    m = ast.parse(ast.unparse(fn)).body[0]
    if (m.body and isinstance(m.body[0], ast.Expr)
            and isinstance(m.body[0].value, ast.Constant)
            and isinstance(m.body[0].value.value, str)):
        m.body.pop(0)
        if not m.body:
            m.body.append(ast.Pass())
    return ast.unparse(m)


# ═══ the 12 normalized aggregations ══════════════════════════════════════════

class TestNormalizedAggregations:

    @pytest.mark.parametrize("name", NORMALIZED)
    def test_still_normalizes(self, name):
        """Breaks if someone replaces normalize_staff_name(x) with a raw
        string key — the exact defect Batch 4 fixed in two other functions,
        where production showed 5 staff rows for 3 people."""
        assert "normalize_staff_name" in _src(ADMIN, name)

    @pytest.mark.parametrize("name", NORMALIZED)
    def test_does_not_consult_the_flag(self, name):
        """These bucket for DISPLAY, not ownership. If one starts reading the
        flag its numbers would move at the flip while the eleven beside it
        would not — a dashboard describing two different worlds."""
        src = _src(ADMIN, name)
        for token in ("read_fk_enabled", "STAFF_IDENTITY_READ_FK",
                      "owner_column", "owner_filter"):
            assert token not in src, f"{name} now depends on the regime"

    @pytest.mark.parametrize("name", OWNERSHIP_FILTERED)
    def test_ownership_filtered_readers_still_filter(self, name):
        """The inversion of test_does_not_consult_the_flag. RC2.3E-3C moved
        calculate_operations here; if the filter is later removed the reader
        silently goes back to leaking every colleague's customer."""
        src = _src(ADMIN, name)
        assert "owner_filter" in src, f"{name} no longer filters by ownership"

    @pytest.mark.parametrize("name", OWNERSHIP_FILTERED)
    def test_ownership_filtered_readers_stay_tenant_wide_for_admin(self, name):
        """The filter must be conditional on the actor, not unconditional."""
        src = _src(ADMIN, name)
        assert "STAFF" in src, f"{name} filters without checking the role"

    def test_intelligence_does_not_filter_the_shared_leads_collection(self):
        """RC2.3E-9's narrower contract, and the reason it could ship at all.

        calculate_intelligence() is ALSO called by crm_staff_dashboard, which
        never renders the priority queue but does derive the viewer's rank from
        the leaderboard. Filtering the shared `leads` query would collapse that
        leaderboard to one person and every staff member would rank #1. Only
        Module 4 may narrow.
        """
        src = _src(ADMIN, "calculate_intelligence")
        line = [l for l in src.splitlines()
                if l.strip().startswith("leads = tenant_query(")]
        assert line, "the shared leads query moved or was renamed"
        assert "owner_filter" not in line[0], \
            "ownership was applied to the SHARED leads collection"

    def test_automation_aging_loop_is_not_ownership_filtered(self):
        """RC2.3E-10A's narrower contract.

        calculate_automation_intelligence() filters only the customer-record
        loop. `aging` counts the whole tenant from its own earlier loop over
        `leads`; if ownership reached it, a STAFF viewer's aging buckets would
        silently become per-staff counts while the ADMIN's stayed tenant-wide.
        """
        src = _src(ADMIN, "calculate_automation_intelligence")
        assert "for lead in leads:" in src, (
            "the aging loop no longer iterates the full tenant lead set")
        assert "for lead in _cust_leads:" in src, (
            "the customer-record loop is no longer the filtered one")

    def test_the_buckets_are_still_the_expected_size(self):
        """A new aggregation must be classified deliberately, not absorbed.
        Totals are unchanged: RC2.3E-3C MOVED one entry, it did not add one."""
        assert len(NORMALIZED) == 8
        assert len(set(NORMALIZED)) == 8
        assert len(CASE_INSENSITIVE_SQL) == 1
        assert len(OWNERSHIP_FILTERED) == 3
        assert "calculate_operations" not in NORMALIZED
        assert "calculate_intelligence" not in NORMALIZED
        assert "calculate_automation_intelligence" not in NORMALIZED
        assert (len(NORMALIZED) + len(CASE_INSENSITIVE_SQL)
                + len(OWNERSHIP_FILTERED)) == 12


class TestCaseInsensitiveSqlReaders:
    """Same guarantee as the bucket above, reached in SQL rather than Python."""

    @pytest.mark.parametrize("name", CASE_INSENSITIVE_SQL)
    def test_comparison_stays_case_insensitive(self, name):
        """Breaks if someone drops lower()/trim() for a bare == — the Batch 3
        defect. Production holds both 'Kiran' and 'kiran', so an exact match
        silently undercounts."""
        assert "func.lower(func.trim(ConversationState.assigned_staff))" in \
            _src(ADMIN, name)

    @pytest.mark.parametrize("name", CASE_INSENSITIVE_SQL)
    def test_does_not_consult_the_flag(self, name):
        src = _src(ADMIN, name)
        for token in ("read_fk_enabled", "STAFF_IDENTITY_READ_FK",
                      "owner_column", "owner_filter"):
            assert token not in src


# ═══ the display-only readers ════════════════════════════════════════════════

class TestDisplayOnlyReaders:

    @pytest.mark.parametrize("name", DISPLAY_ONLY)
    def test_never_uses_the_name_as_an_ownership_predicate(self, name):
        """These render the name or hand it to a notification. If one grows a
        `lower(trim(assigned_staff)) == <someone>` it has become an ownership
        filter and belongs in Batch 1a's contract, not here."""
        src = _src(ADMIN, name)
        assert "func.lower(func.trim(ConversationState.assigned_staff))" not in src

    def test_notification_recipient_is_still_a_name(self):
        """Notification.recipient has NO foreign key, so there is nothing to
        migrate _notify_stage_change and delete_task TO. This test is the
        record of that blocker; it starts failing when a FK is added, which is
        the moment those two become migratable."""
        from app.models import Notification
        cols = {c.name for c in Notification.__table__.columns}
        assert "recipient" in cols
        assert "recipient_user_id" not in cols, \
            "Notification gained a user FK — _notify_stage_change and " \
            "delete_task can now be migrated; update this contract"

    def test_pipeline_notification_reads_the_name(self):
        assert "lead.assigned_staff" in _src(PIPESVC, "_notify_stage_change")


# ═══ the three SQL unassigned filters ════════════════════════════════════════

class TestSqlUnassignedFilters:

    @pytest.mark.parametrize("name", SQL_UNASSIGNED)
    def test_still_filters_on_the_name_column(self, name):
        """Blocked, not forgotten. See the module docstring: the helper has no
        SQL-side unassigned predicate, and production has ZERO rows where the
        name and FK predicates disagree, so there is nothing to gain by
        hand-rolling one here."""
        src = _src(ADMIN, name)
        assert "assigned_staff" in src

    def test_the_helper_still_has_no_sql_unassigned_predicate(self):
        """THE TRIPWIRE FOR BATCH 5b. is_unassigned() takes a Python key and
        returns a bool — it cannot go in a WHERE clause. When someone adds
        unassigned_filter(model), this fails and the three sites above become
        migratable in one small phase."""
        names = {n.name for n in _tree(HELPER).body
                 if isinstance(n, ast.FunctionDef)}
        assert "is_unassigned" in names
        assert "unassigned_filter" not in names, \
            "a SQL-side unassigned predicate exists now — wire the three " \
            "filters in SQL_UNASSIGNED and update this test"

    def test_is_unassigned_returns_a_bool_not_a_predicate(self):
        """Pins the reason those three cannot use it."""
        from app.services import staff_identity_service as sis
        assert sis.is_unassigned(None) is True
        assert sis.is_unassigned("") is True
        assert sis.is_unassigned("Unassigned") is True
        assert sis.is_unassigned("Anju") is False


# ═══ list_tasks ══════════════════════════════════════════════════════════════

class TestListTasks:

    def test_exact_match_is_unchanged(self):
        assert "Task.assigned_staff == assigned_staff" in _src(TASKSVC, "list_tasks")

    def test_has_no_application_caller(self):
        """WHY THE EXACT MATCH IS LEFT ALONE: nothing in app/ calls it, so the
        case-sensitivity is latent, not live. If an application caller appears
        this fails, and the exact match must be fixed before that caller
        ships — a task assigned as 'kiran' would not match a filter of
        'Kiran'.
        """
        callers = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py"):
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and \
                            ast.unparse(n.func).endswith("list_tasks"):
                        callers.append(os.path.relpath(full, ROOT))
        assert callers == [], \
            f"list_tasks now has application callers {callers}; its " \
            f"case-sensitive exact match must be fixed first"


# ═══ programme-wide invariants ═══════════════════════════════════════════════

class TestProgrammeInvariants:

    def test_no_remaining_reader_depends_on_the_regime(self):
        """The claim Batch 5 rests on: OFF and ON are identical for every
        unmigrated reader BY CONSTRUCTION, not by luck."""
        offenders = []
        for name in (NORMALIZED + CASE_INSENSITIVE_SQL + DISPLAY_ONLY
                     + SQL_UNASSIGNED):
            src = _src(ADMIN, name)
            if any(t in src for t in ("read_fk_enabled", "STAFF_IDENTITY_READ_FK",
                                      "owner_column", "owner_filter",
                                      "staff_keys", "display_for_key")):
                offenders.append(name)
        assert offenders == [], offenders

    def test_every_remaining_reader_is_tenant_scoped(self):
        """No unscoped Model.query anywhere in the remaining readers."""
        offenders = []
        for name in (NORMALIZED + CASE_INSENSITIVE_SQL + DISPLAY_ONLY
                     + SQL_UNASSIGNED):
            src = _src(ADMIN, name)
            if ("ConversationState.query" in src or "Task.query" in src) and \
                    "tenant_query" not in src and "tenant_filter" not in src:
                offenders.append(name)
        assert offenders == [], offenders

    def test_the_migrated_readers_stay_migrated(self):
        """Batches 1a/1b/3/4 must not regress while Batch 5 is in flight."""
        assert "owner_filter" in _src(ADMIN, "crm_staff_management")
        assert "owner_filter" in _src(ADMIN, "_build_leads_query")
        assert "owner_filter" in _src(ADMIN, "crm_my_leads")
        assert "_display_key" in _src(ADMIN, "crm_staff_dashboard")
        assert "_by_norm" in _src(ADMIN, "crm_staff_performance_detail")
        assert "normalize_staff_name(staff)" in _src(ADMIN, "calculate_revenue_analytics")
        assert "owner_filter" in _src(PIPESVC, "_staff_ownership_clause")

    def test_h3_write_paths_remain_eight_of_eight(self):
        n = sum("resolve_assignment" in _src(ADMIN, f) for f in (
            "crm_lead_new", "crm_lead_update", "crm_unassigned_assign",
            "crm_auto_assign_confirm", "crm_reassignment_confirm",
            "crm_leads_import"))
        with open(TASKSVC, encoding="utf-8") as fh:
            svc = fh.read()
        assert n == 6
        assert svc.count("resolve_assignment") >= 2

    def test_task_service_is_the_only_direct_fk_reader_outside_the_helper(self):
        """Batch 2's deliberate exception, and the boundary it crossed.

        Every other consumer reaches the FK through staff_identity_service so
        its behaviour follows the flag. Authorization does not, because the
        name comparison it replaced is unsafe in BOTH regimes. A THIRD direct
        reader would be a new architectural decision, not a continuation.
        """
        allowed = {"models.py", "staff_backfill_service.py",
                   "staff_identity_service.py", "task_service.py"}
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f in allowed:
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Attribute) and n.attr == "assigned_user_id":
                        offenders.append(os.path.relpath(full, ROOT))
        assert sorted(set(offenders)) == [], f"new direct FK reader: {set(offenders)}"

    def test_helper_is_still_the_only_flag_reader(self):
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f in ("flags.py",
                                                  "staff_identity_service.py"):
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, (ast.Module, ast.FunctionDef,
                                      ast.AsyncFunctionDef, ast.ClassDef)):
                        if (n.body and isinstance(n.body[0], ast.Expr)
                                and isinstance(n.body[0].value, ast.Constant)
                                and isinstance(n.body[0].value.value, str)):
                            n.body.pop(0)
                            if not n.body:
                                n.body.append(ast.Pass())
                code = ast.unparse(ast.fix_missing_locations(tree))
                if "STAFF_IDENTITY_READ_FK" in code or "read_fk_enabled" in code:
                    offenders.append(os.path.relpath(full, ROOT))
        assert sorted(set(offenders)) == [], f"new flag reader: {set(offenders)}"

    def test_flag_is_still_off_by_default(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before

    def test_h4_progress_is_counted(self):
        """HONEST RECORD, updated as H4 is repaid — which is what the previous
        version of this test explicitly asked for.

        H4 is 14 route-level sites, not the "two idioms" my Batch 1 discovery
        claimed. Batch 1a closed three; H4-a closed four more. Seven remain,
        all of them H4-b scope: routes where _tid feeds ONLY tenant_query(),
        whose SUPER_ADMIN branch ignores the argument — so migrating them
        changes no behaviour and is pure consistency work.

        Asserted as an exact set, not a count: a count cannot tell an intended
        migration from an accidental one.
        """
        tree = _tree(ADMIN)
        GET = "getattr(current_user, 'tenant_id'"
        remaining = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and GET in ast.unparse(n)
                     and n.name not in ("_actor_tenant_id", "check_billing_status")}
        for closed in ("crm_leads", "crm_my_leads", "crm_staff_dashboard",
                       "crm_lead_update", "crm_lead_send", "crm_lead_detail",
                       "crm_staff_performance_detail"):
            assert closed not in remaining, f"{closed} regressed to the H4 idiom"
        # H4-b closed the last seven, so the set is now empty. H4 total was
        # FOURTEEN route-level sites: Batch 1a 3, H4-a 4, H4-b 7.
        assert remaining == set(), f"H4 sites remain: {sorted(remaining)}"

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out

    def test_this_phase_changed_no_production_code(self):
        """Batch 5a was a CONTRACT phase: its commit touched no app/ file.

        Asserted against the COMMIT that introduced this file, not against the
        working tree. The first version checked `git status -- app/`, which
        made it fail during H4-a merely because a LATER, separately approved
        phase was editing admin.py — a test that breaks whenever anyone else
        works is measuring the wrong thing. What is durably true is what this
        phase itself shipped.
        """
        import subprocess
        rel = os.path.relpath(__file__, ROOT).replace(os.sep, "/")
        sha = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "-1", "--", rel],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not sha:
            pytest.skip("Batch 5a is not committed yet")
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=ROOT, capture_output=True, text=True).stdout.split()
        assert files == [rel], f"the Batch 5a commit touched: {files}"
