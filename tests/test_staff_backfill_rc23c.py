"""Phase RC2.3C — staff identity backfill.

A migration utility, so the tests that matter most are the ones proving what it
does NOT touch. It writes exactly two columns; everything else in the system
must be byte-identical afterwards, because that is what makes rollback lossless
and what lets this run against production while the registry is still
authoritative.

The production case that drove the design is pinned here: assigned_staff
'Anju_display' matches no user anywhere, and the approved operator decision is
to leave it NULL. A backfill that guessed there would silently reassign a real
customer's lead.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23c_backfill.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-a")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (                                                # noqa: E402
    Tenant, User, ConversationState, Task, Notification, ConversationMessage,
    AuditLog,
)
from app.services import staff_backfill_service as bf                   # noqa: E402
from app.services.tenant_provisioning_service import provision_tenant   # noqa: E402

A = "t-a"
B = "t-b"
_APP = create_app()
_APP.config["TESTING"] = True


def _user(tenant, username, display=None, role="STAFF"):
    from werkzeug.security import generate_password_hash
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", ""),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=True,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _lead(tenant, phone, staff=None, name="L"):
    lead = ConversationState(
        phone=phone, name=name, tenant_id=tenant, stage="new",
        course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status="Lead", assigned_staff=staff)
    db.session.add(lead)
    db.session.commit()
    return lead


def _task(tenant, uid, staff=None):
    t = Task(tenant_id=tenant, task_uid=uid, title="T", status="OPEN",
             assigned_staff=staff, created_by="Admin")
    db.session.add(t)
    db.session.commit()
    return t


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Tenant(id=A, name="A", slug="a", status="ACTIVE", billing_exempt=True),
            Tenant(id=B, name="B", slug="b", status="ACTIVE", billing_exempt=True),
        ])
        db.session.commit()
        provision_tenant(A, commit=True)
        provision_tenant(B, commit=True)
        yield
        db.session.remove()


# ═══ Resolution rules ════════════════════════════════════════════════════════

class TestResolutionOrder:
    def test_exact_username_match(self, ctx):
        u = _user(A, "Anju")
        assert bf.resolve_user_id(A, "Anju") == (u.id, "exact username")

    def test_case_insensitive_username(self, ctx):
        u = _user(A, "Kiran")
        assert bf.resolve_user_id(A, "kiran") == (u.id, "case-insensitive username")
        assert bf.resolve_user_id(A, "KIRAN")[0] == u.id

    def test_whitespace_is_trimmed(self, ctx):
        u = _user(A, "Anju")
        assert bf.resolve_user_id(A, "  Anju  ")[0] == u.id

    def test_display_name_fallback(self, ctx):
        u = _user(A, "u_anju", display="Anju Menon")
        uid, rule = bf.resolve_user_id(A, "anju menon")
        assert (uid, rule) == (u.id, "case-insensitive display_name")

    def test_exact_username_takes_precedence_over_display_name(self, ctx):
        """Two users: one whose USERNAME is 'Kiran', one whose DISPLAY_NAME is
        'Kiran'. Rule 1 must win."""
        by_username = _user(A, "Kiran")
        _user(A, "someone_else", display="Kiran")
        uid, rule = bf.resolve_user_id(A, "Kiran")
        assert uid == by_username.id
        assert rule == "exact username"

    def test_ci_username_takes_precedence_over_display_name(self, ctx):
        by_username = _user(A, "Kiran")
        _user(A, "other", display="kiran")
        uid, rule = bf.resolve_user_id(A, "kiran")
        assert uid == by_username.id
        assert rule == "case-insensitive username"

    def test_unknown_value_is_refused(self, ctx):
        _user(A, "Anju")
        uid, reason = bf.resolve_user_id(A, "Anju_display")
        assert uid is None and "no match" in reason

    def test_no_partial_or_fuzzy_matching(self, ctx):
        _user(A, "Anju")
        for probe in ("Anj", "Anjuu", "Anju Menon", "nju", "A"):
            assert bf.resolve_user_id(A, probe)[0] is None, probe

    def test_ambiguous_display_name_is_refused(self, ctx):
        """display_name has no uniqueness constraint."""
        _user(A, "u1", display="Same Name")
        _user(A, "u2", display="Same Name")
        uid, reason = bf.resolve_user_id(A, "Same Name")
        assert uid is None and "ambiguous" in reason

    def test_blank_and_missing_inputs(self, ctx):
        _user(A, "Anju")
        for probe in (None, "", "   "):
            assert bf.resolve_user_id(A, probe)[0] is None
        assert bf.resolve_user_id(None, "Anju")[0] is None


class TestTenantIsolation:
    def test_never_resolves_across_tenants(self, ctx):
        _user(B, "Anju")
        assert bf.resolve_user_id(A, "Anju")[0] is None

    def test_same_username_in_two_tenants_resolves_separately(self, ctx):
        """Production has 'NIBU S S' as a username in four tenants."""
        ua = _user(A, "NIBU S S")
        ub = _user(B, "NIBU S S")
        assert bf.resolve_user_id(A, "NIBU S S")[0] == ua.id
        assert bf.resolve_user_id(B, "NIBU S S")[0] == ub.id
        assert ua.id != ub.id

    def test_backfill_never_assigns_another_tenants_user(self, ctx):
        _user(B, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is None

    def test_backfilling_one_tenant_leaves_the_other_untouched(self, ctx):
        _user(A, "Anju")
        _user(B, "Bob")
        la = _lead(A, "919000000001", staff="Anju")
        lb = _lead(B, "919000000002", staff="Bob")
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(la.id).assigned_user_id is not None
        assert ConversationState.query.get(lb.id).assigned_user_id is None


# ═══ Dry run vs live ═════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_writes_nothing(self, ctx):
        u = _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        t = _task(A, "u1", staff="Anju")

        report = bf.backfill_tenant(A, dry_run=True)
        db.session.expire_all()
        assert report.resolved == 2
        assert ConversationState.query.get(lead.id).assigned_user_id is None
        assert Task.query.get(t.id).assigned_user_id is None
        assert u.id  # target existed

    def test_dry_run_predicts_what_live_does(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        _lead(A, "919000000002", staff="Nobody")
        predicted = bf.backfill_tenant(A, dry_run=True)
        actual = bf.backfill_tenant(A, dry_run=False)
        assert (predicted.resolved, predicted.skipped) == (actual.resolved,
                                                           actual.skipped)

    def test_dry_run_is_the_default(self, ctx):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.backfill_tenant(A)                     # no dry_run argument
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is None

    def test_dry_run_leaves_no_pending_writes_in_the_session(self, ctx):
        """The real dry-run invariant.

        Checking only that the database is unchanged is too weak: a dry run
        that DIRTIED the session would still pass, because backfill_tenant()
        never commits on the dry path. The danger is a later commit elsewhere
        in the same session flushing those pending changes. This asserts the
        session is clean, which is the property that actually protects
        production.
        """
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        _task(A, "u1", staff="Anju")

        bf.backfill_tenant(A, dry_run=True)
        # SQLAlchemy returns an IdentitySet, which never compares equal to a
        # plain set() even when empty — assert on emptiness, not equality.
        assert len(db.session.dirty) == 0, \
            f"dry run left dirty objects: {list(db.session.dirty)}"
        assert len(db.session.new) == 0, \
            f"dry run left pending inserts: {list(db.session.new)}"

        # And a subsequent commit by unrelated code must not persist anything.
        db.session.commit()
        db.session.expire_all()
        assert all(l.assigned_user_id is None for l in ConversationState.query.all())
        assert all(t.assigned_user_id is None for t in Task.query.all())


class TestLiveMode:
    def test_live_populates_both_tables(self, ctx):
        u = _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        t = _task(A, "u1", staff="Anju")

        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id == u.id
        assert Task.query.get(t.id).assigned_user_id == u.id
        assert report.counts["conversation_state"]["resolved"] == 1
        assert report.counts["tasks"]["resolved"] == 1

    def test_case_variants_resolve_to_the_same_user(self, ctx):
        u = _user(A, "Kiran")
        a = _lead(A, "919000000001", staff="Kiran")
        b = _lead(A, "919000000002", staff="kiran")
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(a.id).assigned_user_id == u.id
        assert ConversationState.query.get(b.id).assigned_user_id == u.id

    def test_unassigned_rows_are_ignored(self, ctx):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff=None)
        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is None
        assert report.resolved == 0


class TestIdempotency:
    def test_second_live_run_writes_nothing(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        _task(A, "u1", staff="Anju")

        first = bf.backfill_tenant(A, dry_run=False)
        second = bf.backfill_tenant(A, dry_run=False)
        assert first.resolved == 2
        assert second.resolved == 0, "second run must write nothing"
        assert second.already == 2

    def test_dry_live_live_sequence(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        assert bf.backfill_tenant(A, dry_run=True).resolved == 1
        assert bf.backfill_tenant(A, dry_run=False).resolved == 1
        assert bf.backfill_tenant(A, dry_run=False).resolved == 0

    def test_already_populated_row_is_not_overwritten(self, ctx):
        anju = _user(A, "Anju")
        other = _user(A, "Kiran")
        lead = _lead(A, "919000000001", staff="Anju")
        lead.assigned_user_id = other.id          # deliberately "wrong"
        db.session.commit()

        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id == other.id
        assert report.already == 1 and report.resolved == 0
        assert anju.id != other.id


# ═══ Unresolved rows — the operator decision ═════════════════════════════════

class TestUnresolvedRows:
    def test_anju_display_is_skipped_not_guessed(self, ctx):
        """The approved operator decision for production lead id 4."""
        _user(A, "Anju")
        lead = _lead(A, "919995883671", staff="Anju_display")

        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is None
        assert report.skipped == 1
        assert report.resolved == 0

    def test_skipped_rows_are_reported_with_reason(self, ctx):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju_display")
        report = bf.backfill_tenant(A, dry_run=False)
        assert report.skipped_rows == [
            ("conversation_state", lead.id, "Anju_display", "no match in this tenant")]

    def test_one_unknown_does_not_abandon_the_tenant(self, ctx):
        _user(A, "Anju")
        good = _lead(A, "919000000001", staff="Anju")
        bad = _lead(A, "919000000002", staff="Anju_display")
        good2 = _lead(A, "919000000003", staff="Anju")

        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(good.id).assigned_user_id is not None
        assert ConversationState.query.get(good2.id).assigned_user_id is not None
        assert ConversationState.query.get(bad.id).assigned_user_id is None
        assert (report.resolved, report.skipped) == (2, 1)


# ═══ What must NOT change ════════════════════════════════════════════════════

class TestWritesNothingElse:
    def test_assigned_staff_is_never_modified(self, ctx):
        """The legacy string stays authoritative — this is what makes
        rollback lossless."""
        _user(A, "Kiran")
        a = _lead(A, "919000000001", staff="kiran")
        b = _lead(A, "919000000002", staff="Kiran")
        t = _task(A, "u1", staff="Kiran")

        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(a.id).assigned_staff == "kiran"
        assert ConversationState.query.get(b.id).assigned_staff == "Kiran"
        assert Task.query.get(t.id).assigned_staff == "Kiran"

    def test_notifications_are_untouched(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        db.session.add(Notification(tenant_id=A, recipient="Anju_display",
                                    notif_type="TASK_ASSIGNED", title="t"))
        db.session.commit()
        before = [(n.id, n.recipient) for n in Notification.query.all()]

        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert [(n.id, n.recipient) for n in Notification.query.all()] == before

    def test_conversation_message_is_untouched(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        db.session.add(ConversationMessage(tenant_id=A, phone="919000000001",
                                           direction="incoming", message="hi",
                                           staff_name="Anju"))
        db.session.commit()
        before = [(m.id, m.staff_name) for m in ConversationMessage.query.all()]

        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert [(m.id, m.staff_name) for m in ConversationMessage.query.all()] == before

    def test_audit_tables_are_untouched(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        db.session.add(AuditLog(tenant_id=A, action="LEAD_UPDATE",
                                actor="broadcast-api", target="lead:x"))
        db.session.commit()
        before = AuditLog.query.count()
        bf.backfill_tenant(A, dry_run=False)
        assert AuditLog.query.count() == before

    def test_user_rows_are_untouched(self, ctx):
        u = _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        before = (u.username, u.display_name, u.role, u.is_active)
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        after = User.query.get(u.id)
        assert (after.username, after.display_name, after.role,
                after.is_active) == before

    def test_display_name_is_never_populated(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert all(u.display_name is None for u in User.query.all())

    def test_staff_master_json_is_untouched(self, ctx):
        """The backfill must not write the legacy file.

        Stage 4B: tolerant of Stage 4C's deletion. A missing file is a
        STRONGER guarantee than an unchanged one, so comparing snapshots
        preserves the assertion across the retirement.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "app", "data", "staff_master.json")

        def snap():
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except FileNotFoundError:
                return None

        before = snap()
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        bf.backfill_tenant(A, dry_run=False)
        assert snap() == before

    def test_pipeline_fields_are_untouched(self, ctx):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        before = (lead.sales_stage_id, lead._stage, lead.pipeline_stage_id,
                  lead.lead_status)
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        after = ConversationState.query.get(lead.id)
        assert (after.sales_stage_id, after._stage, after.pipeline_stage_id,
                after.lead_status) == before


