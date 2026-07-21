import logging
import requests
import threading
from app.config import ACCESS_TOKEN, PHONE_NUMBER_ID, WHATSAPP_API_URL
from app.bot.constants import BUTTON_PRESETS

logger = logging.getLogger(__name__)

token_status = "unknown"

def _get_waba_credentials(tenant_id: str = None) -> tuple[str, str]:
    from app.models import Tenant
    from app.services.log_service import resolve_tenant_id
    from app.services.encryption_service import decrypt_token

    # Phase 0 Sprint 2: explicit tenant resolution (config-first). Previously
    # fell back to _get_default_tenant_id() (Tenant.query.first()), which
    # resolves to an arbitrary tenant in multi-tenant production.
    tenant_id = resolve_tenant_id(tenant_id)

    tenant = Tenant.query.get(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found.")

    if tenant.waba_phone_number_id and tenant.waba_access_token_encrypted:
        token = decrypt_token(tenant.waba_access_token_encrypted)
        if not token:
            raise ValueError(f"Failed to decrypt WABA token for tenant {tenant_id}.")
        return tenant.waba_phone_number_id, token

    # Backward compatibility for the primary tenant (global env credentials)
    if tenant_id == resolve_tenant_id(None):
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
        logger.info("✅ WhatsApp token valid")
    else:
        token_status = "invalid"
        logger.error(f"❌ Token invalid: {r.status_code} — {r.text}")

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
    logger.info(f"📤 text → {to}  HTTP {r.status_code}")
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
    from app.perf import mark as _perf_mark
    _perf_mark("send_start")
    r = requests.post(url, headers=_wa_headers(token), json=payload)
    _perf_mark("meta_response")
    logger.info(f"📤 interactive[{preset}] → {to}  HTTP {r.status_code}")
    if r.status_code != 200:
        logger.warning("⚠️  Interactive failed — falling back to plain text")
        return send_text(to, body, tenant_id)
    return r

def send_reply(to: str, body: str, preset: str | None, tenant_id: str = None) -> requests.Response:
    """Send text only or interactive depending on preset."""
    if not preset:
        return send_text(to, body, tenant_id)
    return send_interactive(to, body, preset, tenant_id)

def fetch_templates(tenant_id: str = None) -> list:
    """List approved WhatsApp message templates for the Broadcast Panel registry.

    Phase: Template Registry. Reads the WABA's message_templates edge and returns
    Meta's raw template objects (name, status, category, language, components) so
    the panel can auto-detect header type / variables / buttons / status.

    Uses the global WABA_ID + ACCESS_TOKEN (the primary-tenant Meta credentials
    the broadcast flow already runs on); the Tenant model stores no WABA business
    account id, so per-tenant template listing is not applicable here.
    """
    from app.config import WABA_ID, ACCESS_TOKEN as _TOKEN
    if not WABA_ID or not _TOKEN:
        raise ValueError("Template registry unavailable: WABA_ID / ACCESS_TOKEN not configured.")
    url = f"https://graph.facebook.com/v21.0/{WABA_ID}/message_templates"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        params={"fields": "name,status,category,language,components", "limit": 250},
    )
    if r.status_code != 200:
        raise ValueError(f"Template fetch failed: HTTP {r.status_code} — {r.text}")
    return (r.json() or {}).get("data", [])


def upload_media(file_bytes: bytes, filename: str, content_type: str,
                 tenant_id: str = None) -> str:
    """Upload media to the WhatsApp Cloud API and return its media_id.

    Phase: Image Header Template Support. The returned id is used in a template
    IMAGE-header component: {"type":"image","image":{"id":<media_id>}}.

    Uses the same per-tenant WABA credentials as the send path. Multipart POST,
    so the Authorization header is set explicitly (NOT _wa_headers, which forces
    Content-Type: application/json).
    """
    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/media"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, file_bytes, content_type)},
        data={"messaging_product": "whatsapp", "type": content_type},
    )
    if r.status_code != 200:
        raise ValueError(f"Media upload failed: HTTP {r.status_code} — {r.text}")
    media_id = (r.json() or {}).get("id")
    if not media_id:
        raise ValueError(f"Media upload returned no id: {r.text}")
    return media_id


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
    r = requests.post(url, headers=_wa_headers(token), json=payload)

    # ── Diagnostics only (no behaviour change) ──────────────────────────────
    # Surfaces the full Meta error body, which the broadcast route discards
    # (it reads only r.status_code). The Authorization token lives in the
    # request HEADERS, not this payload, so logging the payload leaks nothing.
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        logger.error(f"❌ template '{template}' → {to}  HTTP {r.status_code}")
        logger.info(f"   meta.code={err.get('code')} subcode={err.get('error_subcode')} "
              f"type={err.get('type')}")
        logger.info(f"   meta.message={err.get('message')}")
        logger.info(f"   meta.error_data={err.get('error_data')}")
        logger.info(f"   meta.body={r.text}")
        _components = payload["template"].get("components") or []
        _has_image_header = any(
            isinstance(c, dict) and c.get("type") == "header"
            and any(p.get("type") == "image" for p in c.get("parameters", []))
            for c in _components
        )
        logger.info(f"   sent.components={_components or '<none>'}")
        logger.info(f"   sent.has_image_header={_has_image_header}")
    return r

def send_automation(to: str, text: str, name: str = "Student", tenant_id: str = None) -> requests.Response:
    """
    Phase 11-D3B2: Automation-only Interceptor
    Checks the 24-hour window. If closed, queues the text and sends a template fallback.
    Phase 12-C2: Now resolves tenant_id dynamically before PendingMessage INSERT.
    """
    from app.models import ConversationState, PendingMessage
    from app.extensions import db
    from datetime import datetime
    # Phase 12-C2 / Phase 0 Sprint 2: resolve tenant_id before any INSERT
    from app.services.log_service import resolve_tenant_id

    tenant_id = resolve_tenant_id(tenant_id)

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
            logger.warning(f"⚠️  Template fallback failed for {to}: HTTP {response.status_code} - {response.text}")
        else:
            logger.warning(f"🛑 Interceptor active: Template fallback sent to {to}")
            
        return response

