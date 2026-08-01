"""Phase 14D — tenant provisioning automation.

Registration created a Tenant and an ADMIN User and nothing else, so a new
tenant signed up for a product it could not use: no sales pipeline meant every
lead entered with sales_stage_id NULL and the pipeline dashboard was
permanently empty, and TenantSettings — whose own docstring claims it is
"created on tenant registration" — was never instantiated at all.

The load-bearing tests here are the ATOMICITY ones. Idempotency and defaults
are easy to get right and easy to verify; "the whole registration rolls back if
provisioning fails" is the claim that is both hardest to believe and worst to
get wrong, because the failure leaves a real customer with an account that
looks fine until they open the pipeline. Those tests inject a failure and
assert the Tenant and User are gone.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_14d_provisioning.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (                                                # noqa: E402
    Tenant, User, ConversationState, PipelineDefinition, PipelineStage,
    TenantSettings, LEAD_STATUSES, SALES_PIPELINE_KEY,
)
from app.services import tenant_provisioning_service as tps             # noqa: E402
from app.services.tenant_provisioning_service import (                  # noqa: E402
    provision_tenant, is_provisioned, backfill_all_tenants,
    DEFAULT_SETTINGS, ProvisioningReport,
)

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

T1 = "t-14d-one"
T2 = "t-14d-two"


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Tenant(id=T1, name="One", slug="one", status="ACTIVE"),
            Tenant(id=T2, name="Two", slug="two", status="ACTIVE"),
        ])
        db.session.commit()
        yield
        db.session.remove()


def sales_pipeline(tenant_id):
    return (PipelineDefinition.query
            .filter_by(tenant_id=tenant_id, internal_key=SALES_PIPELINE_KEY)
            .first())


def stages(tenant_id):
    p = sales_pipeline(tenant_id)
    if p is None:
        return []
    return (PipelineStage.query.filter_by(pipeline_id=p.id)
            .order_by(PipelineStage.order_index).all())


# ── What provisioning creates ────────────────────────────────────────────────

class TestProvisionsEverything:
    def test_creates_the_sales_pipeline(self, ctx):
        provision_tenant(T1, commit=True)
        p = sales_pipeline(T1)
        assert p is not None
        assert p.internal_key == SALES_PIPELINE_KEY
        assert p.is_active is True

    def test_does_not_steal_the_default_pipeline_flag(self, ctx):
        """The AI funnel may already hold is_default; moving it silently is a
        behavioural change provisioning has no mandate to make."""
        provision_tenant(T1, commit=True)
        assert sales_pipeline(T1).is_default is False

    def test_creates_every_stage(self, ctx):
        provision_tenant(T1, commit=True)
        names = [s.display_name for s in stages(T1)]
        assert names == list(LEAD_STATUSES)

    def test_stage_metadata_is_correct(self, ctx):
        provision_tenant(T1, commit=True)
        by_name = {s.display_name: s for s in stages(T1)}
        assert by_name["Lead"].is_entry is True
        assert by_name["Lead"].order_index == 0
        assert by_name["Enrolled"].stage_category == "won"
        assert by_name["Enrolled"].is_terminal is True
        assert by_name["Lost"].stage_category == "lost"
        assert by_name["Not Interested"].is_terminal is True
        assert by_name["Interested"].stage_category == "open"
        assert by_name["Interested"].is_terminal is False

    def test_exactly_one_entry_stage(self, ctx):
        provision_tenant(T1, commit=True)
        assert sum(1 for s in stages(T1) if s.is_entry) == 1

    def test_creates_tenant_settings(self, ctx):
        provision_tenant(T1, commit=True)
        row = TenantSettings.query.filter_by(tenant_id=T1).first()
        assert row is not None
        assert json.loads(row.settings)

    def test_settings_contain_ai_defaults(self, ctx):
        provision_tenant(T1, commit=True)
        s = json.loads(TenantSettings.query.filter_by(tenant_id=T1).first().settings)
        assert s["ai"]["enabled"] is True
        assert s["ai"]["persona_name"] is None, "must not inherit another tenant's persona"
        assert s["ai"]["prompt_override"] is None

    def test_settings_contain_notification_defaults(self, ctx):
        provision_tenant(T1, commit=True)
        s = json.loads(TenantSettings.query.filter_by(tenant_id=T1).first().settings)
        n = s["notifications"]
        assert n["stage_change"] is True and n["lead_assigned"] is True
        assert n["campaign_complete"] is False, "bulk notifications default off"

    def test_settings_contain_branding_locale_hours_features(self, ctx):
        provision_tenant(T1, commit=True)
        s = json.loads(TenantSettings.query.filter_by(tenant_id=T1).first().settings)
        for key in ("branding", "locale", "working_hours", "features"):
            assert key in s, key

    def test_settings_are_json_round_trippable(self, ctx):
        provision_tenant(T1, commit=True)
        raw = TenantSettings.query.filter_by(tenant_id=T1).first().settings
        assert json.loads(raw) == DEFAULT_SETTINGS

    def test_is_provisioned_reports_true_afterwards(self, ctx):
        assert is_provisioned(T1) is False
        provision_tenant(T1, commit=True)
        assert is_provisioned(T1) is True

    def test_a_provisioned_tenant_can_actually_take_a_lead(self, ctx):
        """The user-visible point of the whole phase: before this, a new
        tenant's leads entered with sales_stage_id NULL."""
        provision_tenant(T1, commit=True)
        lead = ConversationState(
            phone="919000000001", name="X", tenant_id=T1, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead")
        db.session.add(lead)
        db.session.commit()
        assert lead.sales_stage_id is not None
        assert db.session.get(PipelineStage, lead.sales_stage_id).display_name == "Lead"

    def test_unprovisioned_tenant_lead_is_unlinked(self, ctx):
        """Documents the defect being fixed."""
        lead = ConversationState(
            phone="919000000002", name="X", tenant_id=T2, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead")
        db.session.add(lead)
        db.session.commit()
        assert lead.sales_stage_id is None


# ── Idempotency ──────────────────────────────────────────────────────────────

class TestIdempotent:
    def test_running_twice_creates_nothing_the_second_time(self, ctx):
        first = provision_tenant(T1, commit=True)
        second = provision_tenant(T1, commit=True)
        assert first.pipeline_created == 1 and first.stages_created == len(LEAD_STATUSES)
        assert second.pipeline_created == 0
        assert second.stages_created == 0
        assert second.settings_created == 0
        assert second.changed is False

    def test_running_twice_does_not_duplicate_rows(self, ctx):
        provision_tenant(T1, commit=True)
        provision_tenant(T1, commit=True)
        provision_tenant(T1, commit=True)
        assert PipelineDefinition.query.filter_by(
            tenant_id=T1, internal_key=SALES_PIPELINE_KEY).count() == 1
        assert len(stages(T1)) == len(LEAD_STATUSES)
        assert TenantSettings.query.filter_by(tenant_id=T1).count() == 1

    def test_fills_only_the_missing_stage(self, ctx):
        provision_tenant(T1, commit=True)
        victim = stages(T1)[5]
        db.session.delete(victim)
        db.session.commit()
        assert len(stages(T1)) == len(LEAD_STATUSES) - 1

        report = provision_tenant(T1, commit=True)
        assert report.stages_created == 1
        assert report.stages_reused == len(LEAD_STATUSES) - 1
        assert len(stages(T1)) == len(LEAD_STATUSES)

    def test_existing_settings_are_never_overwritten(self, ctx):
        """A tenant that customised its settings must not have provisioning
        silently reinstate a default it deliberately changed."""
        db.session.add(TenantSettings(tenant_id=T1,
                                      settings=json.dumps({"ai": {"enabled": False}})))
        db.session.commit()

        report = provision_tenant(T1, commit=True)
        assert report.settings_created == 0 and report.settings_reused == 1
        kept = json.loads(TenantSettings.query.filter_by(tenant_id=T1).first().settings)
        assert kept == {"ai": {"enabled": False}}

    def test_existing_pipeline_is_reused_not_replaced(self, ctx):
        provision_tenant(T1, commit=True)
        pid = sales_pipeline(T1).id
        provision_tenant(T1, commit=True)
        assert sales_pipeline(T1).id == pid


class TestDoesNotTouchOtherTenants:
    def test_provisioning_one_tenant_leaves_the_other_alone(self, ctx):
        provision_tenant(T1, commit=True)
        assert sales_pipeline(T2) is None
        assert TenantSettings.query.filter_by(tenant_id=T2).count() == 0

    def test_each_tenant_gets_its_own_stage_rows(self, ctx):
        provision_tenant(T1, commit=True)
        provision_tenant(T2, commit=True)
        a = {s.id for s in stages(T1)}
        b = {s.id for s in stages(T2)}
        assert a and b and not (a & b)

    def test_existing_lead_data_is_untouched(self, ctx):
        lead = ConversationState(
            phone="919000000003", name="Existing", tenant_id=T1, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead", lead_score=42)
        db.session.add(lead)
        db.session.commit()
        before = (lead.name, lead.lead_score, lead._lead_status, lead._stage)

        provision_tenant(T1, commit=True)
        db.session.expire_all()
        after_lead = ConversationState.query.filter_by(phone="919000000003").first()
        assert (after_lead.name, after_lead.lead_score,
                after_lead._lead_status, after_lead._stage) == before


# ── Atomicity — the load-bearing guarantee ───────────────────────────────────

class TestProvisioningIsTransactional:
    def test_provision_tenant_does_not_commit_by_default(self, ctx):
        """This is what lets registration be atomic."""
        provision_tenant(T1)                      # no commit
        db.session.rollback()
        assert sales_pipeline(T1) is None
        assert TenantSettings.query.filter_by(tenant_id=T1).count() == 0

    def test_a_failure_leaves_nothing_behind(self, ctx, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("settings step failed")
        monkeypatch.setattr(tps, "_provision_settings", boom)

        with pytest.raises(RuntimeError):
            provision_tenant(T1, commit=True)
        db.session.rollback()

        assert sales_pipeline(T1) is None, "pipeline survived a failed provision"
        assert stages(T1) == []

    def test_it_raises_rather_than_returning_a_broken_tenant(self, ctx, monkeypatch):
        """Silent partial success is the worst outcome — the operator would not
        discover it until they opened an empty pipeline."""
        monkeypatch.setattr(tps, "_provision_stages",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            provision_tenant(T1)
        db.session.rollback()

    def test_missing_tenant_id_is_refused(self, ctx):
        for bad in (None, ""):
            with pytest.raises(ValueError):
                provision_tenant(bad)


class TestRegistrationIsAtomic:
    """End-to-end through the real /register route."""

    FORM = {
        "business_name": "New Institute",
        "admin_name": "Priya",
        "email": "priya@newinstitute.test",
        "phone": "919000000099",
        "password": "S3cure!Password",
        "industry": "Education",
    }

    @staticmethod
    def _register(form=None, follow=False):
        return _APP.test_client().post("/register", data=form or dict(
            TestRegistrationIsAtomic.FORM), follow_redirects=follow)

    def test_registration_provisions_the_new_tenant(self, ctx, monkeypatch):
        monkeypatch.setattr("app.services.email_service.email_service."
                            "send_verification_email", lambda **kw: True)
        self._register()
        tenant = Tenant.query.filter_by(slug="new-institute").first()
        assert tenant is not None
        assert is_provisioned(tenant.id) is True
        assert len(stages(tenant.id)) == len(LEAD_STATUSES)

    def test_registration_creates_tenant_user_pipeline_and_settings(self, ctx, monkeypatch):
        monkeypatch.setattr("app.services.email_service.email_service."
                            "send_verification_email", lambda **kw: True)
        self._register()
        tenant = Tenant.query.filter_by(slug="new-institute").first()
        assert User.query.filter_by(tenant_id=tenant.id, role="ADMIN").count() == 1
        assert sales_pipeline(tenant.id) is not None
        assert TenantSettings.query.filter_by(tenant_id=tenant.id).count() == 1

    def test_provisioning_failure_rolls_back_the_WHOLE_registration(self, ctx, monkeypatch):
        """The guarantee that matters. Before this phase a failure here was
        impossible because provisioning did not exist; now it must never leave
        a Tenant or User behind."""
        before_tenants = Tenant.query.count()
        before_users = User.query.count()

        monkeypatch.setattr(
            "app.services.tenant_provisioning_service.provision_tenant",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("provisioning down")))

        self._register()

        assert Tenant.query.count() == before_tenants, "orphan tenant created"
        assert User.query.count() == before_users, "orphan user created"
        assert Tenant.query.filter_by(slug="new-institute").first() is None
        assert User.query.filter_by(email=self.FORM["email"]).first() is None

    def test_a_failed_registration_can_be_retried(self, ctx, monkeypatch):
        """The rollback must leave no unique-constraint debris behind."""
        monkeypatch.setattr(
            "app.services.tenant_provisioning_service.provision_tenant",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("transient")))
        self._register()
        monkeypatch.undo()
        monkeypatch.setattr("app.services.email_service.email_service."
                            "send_verification_email", lambda **kw: True)

        self._register()
        assert Tenant.query.filter_by(slug="new-institute").first() is not None

    def test_email_failure_does_not_roll_back_a_good_registration(self, ctx, monkeypatch):
        """Email is dispatched AFTER the commit and is best-effort — a bounced
        verification must not destroy a valid account."""
        monkeypatch.setattr("app.services.email_service.email_service."
                            "send_verification_email",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("smtp")))
        self._register()
        tenant = Tenant.query.filter_by(slug="new-institute").first()
        assert tenant is not None and is_provisioned(tenant.id)


# ── Backfill ─────────────────────────────────────────────────────────────────

class TestBackfill:
    def test_dry_run_writes_nothing(self, ctx):
        reports = backfill_all_tenants(dry_run=True)
        assert len(reports) == 2
        assert all(r.changed for r in reports)
        assert sales_pipeline(T1) is None
        assert TenantSettings.query.count() == 0

    def test_dry_run_predicts_what_live_will_do(self, ctx):
        predicted = {r.tenant_id: r.stages_created
                     for r in backfill_all_tenants(dry_run=True)}
        actual = {r.tenant_id: r.stages_created
                  for r in backfill_all_tenants(dry_run=False)}
        assert predicted == actual

    def test_live_provisions_every_tenant(self, ctx):
        backfill_all_tenants(dry_run=False)
        for t in (T1, T2):
            assert is_provisioned(t) is True

    def test_live_run_is_idempotent(self, ctx):
        backfill_all_tenants(dry_run=False)
        second = backfill_all_tenants(dry_run=False)
        assert all(not r.changed for r in second)
        assert PipelineDefinition.query.filter_by(
            internal_key=SALES_PIPELINE_KEY).count() == 2

    def test_already_provisioned_tenant_is_reported_as_reused(self, ctx):
        provision_tenant(T1, commit=True)
        reports = {r.tenant_id: r for r in backfill_all_tenants(dry_run=True)}
        assert reports[T1].changed is False
        assert reports[T2].changed is True

    def test_one_tenant_failing_does_not_stop_the_others(self, ctx, monkeypatch):
        real = tps._provision_settings

        def selective(tenant_id, report):
            if tenant_id == T1:
                raise RuntimeError("boom")
            return real(tenant_id, report)

        monkeypatch.setattr(tps, "_provision_settings", selective)
        reports = {r.tenant_id: r for r in backfill_all_tenants(dry_run=False)}
        assert reports[T1].errors, "failure not recorded"
        assert is_provisioned(T2) is True, "sibling tenant was not provisioned"

    def test_a_failed_tenant_is_rolled_back_not_half_written(self, ctx, monkeypatch):
        real = tps._provision_settings

        def selective(tenant_id, report):
            if tenant_id == T1:
                raise RuntimeError("boom")
            return real(tenant_id, report)

        monkeypatch.setattr(tps, "_provision_settings", selective)
        backfill_all_tenants(dry_run=False)
        db.session.expire_all()
        assert sales_pipeline(T1) is None, "T1 left half-provisioned"


class TestReportShape:
    def test_report_serialises(self, ctx):
        r = provision_tenant(T1, commit=True)
        d = r.as_dict()
        assert d["tenant_id"] == T1 and d["changed"] is True
        json.dumps(d)

    def test_empty_report_is_not_changed(self):
        assert ProvisioningReport("x").changed is False