# ═══ Rollback ════════════════════════════════════════════════════════════════

class TestRollback:
    def test_clearing_the_fk_restores_the_pre_backfill_state(self, ctx):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        t = _task(A, "u1", staff="Anju")
        before = (lead.assigned_staff, t.assigned_staff)

        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is not None

        # rollback = clear the FK, nothing else
        for row in ConversationState.query.all():
            row.assigned_user_id = None
        for row in Task.query.all():
            row.assigned_user_id = None
        db.session.commit()
        db.session.expire_all()

        assert ConversationState.query.get(lead.id).assigned_user_id is None
        assert (ConversationState.query.get(lead.id).assigned_staff,
                Task.query.get(t.id).assigned_staff) == before

    def test_backfill_can_be_rerun_after_rollback(self, ctx):
        u = _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        ConversationState.query.get(lead.id).assigned_user_id = None
        db.session.commit()

        report = bf.backfill_tenant(A, dry_run=False)
        db.session.expire_all()
        assert report.resolved == 1
        assert ConversationState.query.get(lead.id).assigned_user_id == u.id


# ═══ Multi-tenant orchestration ══════════════════════════════════════════════

class TestAllTenants:
    def test_backfills_every_tenant(self, ctx):
        ua = _user(A, "Anju")
        ub = _user(B, "Bob")
        la = _lead(A, "919000000001", staff="Anju")
        lb = _lead(B, "919000000002", staff="Bob")

        bf.backfill_all_tenants(dry_run=False)
        db.session.expire_all()
        assert ConversationState.query.get(la.id).assigned_user_id == ua.id
        assert ConversationState.query.get(lb.id).assigned_user_id == ub.id

    def test_one_tenant_failing_does_not_roll_back_another(self, ctx, monkeypatch):
        _user(A, "Anju")
        _user(B, "Bob")
        la = _lead(A, "919000000001", staff="Anju")
        _lead(B, "919000000002", staff="Bob")

        real = bf.backfill_tenant

        def selective(tenant_id, dry_run=True):
            if tenant_id == B:
                raise RuntimeError("boom")
            return real(tenant_id, dry_run=dry_run)

        monkeypatch.setattr(bf, "backfill_tenant", selective)
        reports = {r.tenant_id: r for r in bf.backfill_all_tenants(dry_run=False)}
        db.session.expire_all()
        assert reports[B].errors, "failure not recorded"
        assert ConversationState.query.get(la.id).assigned_user_id is not None, \
            "tenant A's completed work was rolled back"

    def test_report_shape(self, ctx):
        _user(A, "Anju")
        _lead(A, "919000000001", staff="Anju")
        _lead(A, "919000000002", staff="Nobody")
        r = bf.backfill_tenant(A, dry_run=True)
        d = r.as_dict()
        assert d["tenant_id"] == A
        assert d["counts"]["conversation_state"]["resolved"] == 1
        assert d["counts"]["conversation_state"]["skipped"] == 1
        assert d["resolved"] == 1 and d["skipped"] == 1


