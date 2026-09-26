"""Phase RC2.5.19-E: WhatsApp Embedded Signup (Tech Provider) — server side.

WHAT THIS MODULE DOES
---------------------
Turns the result of Meta's Embedded Signup popup into a VERIFIED, tenant-bound
WhatsApp connection, and then activates it:

    exchange_code()      code (30 s TTL)  -> customer business token
    verified_waba_id()   debug_token: the WABA must be one the token was granted
    verify_phone()       the phone number must be listed under that WABA
    bind_connection()    row-locked, one-time write onto the caller's tenant
    activate()           register the number (tenant PIN) + subscribed_apps

THE SECURITY BOUNDARY IS HERE, NOT IN THE BROWSER
-------------------------------------------------
The popup reports waba_id / phone_number_id to the page. Those are HINTS: the
page is the client and can send anything. A binding is written only when the
server has proven, with the token it just obtained, that Meta granted THIS
app access to that WABA and that the number belongs to it -- and only onto
the caller's own tenant, which the route takes from the session, never from
the request.

SECRETS
-------
The code, the business token, the tenant's PIN and META_SYSTEM_USER_TOKEN are
never logged, returned, audited or stored in the session. The business token
is stored only MultiFernet-encrypted. Errors carry a category, the HTTP status
and Meta's numeric error code -- never a response body.

GRAPH
-----
Every call goes through whatsapp_service._graph_base() (v21.0, RC2.5.19-E
decision E-D7) and _timeout(). Nothing here upgrades the version.

UNRESOLVED META ITEMS (production validation TODOs, NOT settled here)
--------------------------------------------------------------------
  * whether the code exchange ever needs redirect_uri   (none is sent)
  * whether debug_token accepts an app token instead of the System User token
  * whether /register is idempotent                     (activate() re-runs it)
  * PIN retry / lockout behaviour
  * phone-number id stability
  * Graph v21.0 compatibility with Embedded Signup v4
Each must be confirmed in the first flagged live-tenant test before the
feature is enabled for customers.
"""
import importlib
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# ── connection status (tenants.whatsapp_connection_status) ─────────────────
STATUS_PENDING_ACTIVATION = "VERIFIED_PENDING_ACTIVATION"
STATUS_CONNECTED = "CONNECTED"
STATUS_RECONNECT_REQUIRED = "RECONNECT_REQUIRED"

SOURCE_EMBEDDED_SIGNUP = "embedded_signup"

# ── failure categories (SignupError.category) ──────────────────────────────
NOT_CONFIGURED = "not_configured"
EXCHANGE_FAILED = "exchange_failed"
WABA_NOT_GRANTED = "waba_not_granted"
PHONE_NOT_IN_WABA = "phone_not_in_waba"
ALREADY_BOUND = "already_bound"
REGISTER_FAILED = "register_failed"
SUBSCRIBE_FAILED = "subscribe_failed"
TRANSPORT = "transport"
STORAGE_FAILED = "storage_failed"
NOT_PENDING = "not_pending"


class SignupError(Exception):
    """A classified Embedded Signup failure. Carries no secret and no body."""

    def __init__(self, category, http_status=None, meta_code=None):
        super().__init__(category)
        self.category = category
        self.http_status = http_status
        self.meta_code = meta_code


def _cfg(name):
    # Read at call time (same reason as whatsapp_service._graph_base): tests
    # and a changed environment take effect without re-importing this module.
    return getattr(importlib.import_module("app.config"), name, "") or ""


def _wa():
    return importlib.import_module("app.services.whatsapp_service")


def is_configured() -> bool:
    """True when every value the flow needs is present. Never returns values."""
    return all(_cfg(n) for n in ("META_APP_ID", "META_APP_SECRET",
                                 "META_ES_CONFIG_ID", "META_SYSTEM_USER_TOKEN"))


def public_config() -> dict:
    """The only configuration the browser receives: public identifiers."""
    import app.config as c
    return {"app_id": _cfg("META_APP_ID"),
            "config_id": _cfg("META_ES_CONFIG_ID"),
            "graph_version": c.GRAPH_API_VERSION}


# ── HTTP ────────────────────────────────────────────────────────────────────

def _call(method, path, category, *, token=None, params=None, data=None):
    """One Graph request. Returns the JSON body of a 2xx, else raises
    SignupError(category) with the status and Meta's numeric code only."""
    wa = _wa()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = getattr(requests, method)(f"{wa._graph_base()}/{path}", headers=headers,
                                      params=params, data=data, timeout=wa._timeout())
    except wa._transport_errors() as exc:
        logger.warning("[wa-es] %s: transport %s", category, type(exc).__name__)
        raise SignupError(TRANSPORT)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if 200 <= r.status_code < 300:
        return body
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    logger.warning("[wa-es] %s: http=%s meta.code=%s", category, r.status_code, err.get("code"))
    raise SignupError(category, http_status=r.status_code, meta_code=err.get("code"))


# ── verification ────────────────────────────────────────────────────────────

def exchange_code(code: str) -> str:
    """Exchange the popup's code for the customer's business token.

    GET /oauth/access_token with client_id, client_secret, code. No
    redirect_uri is sent (UNRESOLVED -- confirm in the live test).
    """
    if not is_configured():
        raise SignupError(NOT_CONFIGURED)
    body = _call("get", "oauth/access_token", EXCHANGE_FAILED, params={
        "client_id": _cfg("META_APP_ID"),
        "client_secret": _cfg("META_APP_SECRET"),
        "code": code,
    })
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise SignupError(EXCHANGE_FAILED)
    return token


