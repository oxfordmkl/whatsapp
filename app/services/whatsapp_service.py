import logging
import requests
import threading
from dataclasses import dataclass
from app.config import ACCESS_TOKEN, PHONE_NUMBER_ID
from app.bot.constants import BUTTON_PRESETS

logger = logging.getLogger(__name__)

token_status = "unknown"

def _get_waba_credentials(tenant_id: str = None) -> tuple[str, str]:
    from app.models import Tenant
    from app.services.log_service import resolve_tenant_id
    from app.services.encryption_service import decrypt_token

    # Phase RC2.4.1: OUTBOUND CREDENTIALS FAIL CLOSED WITHOUT A TENANT.
    #
    # resolve_tenant_id(None) answers PRIMARY_TENANT_ID (leg 2). For a LOG
    # write that is a defensible default; for OUTBOUND TRANSPORT it is not —
    # it decides which WhatsApp number the customer sees, whose quota is
    # spent, and which tenant's webhook receives the reply. A caller that does
    # not name a tenant is not asking for the primary one, it has lost track
    # of which tenant it is acting for.
    #
    # RC2.4.0 traced 5 production messages that were sent through the primary
    # tenant's WABA while being persisted elsewhere: the transport and the
    # record disagreed because only one of them was explicit. This closes the
    # transport half. Nothing else about resolution changes — an explicitly
    # named tenant still resolves exactly as before, and a known tenant with
    # no credentials still fails closed below.
    #
    # resolve_tenant_id() itself is deliberately NOT modified: it is shared
    # with the log writers and bot/router.smart_reply(), whose leg-2 behaviour
    # is out of this phase's scope.
    if not tenant_id:
        raise ValueError(
            "Outbound WhatsApp requires an explicit tenant_id. Refusing to "
            "fall back to the primary tenant: the caller must name the tenant "
            "whose WABA identity is being used.")

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
    #
    # Phase RC2.4.4a: read PRIMARY_TENANT_ID directly instead of calling
    # resolve_tenant_id(None). The old form was a CONFIG READ dressed up as a
    # resolution: it fired leg 2's "[tenant] implicit resolution" ERROR on every
    # legitimate primary-tenant send, drowning the one log line that is supposed
    # to signal a real defect. That alarm has to be trustworthy before leg 2 can
    # be retired, which is why this is fixed here and not later.
    #
    # Boolean behaviour is identical: a blank/unset PRIMARY_TENANT_ID still
    # yields no match (falls through to the raise below), and no app context
    # still yields no match. resolve_tenant_id() itself is NOT modified.
    try:
        from flask import current_app
        _primary = (current_app.config.get("PRIMARY_TENANT_ID") or "").strip()
    except Exception:
        _primary = ""
    if _primary and tenant_id == _primary:
        if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
            raise ValueError("Primary tenant missing global WABA configuration.")
        return PHONE_NUMBER_ID, ACCESS_TOKEN

    raise ValueError(f"Tenant {tenant_id} has no WABA credentials configured.")

def _wa_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


# ── Phase RC2.5.18-A: transport hardening ───────────────────────────────────
#
# Used only when app.config is a stub that predates the settings. Several test
# suites load this module against a hand-built app.config holding just the
# names they need, and a top-level `from app.config import ...` would make the
# module unimportable there. The real defaults, and the reasoning behind them,
# live in app/config.py -- including why these are NOT a wall-clock limit.
_FALLBACK_CONNECT_TIMEOUT_SECONDS = 3.05
_FALLBACK_READ_TIMEOUT_SECONDS = 8.0

#: Status code of a TransportFailure. Outside 4xx on purpose: the campaign
#: dispatcher's provider-failure classifier treats every non-4xx as TRANSIENT,
#: which is how its `except Exception` branch classified a RAISED transport
#: error before RC2.5.18-A. It can never be mistaken for success.
#: (Named descriptively, not by module: a layering test forbids this file
#: from naming the campaign worker module at all, even in prose.)
TRANSPORT_FAILURE_STATUS = 599