# ═══ Scope containment ═══════════════════════════════════════════════════════

class TestScopeContainment:
    def test_only_two_columns_are_assigned(self):
        """AST: the only attribute this module sets on a MODEL ROW is
        assigned_user_id.

        `self.x = ...` inside TenantBackfillReport is excluded — those are the
        report's own Python fields, not database columns. Including them made
        this test fail on its own bookkeeping.
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "app", "services", "staff_backfill_service.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        assigned = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute)
                            and not (isinstance(t.value, ast.Name)
                                     and t.value.id == "self")):
                        assigned.add(t.attr)
        assert assigned <= {"assigned_user_id"}, f"writes other attrs: {assigned}"

    def test_only_the_dual_write_flag_is_read(self):
        """Updated in Phase RC2.3D: this module now owns the flag-gated mirror
        write, so it reads DUAL_WRITE by design. READ_FK gates RC2.3E and must
        still be read by nothing."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "app", "services", "staff_backfill_service.py")
        body = open(path, encoding="utf-8").read()
        assert "staff_identity_dual_write_enabled" in body,             "the dual-write gate disappeared"
        assert "staff_identity_read_fk_enabled" not in body,             "READ_FK read before RC2.3E"

    def test_registry_is_retired(self):
        """Was "load_staff_registry still exists". Stage 4C removed it along
        with the file. The backfill never depended on either — it resolves
        against the User table — so its contract is unaffected."""
        from app.routes import admin
        assert not hasattr(admin, "load_staff_registry")
        assert not hasattr(admin, "save_staff_registry")
        assert not hasattr(admin, "get_staff_json_path")

    def test_no_consumer_reads_assigned_user_id(self):
        """RC2.3C is a migration utility; reader migration is RC2.3D."""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        allowed = {"models.py", "staff_backfill_service.py"}
        for dp, _d, fs in os.walk(os.path.join(root, "app")):
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
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr == "assigned_user_id":
                        offenders.append(os.path.relpath(full, root))
        assert sorted(set(offenders)) == [], f"consumers read the FK: {set(offenders)}"
