import json
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from app.config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON

def _get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_json = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEETS_ID)

def save_lead_to_sheets(phone: str, name: str, message: str, is_new: bool):
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

def update_lead_status(phone: str, status: str, append_note: str = ""):
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