#: Delivery state of a TransportFailure. The distinction a caller needs is not
#: "did the call fail" but "could Meta have received it":
#:
#:   NOT_SENT   the request provably never left this process -- the connection
#:              was never established. Nothing reached Meta.
#:   AMBIGUOUS  the request may have been transmitted and processed; only the
#:              RESPONSE was lost. The customer may already have the message.
#:
#: An AMBIGUOUS failure must never trigger an automatic second send: a format
#: fallback or an immediate resend would deliver the message twice.
NOT_SENT = "not_sent"
AMBIGUOUS = "ambiguous"


def _timeout() -> tuple:
    """(connect, read) timeouts, read at call time so tests can vary them.

    import_module("app.config") rather than `from app import config`: the
    latter imports the parent `app` package, which suites that load this file
    against stubbed app.* modules never import. import_module returns the
    already-registered app.config -- real or stub -- without touching `app`.
    """
    import importlib
    _cfg = importlib.import_module("app.config")
    return (
        getattr(_cfg, "WHATSAPP_CONNECT_TIMEOUT_SECONDS",
                _FALLBACK_CONNECT_TIMEOUT_SECONDS),
        getattr(_cfg, "WHATSAPP_READ_TIMEOUT_SECONDS",
                _FALLBACK_READ_TIMEOUT_SECONDS),
    )


def _delivery_state(exc) -> str:
    """NOT_SENT only when the request provably never left; else AMBIGUOUS.

    Deliberately conservative -- the default is AMBIGUOUS, because wrongly
    calling a transmitted request NOT_SENT is what causes a duplicate message,
    while wrongly calling an unsent one AMBIGUOUS only forgoes a fallback.

    NOT_SENT is proven by exactly two shapes, both of which mean the TCP
    connection was never established:
      * ConnectTimeout -- the connect phase timed out;
      * a ConnectionError caused by urllib3's NewConnectionError (which
        includes NameResolutionError) -- refused, unreachable, or DNS failure.

    A bare ConnectionError is NOT evidence of non-delivery. Verified against a
    real socket: a server that RECEIVES the request and then resets the
    connection surfaces as a plain ConnectionError with no NewConnectionError
    anywhere in its cause chain. ReadTimeout, ChunkedEncodingError and every
    other failure after the connection opened are AMBIGUOUS for the same reason.
    """
    rexc = getattr(requests, "exceptions", None)
    connect_timeout = getattr(rexc, "ConnectTimeout", None)
    if connect_timeout is not None and isinstance(exc, connect_timeout):
        return NOT_SENT

    try:
        from urllib3.exceptions import NewConnectionError
    except Exception:                                           # noqa: BLE001
        return AMBIGUOUS

    # Walk the cause chain: requests wraps urllib3's MaxRetryError, whose
    # .reason is the underlying connection error.
    seen, stack = set(), [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, NewConnectionError):
            return NOT_SENT
        stack.append(getattr(cur, "reason", None))
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
        stack.extend(a for a in getattr(cur, "args", ())
                     if isinstance(a, BaseException))
    return AMBIGUOUS


def _transport_errors() -> tuple:
    """requests' transport exception base class, or () if unavailable.

    Resolved lazily rather than referenced at module level: several test suites
    replace `requests` with a bare types.ModuleType holding only post/get, and
    a module-level `requests.exceptions.RequestException` would make this file
    unimportable under them. `except ()` is valid Python and catches nothing,
    so under such a stub behaviour is exactly what it was before this phase.
    """
    exc = getattr(requests, "exceptions", None)
    base = getattr(exc, "RequestException", None) if exc is not None else None
    return (base,) if base is not None else ()


class TransportFailure:
    """Stands in for a requests.Response when the request never completed.

    The four send functions have always RETURNED a response; callers read
    .status_code, .text and .json() and nothing else. Raising a new exception
    type from them would change that contract underneath every caller -- in
    particular broadcast.py's per-number loops, which have no try/except and
    would abort mid-broadcast without writing their BROADCAST_SEND audit row.
    Returning a non-200 result keeps the contract: every existing caller
    already knows how to handle a failed send.

    Carries the exception CLASS name only. A requests exception message can
    embed the request URL, and the text is surfaced by callers into logs and
    campaign failure reasons.

    RC2.5.18-A-FIX1: it is NOT just another non-200. `delivery_state` says
    whether Meta could have received the message, and `ambiguous` is the
    question every caller actually has to answer before sending again: "may
    this already have been delivered?" Use is_ambiguous(response) rather than
    inferring it from status_code.
    """
    status_code = TRANSPORT_FAILURE_STATUS
    ok = False

    def __init__(self, error_name: str, delivery_state: str = AMBIGUOUS):
        self.error_name = error_name
        self.delivery_state = delivery_state
        self.text = f"transport failure: {error_name} ({delivery_state})"

    @property
    def ambiguous(self) -> bool:
        return self.delivery_state != NOT_SENT

    def json(self):
        return {"error": {"message": self.text, "type": "transport",
                          "code": None,
                          "delivery_state": self.delivery_state}}


