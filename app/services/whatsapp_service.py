import requests
import threading
from app.config import ACCESS_TOKEN, PHONE_NUMBER_ID, WHATSAPP_API_URL
from app.bot.constants import BUTTON_PRESETS

token_status = "unknown"

def _wa_headers() -> dict:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

def validate_token():
    global token_status
    r = requests.get(
        f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    )
    if r.status_code == 200:
        token_status = "valid"
        print("✅ WhatsApp token valid")
    else:
        token_status = "invalid"
        print(f"❌ Token invalid: {r.status_code} — {r.text}")

threading.Thread(target=validate_token, daemon=True).start()

def send_text(to: str, text: str) -> requests.Response:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)
    print(f"📤 text → {to}  HTTP {r.status_code}")
    return r

def send_interactive(to: str, body: str, preset: str) -> requests.Response:
    """Send message with up to 3 reply buttons from a named preset."""
    buttons_data = BUTTON_PRESETS.get(preset, BUTTON_PRESETS["COURSE"])
    buttons = [{"type": "reply", "reply": b} for b in buttons_data]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": buttons},
        },
    }
    r = requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)
    print(f"📤 interactive[{preset}] → {to}  HTTP {r.status_code}")
    if r.status_code != 200:
        print("⚠️  Interactive failed — falling back to plain text")
        return send_text(to, body)
    return r

def send_reply(to: str, body: str, preset: str | None) -> requests.Response:
    """Send text only or interactive depending on preset."""
    if not preset:
        return send_text(to, body)
    return send_interactive(to, body, preset)

def send_template(to: str, template: str, lang: str = "en", components: list | None = None) -> requests.Response:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}},
    }
    if components:
        payload["template"]["components"] = components
    return requests.post(WHATSAPP_API_URL, headers=_wa_headers(), json=payload)