def verified_waba_id(business_token: str, waba_hint: str) -> str:
    """The WABA the business token was granted, which must equal the hint.

    GET /debug_token?input_token=<business token>, authorised with Oxford's
    System User token. The WABA must be in granular_scopes[].target_ids for
    whatsapp_business_management.
    """
    body = _call("get", "debug_token", WABA_NOT_GRANTED,
                 token=_cfg("META_SYSTEM_USER_TOKEN"),
                 params={"input_token": business_token})
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    granted = set()
    for scope in data.get("granular_scopes") or []:
        if isinstance(scope, dict) and scope.get("scope") == "whatsapp_business_management":
            granted.update(str(t) for t in scope.get("target_ids") or [])
    if not data.get("is_valid", True) or str(waba_hint) not in granted:
        raise SignupError(WABA_NOT_GRANTED)
    return str(waba_hint)


def verify_phone(business_token: str, waba_id: str, phone_hint: str) -> str:
    """The phone number, which must be listed under the verified WABA."""
    body = _call("get", f"{waba_id}/phone_numbers", PHONE_NOT_IN_WABA,
                 token=business_token, params={"fields": "id"})
    ids = {str(p.get("id")) for p in body.get("data") or [] if isinstance(p, dict)}
    if str(phone_hint) not in ids:
        raise SignupError(PHONE_NOT_IN_WABA)
    return str(phone_hint)


# ── binding ─────────────────────────────────────────────────────────────────

def bind_connection(tenant_id: str, waba_id: str, phone_id: str, business_token: str):
    """Write the verified connection onto `tenant_id`, once.

    Row-locked; re-checks that the tenant is still unbound (E-D4) and that no
    other tenant holds the number or the WABA. The partial unique indexes on
    tenants.waba_phone_number_id and tenants.waba_id are the final boundary
    against a race. Raises SignupError(ALREADY_BOUND / STORAGE_FAILED).
    """
    from sqlalchemy.exc import IntegrityError
    from app.extensions import db
    from app.models import Tenant
    from app.services.encryption_service import encrypt_token

    try:
        tenant = (db.session.query(Tenant).filter(Tenant.id == tenant_id)
                  .with_for_update().one())
        if tenant.waba_phone_number_id or tenant.waba_access_token_encrypted:
            raise SignupError(ALREADY_BOUND)
        clash = Tenant.query.filter(
            Tenant.id != tenant_id,
            (Tenant.waba_phone_number_id == phone_id) | (Tenant.waba_id == waba_id),
        ).first()
        if clash is not None:
            raise SignupError(ALREADY_BOUND)
        tenant.waba_phone_number_id = phone_id
        tenant.waba_id = waba_id
        tenant.waba_access_token_encrypted = encrypt_token(business_token)
        tenant.waba_connection_source = SOURCE_EMBEDDED_SIGNUP
        tenant.whatsapp_connection_status = STATUS_PENDING_ACTIVATION
        tenant.waba_token_obtained_at = datetime.utcnow()
        db.session.commit()
    except SignupError:
        db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        raise SignupError(ALREADY_BOUND)
    except Exception as exc:                                     # noqa: BLE001
        db.session.rollback()
        logger.error("[wa-es] bind failed for tenant %s: %s", tenant_id, type(exc).__name__)
        raise SignupError(STORAGE_FAILED)


# ── activation ──────────────────────────────────────────────────────────────

def register_phone(business_token: str, phone_id: str, pin: str) -> None:
    """POST /{phone_id}/register with the tenant's 6-digit PIN. The PIN is
    sent to Meta and nowhere else."""
    _call("post", f"{phone_id}/register", REGISTER_FAILED, token=business_token,
          data={"messaging_product": "whatsapp", "pin": pin})


def subscribe_app(business_token: str, waba_id: str) -> None:
    """POST /{waba_id}/subscribed_apps -- without it no webhook is delivered."""
    _call("post", f"{waba_id}/subscribed_apps", SUBSCRIBE_FAILED, token=business_token)


def activate(tenant_id: str, pin: str) -> str:
    """Register the bound number and subscribe the app; mark CONNECTED.

    Only for a tenant in VERIFIED_PENDING_ACTIVATION from Embedded Signup. On
    any failure the status stays VERIFIED_PENDING_ACTIVATION so the tenant can
    retry. A retry re-runs registration (idempotency UNRESOLVED -- live test).
    """
    from app.extensions import db
    from app.models import Tenant
    from app.services.encryption_service import decrypt_token

    tenant = Tenant.query.get(tenant_id)
    if (tenant is None
            or tenant.waba_connection_source != SOURCE_EMBEDDED_SIGNUP
            or tenant.whatsapp_connection_status != STATUS_PENDING_ACTIVATION
            or not tenant.waba_phone_number_id or not tenant.waba_id):
        raise SignupError(NOT_PENDING)
    token = decrypt_token(tenant.waba_access_token_encrypted)
    if not token:
        raise SignupError(STORAGE_FAILED)
    register_phone(token, tenant.waba_phone_number_id, pin)
    subscribe_app(token, tenant.waba_id)
    tenant.whatsapp_connection_status = STATUS_CONNECTED
    try:
        db.session.commit()
    except Exception as exc:                                     # noqa: BLE001
        db.session.rollback()
        logger.error("[wa-es] activation commit failed for tenant %s: %s",
                     tenant_id, type(exc).__name__)
        raise SignupError(STORAGE_FAILED)
    return STATUS_CONNECTED