def is_transport_failure(response) -> bool:
    """True for a TransportFailure: the request did not get a Meta answer."""
    return isinstance(response, TransportFailure)


def is_ambiguous(response) -> bool:
    """True when Meta may already have received the message.

    Only a TransportFailure can be ambiguous. A real HTTP response -- 200 or a
    Meta rejection -- is a definite answer, so this is False for it.
    """
    return is_transport_failure(response) and response.ambiguous


def _mask(to) -> str:
    """Masked destination for logs. Never the full number.

    Imported lazily for the same reason as _timeout(): this module is loaded
    under stubbed app.* modules in several suites.
    """
    from app.services.phone_service import mask_destination
    return mask_destination(to)


def _post_message(url: str, token: str, payload: dict):
    """POST one message payload. Never raises for a transport failure.

    The single transport path for the four send functions, which previously
    each made an identical, unbounded requests.post.
    """
    try:
        return requests.post(url, headers=_wa_headers(token), json=payload,
                             timeout=_timeout())
    except _transport_errors() as exc:
        # Class name and delivery state only -- see TransportFailure. No URL,
        # no destination, no payload: the payload of an authentication
        # template IS the code.
        state = _delivery_state(exc)
        logger.error("❌ WhatsApp transport failure (%s, %s)",
                     type(exc).__name__, state)
        return TransportFailure(type(exc).__name__, state)


def validate_token():
    global token_status
    try:
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            timeout=_timeout(),
        )
    except _transport_errors() as exc:
        # Left "unknown", deliberately not "invalid": a timeout says nothing
        # about whether the token is valid, and /health reports this value.
        logger.error("❌ Token check failed: transport %s", type(exc).__name__)
        return
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
    r = _post_message(url, token, payload)
    logger.info(f"📤 text → {_mask(to)}  HTTP {r.status_code}")
    return r

REPLY_BUTTON_TITLE_MAX = 20
REPLY_BUTTON_MAX = 3


def send_interactive(to: str, body: str, preset, tenant_id: str = None) -> requests.Response:
    """Send message with up to 3 reply buttons.

    `preset` is either a named BUTTON_PRESETS key (legacy, unchanged) or an
    explicit list of {"id", "title"} dicts supplied by the screen builder
    (Phase 1.6.6). Meta's 3-button / 20-char limits are enforced defensively.
    """
    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"

    if isinstance(preset, (list, tuple)):
        buttons_data = list(preset)
    else:
        buttons_data = BUTTON_PRESETS.get(preset, BUTTON_PRESETS["COURSE"])
    buttons_data = [
        {"id": str(b.get("id", "")), "title": (b.get("title") or "")[:REPLY_BUTTON_TITLE_MAX]}
        for b in buttons_data[:REPLY_BUTTON_MAX]
    ]
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
    r = _post_message(url, token, payload)
    _perf_mark("meta_response")
    logger.info(f"📤 interactive[{preset}] → {_mask(to)}  HTTP {r.status_code}")
    if is_transport_failure(r):
        # RC2.5.18-A-FIX1: NO fallback after a transport failure. The fallback
        # exists because Meta REJECTED the interactive format; a network
        # failure is not a format problem. If the failure is AMBIGUOUS the
        # interactive message may already be on the customer's phone, and a
        # text fallback would deliver it twice. If it is NOT_SENT the network
        # is down, and a second call would only wait out another timeout on
        # the request path.
        logger.warning("⚠️  Interactive not confirmed (%s) — no fallback sent",
                       r.delivery_state)
        return r
    if r.status_code != 200:
        logger.warning("⚠️  Interactive failed — falling back to plain text")
        return send_text(to, body, tenant_id)
    return r

