"""Phase RC2.5.10 (P2-1, R1-A): each tenant writes only to its own spreadsheet.

WHY
---
crm_service used one global SHEETS_ID for the whole platform and addresses
rows by phone number alone -- the sheet has no tenant column. RC2.5.9 contained
that by refusing every tenant except the primary. R1-A replaces the containment
with a real boundary: the destination is resolved from the tenant's OWN
settings, so a tenant can only ever open the spreadsheet it registered, and a
tenant with no valid configuration writes nowhere.

Settings contract -- TenantSettings.settings["sheets"]:
    {"enabled": true, "spreadsheet_id": "...", "worksheet": "Leads"}

WHAT THIS SUITE PINS
--------------------
  1. tenant A opens A's spreadsheet and tenant B opens B's -- never the other's;
  2. every incomplete, disabled, malformed or missing configuration refuses,
     and a refusal makes ZERO gspread calls;
  3. a non-ACTIVE tenant refuses, whatever its settings say;
  4. no global SHEETS_ID destination survives anywhere in the writer path;
  5. resolution works inside a bare thread, which is how production calls it,
     and refuses when the app was never captured;
  6. a spreadsheet already registered to another tenant is rejected at save,
     while a tenant may always re-save its own.

gspread is replaced by a recorder: no test performs a Google Sheets call.
"""
import os
import re
import sys
import tempfile
import threading

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2510_sheets_destination.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2510-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2510-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2510-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-primary"
os.environ["SHEETS_ID"] = "GLOBAL-SHEET-MUST-NEVER-BE-USED"
os.environ["GOOGLE_CREDENTIALS"] = '{"type": "service_account", "rc2510": "dummy"}'
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, TenantSettings                                 # noqa: E402
from app.services import crm_service as crm                                   # noqa: E402
from app.services import tenant_settings_service as tss                       # noqa: E402

PRIMARY, A, B = "t-primary", "t-a", "t-b"
SHEET_PRIMARY = "sheet-primary-0001"
SHEET_A, SHEET_B = "sheet-a-0002", "sheet-b-0003"
GLOBAL_SHEET = os.environ["SHEETS_ID"]

_APP = create_app()
_APP.config["TESTING"] = True
crm.init_crm_service(_APP)

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _sheets(spreadsheet_id, worksheet="Leads", enabled=True):
    return {"enabled": enabled, "spreadsheet_id": spreadsheet_id, "worksheet": worksheet}


@pytest.fixture()
def tenants():
    """Three ACTIVE tenants; primary, A and B each with their own sheet."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name, status in ((PRIMARY, "Primary", "ACTIVE"),
                                  (A, "Tenant A", "ACTIVE"),
                                  (B, "Tenant B", "ACTIVE")):
            db.session.add(Tenant(id=tid, name=name, slug=f"rc2510-{tid}",
                                  status=status, billing_exempt=True))
        db.session.commit()
        for tid, sid in ((PRIMARY, SHEET_PRIMARY), (A, SHEET_A), (B, SHEET_B)):
            tss.set_section(tid, "sheets", _sheets(sid))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def _set_status(tenant_id, status):
    with _APP.app_context():
        t = Tenant.query.filter_by(id=tenant_id).first()
        t.status = status
        db.session.commit()


def _set_sheets(tenant_id, section):
    """Writes the raw section, bypassing nothing but the fixture's convenience."""
    with _APP.app_context():
        tss.set_section(tenant_id, "sheets", section)
        db.session.commit()


def _drop_sheets(tenant_id):
    import json as _json
    with _APP.app_context():
        row = TenantSettings.query.filter_by(tenant_id=tenant_id).first()
        blob = _json.loads(row.settings or "{}")
        blob.pop("sheets", None)
        row.settings = _json.dumps(blob)
        db.session.commit()


