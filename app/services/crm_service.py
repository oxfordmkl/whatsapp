import json
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from app.config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON, PRIMARY_TENANT_ID


# ── Phase RC2.5.9 (P2-1): containment, not tenant scoping ────────────────────
#
# SHEETS_ID is ONE spreadsheet for the whole platform and the two writers below
# address rows by phone number alone -- no tenant column exists in the sheet to
# address by. Phase 12-E2B threaded tenant_id through every caller and widened
# these signatures to receive it, but the bodies never read it, so any tenant
# reaching a writer wrote into the primary tenant's document: names, phone
# numbers and message text. app/bot/offer_handlers.py already refuses to write
# a payment reference here for exactly that reason.
#
# Until a per-tenant destination exists (audited as R1, NOT implemented here),
# the boundary is drawn where the data actually goes: the one global sheet
# belongs to the primary tenant, so only the primary tenant may write to it.
# Everyone else is refused BEFORE any credential is built or any gspread call
# is made, and there is deliberately no fallback -- a refused write is dropped,
# never redirected.
#
# Fails CLOSED by construction: a blank PRIMARY_TENANT_ID matches nothing, so a
# misconfigured deployment writes nowhere rather than writing everywhere. The
# comparison is a plain string match against config -- these functions run in
# bare threads with no application context, so no query is available here and
# none is needed: an unknown id simply is not the primary id.
def _tenant_may_write(tenant_id) -> bool:
    primary = (PRIMARY_TENANT_ID or "").strip()
    return bool(primary) and str(tenant_id or "").strip() == primary


def _refuse(fn_name: str, tenant_id) -> None:
    # print(), not logging: this module has no logger and RC2.5.9 is scoped to
    # the tenant boundary, not to the file's logging style. The tenant id is an
    # opaque identifier, never a credential or customer datum.
    print(f"⛔ CRM: {fn_name} refused — Google Sheets is primary-tenant only "
          f"(tenant_id={tenant_id!r})")


def _get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEETS_ID)

def save_lead_to_sheets(phone: str, name: str, message: str, is_new: bool, tenant_id: str = None):
    # RC2.5.9 (P2-1): first statement in the function, so a refused tenant
    # cannot reach _get_sheet(), the service-account credentials or SHEETS_ID.
    if not _tenant_may_write(tenant_id):
        _refuse("save_lead_to_sheets", tenant_id)
        return
    try:
        if not SHEETS_ID or GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet()
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet("Leads") if "Leads" in titles else wb.sheet1

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
    # RC2.5.9 (P2-1): same guard, first statement. This writer is the sharper
    # edge of the two -- it locates a row by phone number alone, so without the
    # guard a second tenant sharing a number would overwrite the primary
    # tenant's status and notes in place.
    if not _tenant_may_write(tenant_id):
        _refuse("update_lead_status", tenant_id)
        return
    try:
        if not SHEETS_ID or GOOGLE_CREDENTIALS_JSON == "{}":
            return
        wb  = _get_sheet()
        titles = [s.title for s in wb.worksheets()]
        ws  = wb.worksheet("Leads") if "Leads" in titles else wb.sheet1
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