@dataclass(frozen=True)
class ListMessage:
    """A List Message plus its legacy rendering.

    Phase 1.6.9: routing code returns this in the existing `preset` slot, so the
    single send_reply() pipeline gains list support without any caller change
    and without a second send path.

    `fallback_body` / `fallback_preset` describe how this same screen was
    rendered before List Messages existed. The transport uses them when
    WA_LIST_MESSAGES is OFF — which is why business logic never inspects the flag.
    """
    button_label: str
    sections: list
    header: str = ""
    footer: str = ""
    fallback_body: str | None = None
    fallback_preset: object = None


def send_reply(to: str, body: str, preset=None, tenant_id: str = None) -> requests.Response:
    """Single send pipeline. Dispatches on what `preset` is:

        None                      → plain text
        str                       → reply buttons from a named preset (legacy)
        list/tuple of button dicts→ explicit reply buttons
        ListMessage               → List Message (or its legacy fallback)

    Caller behaviour is unchanged: the webhook still calls
    send_reply(to, text, preset, tenant_id).
    """
    if isinstance(preset, ListMessage):
        return send_list(
            to, body, preset.button_label, preset.sections,
            header=preset.header, footer=preset.footer, tenant_id=tenant_id,
            fallback_body=preset.fallback_body,
            fallback_preset=preset.fallback_preset,
        )
    if not preset:
        return send_text(to, body, tenant_id)
    return send_interactive(to, body, preset, tenant_id)


# ── Phase 1.6.2: WhatsApp List Message transport ────────────────────────────
# Transport concern only — this module builds and posts the payload. It never
# decides WHICH screen to show; screen content comes from app/bot/screens.py.
#
# Meta platform limits (enforced defensively here, where the API contract lives).
LIST_BUTTON_LABEL_MAX = 20
LIST_SECTION_TITLE_MAX = 24
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESC_MAX = 72
LIST_HEADER_MAX = 60
LIST_FOOTER_MAX = 60
LIST_BODY_MAX = 1024
LIST_MAX_ROWS = 10          # total rows across ALL sections


def _clip(text, limit: int) -> str:
    return (text or "")[:limit]


