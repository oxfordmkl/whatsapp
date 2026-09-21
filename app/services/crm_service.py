import json
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from app.config import GOOGLE_CREDENTIALS_JSON

# ── Phase RC2.5.10 (P2-1, R1-A): tenant-scoped destinations ──────────────────
#
# HISTORY. SHEETS_ID used to be ONE spreadsheet for the whole platform, and
# these writers address rows by phone number alone -- the sheet carries no
# tenant column to address by. Phase 12-E2B threaded tenant_id through every
# caller and widened both signatures to receive it, but the bodies never read
# it, so any tenant reaching a writer wrote into the primary tenant's document.
# RC2.5.9 contained that by refusing everyone except the primary tenant.
#
# NOW. The destination is resolved per tenant from the tenant's own settings,
# so a tenant can only ever open the spreadsheet it registered. SHEETS_ID is no
# longer read here at all: there is no global destination and no fallback of
# any kind. A tenant with no valid configuration writes NOWHERE.
#
# Settings contract -- TenantSettings.settings["sheets"]:
#     {"enabled": true, "spreadsheet_id": "...", "worksheet": "Leads"}
# Every field is required. `enabled` must be exactly True, and both strings
# must be non-blank after stripping. Anything else is refused: this is the
# security boundary, so it reads strictly rather than generously.
#
# Resolution runs under the application object captured at boot, because these
# writers execute in bare threads with no context of their own --
# tenant_settings_service and Tenant.query both need one. That mirrors
# log_service.log_lead_event_in_thread(), which is spawned alongside these very
# calls in app/bot/router.py.
_app = None


def init_crm_service(app):
    """Capture the Flask app for in-thread destination resolution.

    Called once from create_app(), beside init_followup_service(). Without it
    _app stays None and every write is refused -- a CRM that writes nowhere,
    never a CRM that writes to the wrong tenant.
    """
    global _app
    _app = app


def _refuse(fn_name: str, tenant_id, reason: str) -> None:
    # print(), not logging: this module has no logger, and RC2.5.10 is scoped
    # to the tenant boundary rather than to the file's logging style. The
    # tenant id is an opaque identifier; the spreadsheet id is NEVER printed.
    print(f"⛔ CRM: {fn_name} refused — {reason} (tenant_id={tenant_id!r})")


def _resolve_destination(tenant_id):
    """Return (spreadsheet_id, worksheet) for a tenant, or None to refuse.

    Fails closed on every path, including its own errors: a refusal drops the
    write, and there is deliberately no value it can fall back to. Never
    raises -- the callers are threads whose exceptions nobody catches.
    """
    if not tenant_id or not str(tenant_id).strip():
        return None
    if _app is None:
        # init_crm_service() never ran: no context, so no tenant can be
        # verified. Refuse rather than guess a destination.
        return None

    try:
        with _app.app_context():
            from app.models import Tenant
            from app.services import tenant_settings_service

            tenant = Tenant.query.filter_by(id=str(tenant_id).strip()).first()
            if tenant is None:
                return None
            # Only a live tenant may sync. SUSPENDED, PENDING, TRIAL and
            # anything else are refused -- the lead still lands in the CRM
            # database, which is tenant-scoped; only the mirror stops.
            if (getattr(tenant, "status", "") or "").strip().upper() != "ACTIVE":
                return None

            section = tenant_settings_service.get_section(tenant.id, "sheets")
            # get_section() is fail-OPEN by contract (RC2.5.2): a DB error, a
            # malformed blob and an absent section all return {}. Treating an
            # empty/partial section as a refusal is what turns that fail-open
            # read into a fail-closed write.
            if not isinstance(section, dict):
                return None
            if section.get("enabled") is not True:
                return None
            spreadsheet_id = str(section.get("spreadsheet_id") or "").strip()
            worksheet = str(section.get("worksheet") or "").strip()
            if not spreadsheet_id or not worksheet:
                return None
            return spreadsheet_id, worksheet
    except Exception:
        # Including "no application context" and any DB failure.
        return None


def _get_sheet(spreadsheet_id: str):
    """Open ONE tenant's spreadsheet. The id comes only from _resolve_destination()."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(spreadsheet_id)

def save_lead_to_sheets(phone: str, name: str, message: str, is_new: bool, tenant_id: str = None):
    # RC2.5.10 (P2-1): first statement, so a tenant without a valid registered
    # destination never reaches the credentials or any gspread call.
    destination = _resolve_destination(tenant_id)
    if destination is None:
        _refuse("save_lead_to_sheets", tenant_id, "no enabled Sheets destination")
        return
    spreadsheet_id, worksheet = destination
    try:
        if GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet(spreadsheet_id)
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet(worksheet) if worksheet in titles else wb.sheet1

        if not ws.cell(1, 1).value:
            ws.update("A1:G1", [
                ["Timestamp", "Name", "Phone", "Last Message", "Status", "Source", "Notes"]
            ])

        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        note  = f"[{ts}] {message}"

        if is_new:
            ws.append_row([ts, name, phone, message, "New Lead", "WhatsApp", note])
            print(f"✅ CRM: new lead saved — {name}")
        else:
            phones = ws.col_values(3)
            if phone in phones:
                row = phones.index(phone) + 1
                ws.update_cell(row, 1, ts)
                ws.update_cell(row, 4, message)
                existing = ws.cell(row, 7).value or ""
                ws.update_cell(row, 7, f"{existing}\n{note}" if existing else note)
                print(f"✅ CRM: lead updated — {name}")
    except Exception as e:
        print(f"⚠️  Sheets save error: {e}")

def update_lead_status(phone: str, status: str, append_note: str = "", tenant_id: str = None):
    # RC2.5.10 (P2-1): same resolution, first statement. This writer is the
    # sharper edge of the two -- it locates a row by phone number alone, so it
    # must never be pointed at a spreadsheet the tenant does not own. With
    # per-tenant destinations the phone lookup can only ever match a row inside
    # that tenant's own document.
    destination = _resolve_destination(tenant_id)
    if destination is None:
        _refuse("update_lead_status", tenant_id, "no enabled Sheets destination")
        return
    spreadsheet_id, worksheet = destination
    try:
        if GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet(spreadsheet_id)
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet(worksheet) if worksheet in titles else wb.sheet1
        phones = ws.col_values(3)
        if phone in phones:
            row = phones.index(phone) + 1
            ws.update_cell(row, 5, status)
            if append_note:
                existing = ws.cell(row, 7).value or ""
                ws.update_cell(row, 7, f"{existing}\n{append_note}" if existing else append_note)
            print(f"✅ CRM: status → {status} ({phone})")
    except Exception as e:
        print(f"⚠️  Sheets status error: {e}")