class Recorder:
    """Stands in for gspread + credentials. Records; performs no I/O."""

    def __init__(self):
        self.opened = []
        self.calls = []
        # Worksheet titles the fake workbook claims to contain. The writer
        # picks the configured name when present and falls back to sheet1
        # otherwise -- behaviour inherited unchanged from before R1, and
        # harmless because both live inside the tenant's OWN spreadsheet.
        self.titles = ["Leads"]

    def from_json_keyfile_dict(self, creds, scope):
        self.calls.append("credentials")
        return object()

    def authorize(self, creds):
        self.calls.append("authorize")
        return self

    def open_by_key(self, key):
        self.opened.append(key)
        self.calls.append(f"open:{key}")
        return self

    def worksheets(self):
        return [type("W", (), {"title": t})() for t in self.titles]

    def worksheet(self, title):
        self.calls.append(f"worksheet:{title}")
        return self

    @property
    def sheet1(self):
        self.calls.append("sheet1")
        return self

    def cell(self, row, col):
        return type("C", (), {"value": "seed"})()

    def col_values(self, col):
        return ["Phone", "919999000111"]

    def append_row(self, values):
        self.calls.append("append_row")

    def update_cell(self, row, col, value):
        self.calls.append("update_cell")

    def update(self, rng, values):
        self.calls.append("update")


@pytest.fixture()
def rec(monkeypatch):
    """Only gspread and the credentials builder are replaced.

    Deliberately NOT patched: json.loads. `crm.json` is the shared stdlib
    module, so patching it also rewires tenant_settings_service's own parsing
    and the settings blob silently stops resolving -- a trap this suite hit
    once already. GOOGLE_CREDENTIALS is set to real JSON above instead.
    """
    r = Recorder()
    monkeypatch.setattr(crm, "gspread", r, raising=True)
    monkeypatch.setattr(crm, "ServiceAccountCredentials", r, raising=True)
    return r


def save(tenant_id, rec_, is_new=True):
    crm.save_lead_to_sheets("919999000111", "RC2510 Lead", "hello", is_new, tenant_id)
    return rec_.opened


def status(tenant_id, rec_):
    crm.update_lead_status("919999000111", "Contacted", "note", tenant_id)
    return rec_.opened


# ── 1. each tenant reaches its own destination, and only its own ─────────────

class TestPerTenantDestination:

    def test_tenant_a_opens_only_sheet_a(self, tenants, rec):
        assert save(A, rec) == [SHEET_A]

    def test_tenant_b_opens_only_sheet_b(self, tenants, rec):
        assert save(B, rec) == [SHEET_B]

    def test_primary_opens_its_configured_sheet(self, tenants, rec):
        assert save(PRIMARY, rec) == [SHEET_PRIMARY]

    def test_a_never_reaches_b_and_b_never_reaches_a(self, tenants, rec):
        save(A, rec)
        status(A, rec)
        assert SHEET_B not in rec.opened
        rec.opened.clear()
        save(B, rec)
        status(B, rec)
        assert SHEET_A not in rec.opened

    def test_update_status_uses_the_tenant_destination(self, tenants, rec):
        assert status(A, rec) == [SHEET_A]
        assert any(c.startswith("worksheet:Leads") for c in rec.calls)

    def test_configured_worksheet_name_is_honoured(self, tenants, rec):
        rec.titles = ["Leads", "CRM Leads"]
        _set_sheets(A, _sheets(SHEET_A, worksheet="CRM Leads"))
        save(A, rec)
        assert rec.opened == [SHEET_A]
        assert "worksheet:CRM Leads" in rec.calls

    def test_absent_worksheet_falls_back_within_the_same_spreadsheet(self, tenants, rec):
        """Pre-R1 behaviour, deliberately retained: a configured worksheet the
        workbook does not contain falls back to sheet1 -- of the TENANT'S OWN
        document, so it crosses no boundary."""
        rec.titles = ["Something Else"]
        _set_sheets(A, _sheets(SHEET_A, worksheet="Leads"))
        save(A, rec)
        assert rec.opened == [SHEET_A]
        assert "sheet1" in rec.calls

    def test_three_tenants_three_destinations(self, tenants, rec):
        for tid in (PRIMARY, A, B):
            save(tid, rec)
        assert rec.opened == [SHEET_PRIMARY, SHEET_A, SHEET_B]


# ── 2. every invalid configuration refuses, touching nothing ─────────────────

