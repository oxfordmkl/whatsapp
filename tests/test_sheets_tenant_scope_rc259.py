"""Phase RC2.5.9 (P2-1): Google Sheets writes are primary-tenant only.

WHY
---
SHEETS_ID is ONE spreadsheet for the whole platform, and crm_service addresses
rows by phone number alone -- the sheet has no tenant column to address by.
Phase 12-E2B (d980030) threaded tenant_id through every caller and widened both
writer signatures to receive it, but the bodies never read it, so ANY tenant
reaching a writer wrote into the primary tenant's document: lead names, phone
numbers, message text and status. app/bot/offer_handlers.py already declines to
write a payment reference there for exactly this reason.

RC2.5.9 is CONTAINMENT, not the per-tenant destination redesign (audited as R1,
deliberately not implemented): the one global sheet belongs to the primary
tenant, so only the primary tenant may write to it.

WHAT THIS SUITE PINS
--------------------
  1. the primary tenant still writes, unchanged;
  2. any other tenant id is refused -- including an unknown one, a blank one,
     None, and near-miss variants of the primary id;
  3. a refusal makes ZERO gspread calls: no client, no credentials, no
     open_by_key, so SHEETS_ID is never even read;
  4. a refusal writes nothing anywhere -- it is dropped, never redirected;
  5. both writers enforce it, independently;
  6. a blank PRIMARY_TENANT_ID fails CLOSED (nobody writes) rather than open;
  7. every production caller still passes tenant context positionally.

No test here touches a real spreadsheet: gspread is replaced by a recorder that
fails loudly if anything reaches it.
"""
import importlib
import os
import re
import sys

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

PRIMARY = "af8136356c9743ff95a26be174c69477"
OTHER = "229f05296b0e4b7586c5a6b23e82d7cd"          # the RC2.5.6b canary id
os.environ["PRIMARY_TENANT_ID"] = PRIMARY
os.environ.setdefault("DATABASE_URL", "sqlite:///rc259_sheets_scope.db")
os.environ.setdefault("ADMIN_KEY", "rc259-admin-key")
os.environ.setdefault("SECRET_KEY", "rc259-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc259-broadcast")
os.environ["SHEETS_ID"] = "rc259-fake-spreadsheet-id"
os.environ["GOOGLE_CREDENTIALS"] = '{"type": "service_account", "rc259": "dummy"}'
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.config as _cfg                                                    # noqa: E402
_cfg.PRIMARY_TENANT_ID = PRIMARY
_cfg.SHEETS_ID = os.environ["SHEETS_ID"]
_cfg.GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]

import app.services.crm_service as crm                                       # noqa: E402
crm = importlib.reload(crm)

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


class Recorder:
    """Stands in for the whole gspread/credentials stack.

    Every entry point appends to `calls`. Nothing here performs I/O, so a test
    that reaches it is proving a write was ATTEMPTED, never performing one.
    """

    def __init__(self):
        self.calls = []
        self.cells = []

    # -- credentials + client ------------------------------------------------
    def from_json_keyfile_dict(self, creds, scope):
        self.calls.append(("credentials", None))
        return object()

    def authorize(self, creds):
        self.calls.append(("authorize", None))
        return self

    def open_by_key(self, key):
        self.calls.append(("open_by_key", key))
        return self

    # -- workbook ------------------------------------------------------------
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

    # -- worksheet -----------------------------------------------------------
    def cell(self, row, col):
        self.calls.append(("cell", (row, col)))
        return type("C", (), {"value": "seed" if (row, col) == (1, 1) else ""})()

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
    r = Recorder()
    monkeypatch.setattr(crm, "gspread", r, raising=True)
    monkeypatch.setattr(crm, "ServiceAccountCredentials", r, raising=True)
    monkeypatch.setattr(crm.json, "loads", lambda s: {"rc259": "dummy"}, raising=True)
    return r


def save(tenant_id, rec_, is_new=True):
    crm.save_lead_to_sheets("919999000111", "RC259 Lead", "hello", is_new, tenant_id)
    return rec_.calls


def status(tenant_id, rec_):
    crm.update_lead_status("919999000111", "Contacted", "note", tenant_id)
    return rec_.calls


# ── 1. the primary tenant is unaffected ──────────────────────────────────────