def build_list_payload(to: str, body: str, button_label: str, sections: list,
                       header: str = "", footer: str = "") -> dict:
    """Build a WhatsApp interactive list payload, enforcing Meta's limits.

    `sections` is a list of {"title": str, "rows": [{"id","title","description"}]}.
    Rows are capped at LIST_MAX_ROWS in total; empty sections are dropped.
    Pure function — no I/O, no flag checks.
    """
    out_sections = []
    remaining = LIST_MAX_ROWS

    for section in sections or []:
        if remaining <= 0:
            break
        rows = []
        for row in (section.get("rows") or []):
            if remaining <= 0:
                break
            entry = {
                "id": str(row.get("id", "")),
                "title": _clip(row.get("title"), LIST_ROW_TITLE_MAX),
            }
            description = _clip(row.get("description"), LIST_ROW_DESC_MAX)
            if description:
                entry["description"] = description
            rows.append(entry)
            remaining -= 1
        if rows:
            out_sections.append({
                "title": _clip(section.get("title"), LIST_SECTION_TITLE_MAX),
                "rows": rows,
            })

    interactive = {
        "type": "list",
        "body": {"text": _clip(body, LIST_BODY_MAX)},
        "action": {
            "button": _clip(button_label, LIST_BUTTON_LABEL_MAX),
            "sections": out_sections,
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _clip(header, LIST_HEADER_MAX)}
    if footer:
        interactive["footer"] = {"text": _clip(footer, LIST_FOOTER_MAX)}

    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


def _list_text_fallback(body: str, sections: list) -> str:
    """Plain-text rendering used when List Messages are unavailable.

    Row titles are listed without numeric prefixes on purpose: a numeric
    affordance would invite replies that collide with the legacy positional
    handlers.
    """
    lines = [body or ""]
    for section in sections or []:
        title = (section.get("title") or "").strip()
        if title:
            lines.append(f"\n*{title}*")
        for row in (section.get("rows") or []):
            row_title = (row.get("title") or "").strip()
            if row_title:
                lines.append(f"• {row_title}")
    return "\n".join(lines).strip()


def send_list(to: str, body: str, button_label: str, sections: list,
              header: str = "", footer: str = "", tenant_id: str = None,
              fallback_body: str | None = None,
              fallback_preset=None) -> requests.Response:
    """Send an interactive List Message.

    WA_LIST_MESSAGES is evaluated HERE and nowhere else — it is purely a
    transport decision. When OFF (or on an API error) the screen degrades to its
    legacy rendering: `fallback_body`/`fallback_preset` when the caller supplied
    them, otherwise a plain-text listing of the rows.
    """
    from app.flags import wa_list_messages_enabled

    def _degrade():
        text = fallback_body if fallback_body is not None else _list_text_fallback(body, sections)
        if fallback_preset:
            return send_interactive(to, text, fallback_preset, tenant_id)
        return send_text(to, text, tenant_id)

    if not wa_list_messages_enabled():
        return _degrade()

    phone_id, token = _get_waba_credentials(tenant_id)
    url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
    payload = build_list_payload(to, body, button_label, sections, header, footer)

    from app.perf import mark as _perf_mark
    _perf_mark("send_start")
    r = _post_message(url, token, payload)
    _perf_mark("meta_response")
    logger.info(f"📤 list → {_mask(to)}  HTTP {r.status_code}")
    if is_transport_failure(r):
        # RC2.5.18-A-FIX1: NO degrade after a transport failure -- same reason
        # as send_interactive(). The list may already have been delivered.
        logger.warning("⚠️  List not confirmed (%s) — no fallback sent",
                       r.delivery_state)
        return r
    if r.status_code != 200:
        logger.warning("⚠️  List message failed — falling back to legacy rendering")
        return _degrade()
    return r

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
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            params={"fields": "name,status,category,language,components", "limit": 250},
            timeout=_timeout(),
        )
    except _transport_errors() as exc:
        # This function's contract is "raise ValueError on failure", and
        # broadcast.templates_route catches exactly ValueError. A raw transport
        # exception would escape that handler as a 500.
        raise ValueError(f"Template fetch failed: transport {type(exc).__name__}")
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
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, file_bytes, content_type)},
            data={"messaging_product": "whatsapp", "type": content_type},
            timeout=_timeout(),
        )
    except _transport_errors() as exc:
        # Same contract as fetch_templates: broadcast.upload_media_route
        # catches ValueError.
        raise ValueError(f"Media upload failed: transport {type(exc).__name__}")
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
    r = _post_message(url, token, payload)

    # ── Diagnostics only (no behaviour change) ──────────────────────────────
    # Surfaces Meta's error fields, which the broadcast route discards (it
    # reads only r.status_code).
    #
    # Phase RC2.5.18-A: the previous comment here read "logging the payload
    # leaks nothing". That held for marketing templates and is false for an
    # AUTHENTICATION template, whose body and button parameters ARE the
    # one-time code -- so any Meta rejection would have written a live
    # credential to production logs. Three changes:
    #   * the destination is masked;
    #   * parameter VALUES are never logged, only the component shape;
    #   * every value in the payload is scrubbed out of Meta's echoed fields
    #     before they are logged, because an error describing a bad parameter
    #     may quote it back.
    # The raw response body is no longer logged: every useful field in it is
    # already logged individually above, scrubbed. It is logged only when the
    # body was not parseable JSON, and then scrubbed and truncated.
    if r.status_code != 200:
        secrets = _payload_secrets(payload)
        try:
            err = r.json().get("error", {}) or {}
        except (ValueError, AttributeError):
            err = {}
        if not isinstance(err, dict):
            # A malformed body such as {"error": "..."} must not turn a failed
            # send into an AttributeError on the next line.
            err = {}
        logger.error(f"❌ template '{template}' → {_mask(to)}  HTTP {r.status_code}")
        logger.info(f"   meta.code={err.get('code')} subcode={err.get('error_subcode')} "
              f"type={err.get('type')}")
        logger.info(f"   meta.message={_scrub(err.get('message'), secrets)}")
        logger.info(f"   meta.error_data={_scrub(err.get('error_data'), secrets)}")
        if not err:
            logger.info(f"   meta.body={_scrub(getattr(r, 'text', ''), secrets)[:300]}")
        _components = payload["template"].get("components") or []
        _has_image_header = any(
            isinstance(c, dict) and c.get("type") == "header"
            and any(p.get("type") == "image" for p in c.get("parameters", []))
            for c in _components
        )
        logger.info(f"   sent.components={_component_shape(_components)}")
        logger.info(f"   sent.has_image_header={_has_image_header}")
    return r


