import os

VERIFY_TOKEN         = os.environ.get("VERIFY_TOKEN", "oxford2026")
ACCESS_TOKEN         = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID      = os.environ.get("PHONE_NUMBER_ID", "")
SHEETS_ID            = os.environ.get("SHEETS_ID", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
BROADCAST_API_KEY    = os.environ.get("BROADCAST_API_KEY", "oxford_broadcast_2026")
ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS", "{}")

WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
