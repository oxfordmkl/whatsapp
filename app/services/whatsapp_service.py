import requests
import threading
from app.config import ACCESS_TOKEN, PHONE_NUMBER_ID, WHATSAPP_API_URL
from app.bot.constants import BUTTON_PRESETS

token_status = "unknown"

def _get_waba_credentials(tenant_id: str = None) -> tuple[str, str]:
    from app.models import Tenant
    from app.services.log_service import _get_default_tenant_id
    from app.services.encryption_service import decrypt_token
    
    if not tenant_id:
        tenant_id = _get_default_tenant_id()
        
    tenant = Tenant.query.get(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found.")
        
    if tenant.waba_phone_number_id and tenant.waba_access_token_encrypted:
        token = decrypt_token(tenant.waba_access_token_encrypted)
        if not token:
            raise ValueError(f"Failed to decrypt WABA token for tenant {tenant_id}.")
        return tenant.waba_phone_number_id, token
        
    # Backward compatibility for primary tenant
    if tenant_id == _get_default_tenant_id():
        if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
            raise ValueError("Primary tenant missing global WABA configuration.")
        return PHONE_NUMBER_ID, ACCESS_TOKEN
        
    raise ValueError(f"Tenant {tenant_id} has no WABA credentials configured.")

def _wa_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
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

def send_text(to: str, text: str, tenant_id: str = None) -> requests.Response:
    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(url, headers=_wa_headers(token), json=payload)
    print(f"📤 text → {to}  HTTP {r.status_code}")
    return r

def send_interactive(to: str, body: str, preset: str, tenant_id: str = None) -> requests.Response:
    """Send message with up to 3 reply buttons from a named preset."""
    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"

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
    r = requests.post(url, headers=_wa_headers(token), json=payload)
    print(f"📤 interactive[{preset}] → {to}  HTTP {r.status_code}")
    if r.status_code != 200:
        print("⚠️  Interactive failed — falling back to plain text")
        return send_text(to, body, tenant_id)
    return r

def send_reply(to: str, body: str, preset: str | None, tenant_id: str = None) -> requests.Response:
    """Send text only or interactive depending on preset."""
    if not preset:
        return send_text(to, body, tenant_id)
    return send_interactive(to, body, preset, tenant_id)

def send_template(to: str, template: str, lang: str = "en", components: list | None = None, tenant_id: str = None) -> requests.Response:
    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}},
    }
    if components:
        payload["template"]["components"] = components
    return requests.post(url, headers=_wa_headers(token), json=payload)

def send_automation(to: str, text: str, name: str = "Student", tenant_id: str = None) -> requests.Response:
    """
    Phase 11-D3B2: Automation-only Interceptor
    Checks the 24-hour window. If closed, queues the text and sends a template fallback.
    Phase 12-C2: Now resolves tenant_id dynamically before PendingMessage INSERT.
    """
    from app.models import ConversationState, PendingMessage
    from app.extensions import db
    from datetime import datetime
    # Phase 12-C2: Resolve tenant_id before any INSERT
    from app.services.log_service import _get_default_tenant_id

    if tenant_id is None:
        tenant_id = _get_default_tenant_id()

    state = ConversationState.query.filter_by(phone=to, tenant_id=tenant_id).first()
    
    # Check 24-hour window
    window_open = False
    if state and state.last_msg:
        try:
            last_dt = datetime.fromisoformat(state.last_msg)
            if (datetime.utcnow() - last_dt).total_seconds() < 86400:
                window_open = True
        except ValueError:
            pass

    if window_open:
        return send_text(to, text, tenant_id)
    else:
        # Window closed: Queue the original message and send the template
        pending = PendingMessage(phone=to, text=text, tenant_id=tenant_id)
        db.session.add(pending)
        db.session.commit()
        
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": name}]
        }]
        
        response = send_template(to, "oxford_re_engagement_v1", lang="en", components=components, tenant_id=tenant_id)
        if response.status_code != 200:
            # If the template fails, rollback the pending message so it isn't orphaned
            db.session.delete(pending)
            db.session.commit()
            print(f"⚠️  Template fallback failed for {to}: HTTP {response.status_code} - {response.text}")
        else:
            print(f"🛑 Interceptor active: Template fallback sent to {to}")
            
        return response