class TestConfigurationRefusals:

    @pytest.mark.parametrize("tid", [None, "", "   ", "no-such-tenant"])
    def test_missing_or_unknown_tenant_refuses(self, tenants, rec, tid):
        assert save(tid, rec) == []
        assert status(tid, rec) == []

    def test_tenant_without_settings_row_refuses(self, tenants, rec):
        with _APP.app_context():
            db.session.add(Tenant(id="t-bare", name="Bare", slug="rc2510-bare",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
        assert save("t-bare", rec) == []

    def test_missing_sheets_section_refuses(self, tenants, rec):
        _drop_sheets(A)
        assert save(A, rec) == []
        assert status(A, rec) == []

    @pytest.mark.parametrize("section", [
        {},
        {"enabled": True},
        {"enabled": True, "spreadsheet_id": SHEET_A},          # no worksheet
        {"enabled": True, "worksheet": "Leads"},               # no spreadsheet
        {"enabled": True, "spreadsheet_id": "", "worksheet": "Leads"},
        {"enabled": True, "spreadsheet_id": "   ", "worksheet": "Leads"},
        {"enabled": True, "spreadsheet_id": SHEET_A, "worksheet": ""},
        {"enabled": False, "spreadsheet_id": SHEET_A, "worksheet": "Leads"},
        {"enabled": "true", "spreadsheet_id": SHEET_A, "worksheet": "Leads"},
        {"enabled": 1, "spreadsheet_id": SHEET_A, "worksheet": "Leads"},
        {"spreadsheet_id": SHEET_A, "worksheet": "Leads"},     # no enabled key
    ])
    def test_incomplete_or_disabled_configuration_refuses(self, tenants, rec, section):
        _set_sheets(A, section)
        assert save(A, rec) == []
        assert status(A, rec) == []

    def test_malformed_section_refuses(self, tenants, rec):
        """A non-dict section reaches get_section()'s own {} guard."""
        import json as _json
        with _APP.app_context():
            row = TenantSettings.query.filter_by(tenant_id=A).first()
            blob = _json.loads(row.settings or "{}")
            blob["sheets"] = "not-a-dict"
            row.settings = _json.dumps(blob)
            db.session.commit()
        assert save(A, rec) == []

    def test_refusal_builds_no_credentials_and_opens_nothing(self, tenants, rec):
        _drop_sheets(A)
        save(A, rec)
        status(A, rec)
        assert rec.calls == []


# ── 3. tenant status ─────────────────────────────────────────────────────────

class TestTenantStatus:

    @pytest.mark.parametrize("bad_status", ["SUSPENDED", "INACTIVE", "PENDING", "TRIAL"])
    def test_non_active_tenant_refuses_despite_valid_config(self, tenants, rec, bad_status):
        _set_status(A, bad_status)
        assert save(A, rec) == []
        assert status(A, rec) == []

    def test_reactivating_restores_writes(self, tenants, rec):
        _set_status(A, "SUSPENDED")
        assert save(A, rec) == []
        _set_status(A, "ACTIVE")
        assert save(A, rec) == [SHEET_A]


# ── 4. no global destination survives ────────────────────────────────────────

class TestNoGlobalFallback:

    def test_global_sheets_id_is_never_opened(self, tenants, rec):
        for tid in (PRIMARY, A, B, None, "unknown"):
            save(tid, rec)
            status(tid, rec)
        assert GLOBAL_SHEET not in rec.opened

    def test_refused_tenant_does_not_reach_the_global_sheet(self, tenants, rec):
        _drop_sheets(B)
        assert save(B, rec) == []
        assert rec.opened == []

    def test_writer_module_no_longer_imports_sheets_id(self):
        src = open(os.path.join(_ROOT, "app", "services", "crm_service.py"),
                   encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert "SHEETS_ID" not in code, "the global destination must not be reachable"

    def test_get_sheet_requires_an_explicit_spreadsheet_id(self):
        import inspect
        params = inspect.signature(crm._get_sheet).parameters
        assert "spreadsheet_id" in params
        assert params["spreadsheet_id"].default is inspect.Parameter.empty


# ── 5. thread + app-context behaviour ────────────────────────────────────────

class TestThreadResolution:

    def test_resolution_works_inside_a_bare_thread(self, tenants, rec):
        """Production calls these from threading.Thread with no context."""
        t = threading.Thread(target=crm.save_lead_to_sheets,
                             args=("919999000111", "RC2510", "hi", True, A))
        t.start()
        t.join(timeout=30)
        assert rec.opened == [SHEET_A]

    def test_thread_refusal_is_also_correct(self, tenants, rec):
        t = threading.Thread(target=crm.save_lead_to_sheets,
                             args=("919999000111", "RC2510", "hi", True, "unknown"))
        t.start()
        t.join(timeout=30)
        assert rec.opened == []

    def test_uninitialised_app_refuses_safely(self, tenants, rec, monkeypatch):
        """init_crm_service() never ran: refuse, never guess a destination."""
        monkeypatch.setattr(crm, "_app", None, raising=True)
        assert save(A, rec) == []
        assert status(PRIMARY, rec) == []
        assert rec.calls == []

    def test_resolution_never_raises(self, tenants, monkeypatch):
        """Threads swallow nothing: a raise here would be an unhandled crash."""
        monkeypatch.setattr(crm, "_app", object(), raising=True)   # no app_context
        assert crm._resolve_destination(A) is None


# ── 6. duplicate destination protection ──────────────────────────────────────

class TestDuplicateSpreadsheetGuard:

    def test_another_tenant_cannot_register_the_same_spreadsheet(self, tenants):
        with _APP.app_context():
            with pytest.raises(ValueError):
                tss.set_section(B, "sheets", _sheets(SHEET_A))
            db.session.rollback()

    def test_the_rejection_names_no_spreadsheet_and_no_tenant(self, tenants):
        with _APP.app_context():
            with pytest.raises(ValueError) as exc:
                tss.set_section(B, "sheets", _sheets(SHEET_A))
            db.session.rollback()
        msg = str(exc.value)
        assert SHEET_A not in msg and A not in msg

    def test_a_tenant_may_resave_its_own_spreadsheet(self, tenants):
        with _APP.app_context():
            tss.set_section(A, "sheets", _sheets(SHEET_A, worksheet="Leads 2"))
            db.session.commit()
            assert tss.get_section(A, "sheets")["worksheet"] == "Leads 2"

    def test_different_tenants_may_hold_different_spreadsheets(self, tenants):
        with _APP.app_context():
            tss.set_section(B, "sheets", _sheets("sheet-b-renamed"))
            db.session.commit()
            assert tss.get_section(A, "sheets")["spreadsheet_id"] == SHEET_A
            assert tss.get_section(B, "sheets")["spreadsheet_id"] == "sheet-b-renamed"

    def test_a_section_without_a_spreadsheet_id_is_not_a_duplicate(self, tenants):
        with _APP.app_context():
            tss.set_section(B, "sheets", {"enabled": False})
            db.session.commit()

    def test_a_rejected_save_leaves_the_existing_configuration_intact(self, tenants):
        with _APP.app_context():
            with pytest.raises(ValueError):
                tss.set_section(B, "sheets", _sheets(SHEET_A))
            db.session.rollback()
            assert tss.get_section(B, "sheets")["spreadsheet_id"] == SHEET_B

    def test_unrelated_sections_are_unaffected_by_the_guard(self, tenants):
        """business_profile and friends keep their previous behaviour: two
        tenants may hold identical content, and no uniqueness applies."""
        with _APP.app_context():
            tss.set_section(A, "business_profile", {"legal_name": "Same Ltd"})
            tss.set_section(B, "business_profile", {"legal_name": "Same Ltd"})
            db.session.commit()
            assert tss.get_section(B, "business_profile")["legal_name"] == "Same Ltd"
            # and the sheets sections survived untouched
            assert tss.get_section(A, "sheets")["spreadsheet_id"] == SHEET_A
            assert tss.get_section(B, "sheets")["spreadsheet_id"] == SHEET_B


# ── 7. the old global flag is untouched ──────────────────────────────────────

class TestLegacyFlagUntouched:

    def test_enable_google_sheets_is_not_read_by_the_writer(self):
        src = open(os.path.join(_ROOT, "app", "services", "crm_service.py"),
                   encoding="utf-8").read()
        assert "enable_google_sheets" not in src

    def test_provisioning_default_is_unchanged(self):
        src = open(os.path.join(_ROOT, "app", "services",
                                "tenant_provisioning_service.py"), encoding="utf-8").read()
        assert re.search(r'"enable_google_sheets":\s*False', src)
