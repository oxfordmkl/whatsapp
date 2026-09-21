"""Phase RC2.5.9 (P2-1): Google Sheets writes never leave the tenant.

WHY
---
SHEETS_ID was ONE spreadsheet for the whole platform, and crm_service
addresses rows by phone number alone -- the sheet has no tenant column to
address by. Phase 12-E2B (d980030) threaded tenant_id through every caller and
widened both writer signatures to receive it, but the bodies never read it, so
ANY tenant reaching a writer wrote into the primary tenant's document: lead
names, phone numbers, message text and status. app/bot/offer_handlers.py
already declines to write a payment reference there for exactly this reason.

RC2.5.9 contained that by allowing only the primary tenant to write.
RC2.5.10 (R1-A) replaced the containment with real per-tenant destinations:
each tenant writes to the spreadsheet registered in its OWN settings, and a
tenant with no valid configuration writes nowhere.

WHAT THIS SUITE STILL PINS -- the invariants that survive both phases
--------------------------------------------------------------------
  1. a tenant with no Sheets configuration is refused, on both writers;
  2. `tenant_id=None`, blank and unknown ids are refused;
  3. a refusal makes ZERO gspread calls: no client, no credentials, no
     open_by_key -- nothing is attempted and nothing is redirected;
  4. a refusal never raises: these run in bare threads whose exceptions
     nobody catches;
  5. the writers expose no second route to a workbook.

The per-tenant DESTINATION behaviour introduced by R1-A -- tenant A opening
A's sheet, status gating, the duplicate-spreadsheet guard -- is pinned by
tests/test_sheets_tenant_destination_rc2510.py, which owns that contract. The
primary-only expectations this file used to carry were superseded there and
were removed deliberately, not because they broke.
"""
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PRIMARY = "af8136356c9743ff95a26be174c69477"
OTHER = "229f05296b0e4b7586c5a6b23e82d7cd"          # the RC2.5.6b canary id
_DB = os.path.join(tempfile.gettempdir(), "rc259_sheets_scope.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["PRIMARY_TENANT_ID"] = PRIMARY
os.environ.setdefault("ADMIN_KEY", "rc259-admin-key")
os.environ.setdefault("SECRET_KEY", "rc259-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc259-broadcast")
os.environ["SHEETS_ID"] = "rc259-global-sheet-must-never-be-used"
os.environ["GOOGLE_CREDENTIALS"] = '{"type": "service_account", "rc259": "dummy"}'
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant                                                 # noqa: E402
from app.services import crm_service as crm                                   # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True
crm.init_crm_service(_APP)

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


@pytest.fixture()
def tenants():
    """Two ACTIVE tenants, NEITHER holding a Sheets configuration.

    That is the state every tenant is in until an operator registers a
    spreadsheet -- and the state in which nothing may be written.
    """
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((PRIMARY, "Primary"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=name, slug=f"rc259-{tid[:8]}",
                                  status="ACTIVE", billing_exempt=True))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


class Recorder:
    """Stands in for the whole gspread/credentials stack.

    Every entry point appends to `calls`. Nothing here performs I/O, so a test
    that reaches it is proving a write was ATTEMPTED, never performing one.
    """

    def __init__(self):
        self.calls = []
        self.cells = []

    def from_json_keyfile_dict(self, creds, scope):
        self.calls.append(("credentials", None))
        return object()

    def authorize(self, creds):
        self.calls.append(("authorize", None))
        return self

    def open_by_key(self, key):
        self.calls.append(("open_by_key", key))
        return self

    def worksheets(self):
        self.calls.append(("worksheets", None))
        return [self]

    def worksheet(self, title):
        self.calls.append(("worksheet", title))
        return self

    @property
    def sheet1(self):
        self.calls.append(("sheet1", None))
        return self

    @property
    def title(self):
        return "Leads"

    def cell(self, row, col):
        self.calls.append(("cell", (row, col)))
        return type("C", (), {"value": "seed"})()

    def col_values(self, col):
        self.calls.append(("col_values", col))
        return ["Phone", "919999000111"]

    def append_row(self, values):
        self.calls.append(("append_row", values))

    def update_cell(self, row, col, value):
        self.calls.append(("update_cell", (row, col, value)))
        self.cells.append((row, col, value))

    def update(self, rng, values):
        self.calls.append(("update", rng))


@pytest.fixture()
def rec(monkeypatch):
    # json.loads is deliberately NOT patched: crm.json is the shared stdlib
    # module, and patching it also rewires the settings service's parsing.
    r = Recorder()
    monkeypatch.setattr(crm, "gspread", r, raising=True)
    monkeypatch.setattr(crm, "ServiceAccountCredentials", r, raising=True)
    return r


def save(tenant_id, rec_, is_new=True):
    crm.save_lead_to_sheets("919999000111", "RC259 Lead", "hello", is_new, tenant_id)
    return rec_.calls


def status(tenant_id, rec_):
    crm.update_lead_status("919999000111", "Contacted", "note", tenant_id)
    return rec_.calls


# ── 1. an unconfigured tenant writes nowhere ─────────────────────────────────

REFUSED = [PRIMARY, OTHER, None, "", "   ", "not-a-tenant", "unknown-tenant-id",
           PRIMARY.upper(), PRIMARY[:-1], PRIMARY + "x"]


class TestUnconfiguredTenantRefused:

    @pytest.mark.parametrize("tid", REFUSED)
    def test_save_lead_is_refused(self, tenants, rec, tid):
        assert save(tid, rec) == [], f"gspread was touched for {tid!r}"

    @pytest.mark.parametrize("tid", REFUSED)
    def test_update_status_is_refused(self, tenants, rec, tid):
        assert status(tid, rec) == [], f"gspread was touched for {tid!r}"

    def test_even_the_primary_tenant_is_refused_without_configuration(self, tenants, rec):
        """R1-A removed the primary's privileged position: it is a tenant like
        any other, and must register a destination to write."""
        assert save(PRIMARY, rec) == []
        assert status(PRIMARY, rec) == []

    def test_refusal_never_opens_any_spreadsheet(self, tenants, rec):
        save(OTHER, rec)
        status(OTHER, rec)
        assert not any(c[0] == "open_by_key" for c in rec.calls)

    def test_refusal_builds_no_credentials(self, tenants, rec):
        save(OTHER, rec)
        assert not any(c[0] in ("credentials", "authorize") for c in rec.calls)

    def test_refusal_writes_nothing_anywhere(self, tenants, rec):
        save(OTHER, rec)
        status(OTHER, rec)
        assert rec.cells == []
        assert not any(c[0] in ("append_row", "update_cell", "update") for c in rec.calls)

    def test_tenant_id_omitted_entirely_is_refused(self, tenants, rec):
        crm.save_lead_to_sheets("919999000111", "RC259", "hi", True)
        crm.update_lead_status("919999000111", "Contacted")
        assert rec.calls == []

    def test_refusal_is_silent_toward_the_caller(self, tenants, rec):
        """A refused write must not raise: these run in bare threads whose
        exceptions nobody catches."""
        assert save(OTHER, rec) == []
        assert status(OTHER, rec) == []


# ── 2. the global destination is gone for good ───────────────────────────────

class TestNoGlobalDestination:

    def test_the_writer_module_does_not_reference_sheets_id(self):
        src = open(os.path.join(_ROOT, "app", "services", "crm_service.py"),
                   encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert "SHEETS_ID" not in code

    def test_the_configured_global_sheet_is_never_opened(self, tenants, rec):
        for tid in REFUSED:
            save(tid, rec)
            status(tid, rec)
        assert os.environ["SHEETS_ID"] not in [c[1] for c in rec.calls]


# ── 3. surface ───────────────────────────────────────────────────────────────

class TestWriterSurface:

    def test_both_writers_accept_a_tenant_id_parameter(self):
        import inspect
        for fn in (crm.save_lead_to_sheets, crm.update_lead_status):
            assert "tenant_id" in inspect.signature(fn).parameters, fn.__name__

    @pytest.mark.parametrize("rel,needle", [
        ("app/routes/webhook.py", r"save_lead_to_sheets"),
        ("app/services/followup_service.py", r"update_lead_status"),
        ("app/bot/router.py", r"update_lead_status"),
        ("app/bot/cta_handlers.py", r"update_lead_status"),
        ("app/bot/booking_handlers.py", r"update_lead_status"),
    ])
    def test_every_caller_passes_a_tenant_argument(self, rel, needle):
        """The boundary is only as good as the context the callers hand it:
        each threaded call must still carry a tenant positionally."""
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        threaded = re.findall(
            r"target=%s,\s*(?:args|kwargs)=[^\n]*\n(?:[^\n]*\n){0,2}" % needle, src)
        assert threaded, f"{rel}: no threaded {needle} call found"
        assert any("tenant_id" in block for block in threaded), rel

    def test_the_module_exposes_no_other_sheet_entry_point(self):
        """Nothing may reach a workbook except through the two guarded
        writers -- otherwise the boundary has a side door."""
        src = open(os.path.join(_ROOT, "app", "services", "crm_service.py"),
                   encoding="utf-8").read()
        publics = re.findall(r"^def ([a-z]\w+)", src, re.M)
        assert set(publics) <= {"init_crm_service", "_get_sheet",
                                "save_lead_to_sheets", "update_lead_status"}, publics