class TestPrimaryStillWrites:

    def test_save_lead_reaches_the_sheet_for_the_primary_tenant(self, rec):
        save(PRIMARY, rec)
        assert ("open_by_key", _cfg.SHEETS_ID) in rec.calls
        assert any(c[0] == "append_row" for c in rec.calls)

    def test_update_status_reaches_the_sheet_for_the_primary_tenant(self, rec):
        status(PRIMARY, rec)
        assert ("open_by_key", _cfg.SHEETS_ID) in rec.calls
        assert any(c[0] == "update_cell" for c in rec.calls)

    def test_existing_lead_update_path_is_unchanged(self, rec):
        save(PRIMARY, rec, is_new=False)
        assert any(c[0] == "update_cell" for c in rec.calls)
        assert not any(c[0] == "append_row" for c in rec.calls)


# ── 2-4. everyone else is refused, before any gspread call ───────────────────

REFUSED = [OTHER, None, "", "   ", "not-a-tenant", "unknown-tenant-id",
           PRIMARY.upper(), PRIMARY[:-1], PRIMARY + "x", " " + PRIMARY + " x"]


class TestNonPrimaryRefused:

    @pytest.mark.parametrize("tid", REFUSED)
    def test_save_lead_is_refused(self, rec, tid):
        assert save(tid, rec) == [], f"gspread was touched for {tid!r}"

    @pytest.mark.parametrize("tid", REFUSED)
    def test_update_status_is_refused(self, rec, tid):
        assert status(tid, rec) == [], f"gspread was touched for {tid!r}"

    def test_refusal_never_opens_the_global_spreadsheet(self, rec):
        save(OTHER, rec)
        status(OTHER, rec)
        assert not any(c[0] == "open_by_key" for c in rec.calls)
        assert _cfg.SHEETS_ID not in [c[1] for c in rec.calls]

    def test_refusal_builds_no_credentials(self, rec):
        save(OTHER, rec)
        assert not any(c[0] in ("credentials", "authorize") for c in rec.calls)

    def test_refusal_writes_nothing_anywhere(self, rec):
        save(OTHER, rec)
        status(OTHER, rec)
        assert rec.cells == []
        assert not any(c[0] in ("append_row", "update_cell", "update") for c in rec.calls)

    def test_tenant_id_omitted_entirely_is_refused(self, rec):
        crm.save_lead_to_sheets("919999000111", "RC259", "hi", True)
        crm.update_lead_status("919999000111", "Contacted")
        assert rec.calls == []

    def test_refusal_is_silent_toward_the_caller(self, rec):
        """A refused write must not raise: these run in bare threads whose
        exceptions nobody catches."""
        assert save(OTHER, rec) == []
        assert status(OTHER, rec) == []


# ── 5. the guard helper itself ───────────────────────────────────────────────

class TestGuardHelper:

    @pytest.mark.parametrize("tid", REFUSED)
    def test_helper_refuses(self, tid):
        assert crm._tenant_may_write(tid) is False

    def test_helper_allows_the_primary(self):
        assert crm._tenant_may_write(PRIMARY) is True

    def test_helper_tolerates_surrounding_whitespace_on_the_primary_id(self):
        assert crm._tenant_may_write(f"  {PRIMARY}  ") is True

    def test_blank_primary_tenant_id_fails_closed(self, monkeypatch, rec):
        """A misconfigured deployment must write NOWHERE, not everywhere."""
        monkeypatch.setattr(crm, "PRIMARY_TENANT_ID", "", raising=True)
        assert crm._tenant_may_write(PRIMARY) is False
        assert crm._tenant_may_write("") is False
        assert save(PRIMARY, rec) == []
        assert status(PRIMARY, rec) == []


# ── 6. both writers, and the callers, keep tenant context ────────────────────

class TestCallSitesPreserveTenantContext:

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
        """The guard is only as good as the context the callers hand it: each
        threaded call must still carry a tenant positionally."""
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        threaded = re.findall(
            r"target=%s,\s*(?:args|kwargs)=[^\n]*\n(?:[^\n]*\n){0,2}" % needle, src)
        assert threaded, f"{rel}: no threaded {needle} call found"
        assert any("tenant_id" in block for block in threaded), rel

    def test_the_writer_module_has_no_other_sheet_entry_point(self):
        """Nothing may reach the workbook except through the two guarded
        writers -- otherwise the boundary has a side door."""
        src = open(os.path.join(_ROOT, "app", "services", "crm_service.py"),
                   encoding="utf-8").read()
        body = src.split("def _get_sheet")[1]
        callers = re.findall(r"^def (\w+)|_get_sheet\(\)", body, re.M)
        publics = re.findall(r"^def ([a-z]\w+)", src, re.M)
        assert set(publics) <= {"_get_sheet", "save_lead_to_sheets",
                                "update_lead_status"}, publics