#: Values shorter than this are not scrubbed from echoed Meta text. Scrubbing
#: "1" or "en" would mangle every diagnostic line while protecting nothing; the
#: values that matter here -- a six-digit code, a customer's name, a phone
#: number -- are all longer.
_SCRUB_MIN_LEN = 3


def _payload_secrets(payload: dict) -> list:
    """Every string value in a send payload that must not reach a log.

    Collected structurally rather than by knowing the template: the recipient,
    plus every `text` parameter in every component. That covers an
    authentication template's code in the body AND in the COPY_CODE button,
    and a marketing template's customer name, without this module needing to
    know which template is which.
    """
    found = []
    to = payload.get("to")
    if to:
        found.append(str(to))
    for comp in (payload.get("template") or {}).get("components") or []:
        if not isinstance(comp, dict):
            continue
        for p in comp.get("parameters") or []:
            if isinstance(p, dict) and p.get("text") is not None:
                found.append(str(p["text"]))
    # Longest first, so a value that contains a shorter one is removed whole.
    return sorted({s for s in found if len(s) >= _SCRUB_MIN_LEN},
                  key=len, reverse=True)


def _scrub(value, secrets: list) -> str:
    """str(value) with every secret replaced. Safe on None and non-strings."""
    s = "" if value is None else str(value)
    for secret in secrets:
        s = s.replace(secret, "<redacted>")
    return s


def _component_shape(components: list) -> str:
    """Component types and parameter counts only, e.g. "body:1, button[url]:1".

    Enough to diagnose "Number of parameters does not match" -- the commonest
    template error -- without logging a single parameter value.
    """
    if not components:
        return "<none>"
    parts = []
    for c in components:
        if not isinstance(c, dict):
            parts.append("?")
            continue
        label = str(c.get("type"))
        if c.get("sub_type"):
            label += f"[{c.get('sub_type')}]"
        parts.append(f"{label}:{len(c.get('parameters') or [])}")
    return ", ".join(parts)

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
        # Window closed: Queue the original message and send the template.
        #
        # Phase RC2.5.18-A-FIX1: REUSE an identical queued row rather than add
        # a second one. The follow-up worker retries a job whose send returned
        # non-200, and after an AMBIGUOUS failure (below) the first attempt's
        # row is deliberately kept -- so without this, the retry would queue
        # the same text twice and the customer would receive it twice when the
        # inbound webhook flushes the queue. No schema change: an ordinary
        # lookup on the columns the flush itself filters by.
        pending = PendingMessage.query.filter_by(
            phone=to, tenant_id=tenant_id, text=text).first()
        created_here = pending is None
        if created_here:
            pending = PendingMessage(phone=to, text=text, tenant_id=tenant_id)
            db.session.add(pending)
            db.session.commit()

        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": name}]
        }]

        response = send_template(to, "oxford_re_engagement_v1", lang="en", components=components, tenant_id=tenant_id)
        if is_ambiguous(response):
            # RC2.5.18-A-FIX1: the template MAY have been delivered -- only the
            # response was lost. Keep the queued row: if the customer got the
            # template and replies, the inbound webhook's existing flush
            # delivers the queued text exactly as it would after a clean 200.
            # Deleting it here would lose the message the customer was just
            # invited to reply for. No resend: the caller's own retry policy
            # decides what happens next.
            logger.warning(f"⚠️  Template fallback not confirmed for {_mask(to)} "
                           f"({response.delivery_state}) — queued message kept")
        elif response.status_code != 200:
            # A DEFINITE failure: Meta answered and rejected it, or the request
            # provably never left. Nothing reached the customer, so the queued
            # row is removed -- but only if THIS call created it. A reused row
            # belongs to an earlier interception and is still valid.
            if created_here:
                db.session.delete(pending)
                db.session.commit()
            # Phase RC2.5.18-A: destination masked, and Meta's raw body dropped.
            # send_template() has already logged the parsed error fields with
            # every payload value -- here, the customer's name -- scrubbed out;
            # repeating the raw body would undo that.
            logger.warning(f"⚠️  Template fallback failed for {_mask(to)}: HTTP {response.status_code}")
        else:
            logger.warning(f"🛑 Interceptor active: Template fallback sent to {_mask(to)}")

        return response

