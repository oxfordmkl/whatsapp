# Oxford CRM — WhatsApp Architecture
## Meta Cloud API Integration, Webhook Flow, and Multi-Tenant Routing

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Audience:** Engineers, AI Assistants, Integration Specialists
> **Last Updated:** 2026-07-02 | **Next Review:** Phase 16
> **Source Authority:** Verified against `app/routes/webhook.py`, `app/services/whatsapp_service.py`, `app/services/followup_service.py`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Architecture Overview](#2-architecture-overview)
3. [Meta WhatsApp Cloud API](#3-meta-whatsapp-cloud-api)
4. [Inbound Webhook — Verification](#4-inbound-webhook--verification)
5. [Inbound Webhook — Message Processing](#5-inbound-webhook--message-processing)
6. [Multi-Tenant Routing](#6-multi-tenant-routing)
7. [Message Deduplication](#7-message-deduplication)
8. [Opt-Out and Opt-In Handling](#8-opt-out-and-opt-in-handling)
9. [Message Lifecycle](#9-message-lifecycle)
10. [Outbound Message Architecture](#10-outbound-message-architecture)
11. [Broadcast Architecture](#11-broadcast-architecture)
12. [Follow-Up Worker Interactions](#12-follow-up-worker-interactions)
13. [WABA Configuration](#13-waba-configuration)
14. [Thread Safety Model](#14-thread-safety-model)
15. [Current Production Status](#15-current-production-status)
16. [Known Limitations](#16-known-limitations)
17. [Future Roadmap](#17-future-roadmap)
18. [Related Documents](#18-related-documents)

---

## 1. Purpose and Scope

This document describes how Oxford CRM integrates with Meta's WhatsApp Cloud API — from inbound message receipt through AI processing, logging, and automated follow-up sequences.

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                   WhatsApp Architecture                            │
│                                                                    │
│  ┌────────────────────┐                                           │
│  │  Lead (WhatsApp)   │                                           │
│  │  sends message     │                                           │
│  └────────┬───────────┘                                           │
│           │                                                        │
│           ▼ HTTPS                                                  │
│  ┌────────────────────┐                                           │
│  │  Meta WhatsApp     │                                           │
│  │  Cloud API         │                                           │
│  └────────┬───────────┘                                           │
│           │ POST /webhook                                          │
│           ▼                                                        │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │                  webhook_bp.receive_message()             │    │
│  │                                                          │    │
│  │  1. Verify X-Hub-Signature-256 (403 if invalid)          │    │
│  │  2. For EVERY entry → change:                            │    │
│  │     tenant lookup by phone_number_id + status gate       │    │
│  │  3. For EVERY message in an accepted change:             │    │
│  │     claim wamid (sync insert, unique) → opt-out/opt-in   │    │
│  │     → smart_reply() → send_reply() (both synchronous)    │    │
│  │  4. Background threads: logs, lead events, Sheets        │    │
│  │  5. schedule_followups() [if new lead]                   │    │
│  │                                                          │    │
│  │  return 200 OK → Meta                                    │    │
│  └──────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. Meta WhatsApp Cloud API

Oxford CRM connects to Meta's **WhatsApp Cloud API** (Graph API v19.0). This is the enterprise-grade managed API — not the WhatsApp Business App.

**API Base URL:**
```
https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages
```

**Authentication:** `Authorization: Bearer {ACCESS_TOKEN}` in HTTP headers.

**Supported message types for inbound:**
- `text` — standard text message
- `interactive` — button replies and list replies
- `button` — quick reply button press
- All other types are silently ignored (return 200 OK)

**Webhook method:** Meta sends `POST /webhook` for every inbound message. Oxford CRM must return `200 OK` within 20 seconds or Meta will retry.

---

## 4. Inbound Webhook — Verification

**Source:** `app/routes/webhook.py` — `verify_webhook()`

**Route:** `GET /webhook`

Meta sends a one-time verification request when the webhook is configured:

```
GET /webhook?hub.mode=subscribe&hub.verify_token=<VERIFY_TOKEN>&hub.challenge=<random>

if VERIFY_TOKEN is not configured: return "Forbidden", 403   ← fail-closed
if mode == "subscribe" AND hmac.compare_digest(token, VERIFY_TOKEN):
    return challenge, 200  ← Confirms webhook ownership to Meta
else:
    return "Forbidden", 403
```

**`VERIFY_TOKEN`** comes from the `VERIFY_TOKEN` environment variable and has
**no default** (Phase RC2.5.19-D). It must be set, and must match the value
configured in the Meta Developer Portal. It is never logged.

`VERIFY_TOKEN` authenticates only this handshake. Delivered messages are
authenticated separately, by signature (section 5).

---

## 5. Inbound Webhook — Message Processing

**Source:** `app/routes/webhook.py` — `receive_message()` and the helpers
`_iter_change_values()`, `_resolve_accepting_tenant()`, `_process_change()`,
`_process_message()`, `_claim_inbound()`

**Route:** `POST /webhook`

### Authentication (Phase 14C, fail-closed since RC2.5.5a)

The first statement of `receive_message()` is `verify_meta_signature()`:
HMAC-SHA256 of the **raw request body** under `META_APP_SECRET`, compared with
`hmac.compare_digest` against the `X-Hub-Signature-256` header. A missing
secret, a missing or malformed header, or a mismatch returns **403** before
the body is parsed or the database is touched.

### Step-by-Step Processing (Phase RC2.5.19-D)

```
POST /webhook (JSON payload from Meta)
│
├── Signature check → 403 on failure (nothing parsed, nothing written)
│
├── For EVERY entry[] → changes[] → value   (non-list / non-dict items skipped)
│   │
│   ├── No messages in this change (e.g. statuses only): skip THIS change
│   │   (a status never suppresses messages elsewhere in the delivery)
│   │
│   ├── Tenant routing for THIS change (section 6) — dropped changes do not
│   │   affect sibling changes or other tenants' entries
│   │
│   └── For EVERY message in the change   (a failure is isolated to it)
│       ├── Parse text (text / interactive / button); other types ignored
│       ├── CLAIM: insert the incoming ConversationMessage row synchronously
│       │   (section 7) — a duplicate stops here, before any side effect
│       ├── Opt-out / opt-in check
│       ├── is_new_lead = resolve_is_new_lead(...)
│       ├── Background threads: lead events, MessageLog, save_lead_to_sheets
│       ├── Pending-message delivery (synchronous)
│       ├── smart_reply() then send_reply()  — both SYNCHRONOUS in the request
│       ├── Background threads: outbound MessageLog + ConversationMessage
│       └── schedule_followups() if new lead
│
└── return jsonify({"status": "ok"}), 200 → Meta   (always, once signed)
```

The acknowledgement is **always 200** once the signature is valid, including
when an item fails: Meta retries non-2xx and can disable the subscription
after sustained failures. Failures are logged by exception class only; phone
numbers in webhook logs are masked to the last three digits.

---

## 6. Multi-Tenant Routing

**Source:** `app/routes/webhook.py` — `_resolve_accepting_tenant()`

This is the **core tenant isolation mechanism** for WhatsApp. It runs once per
change, so one delivery can safely carry several tenants' events.

```python
phone_number_id = value["metadata"]["phone_number_id"]   # must be a non-empty str
tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()

if tenant is None:
    return None          # unknown / missing / malformed → change dropped

if not tenant_accepts_whatsapp_inbound(tenant):   # ACTIVE or TRIAL only
    return None          # PENDING / SUSPENDED / CANCELLED → change dropped

return tenant.id
```

There is **no fallback tenant**. The former grace fallback to
`PRIMARY_TENANT_ID` was removed in Phase RC2.5.5a; an unregistered
`phone_number_id` never resolves to any tenant, and `PRIMARY_TENANT_ID` and the
global env credentials play no part in inbound routing.

**Why this works:**
- `tenants.waba_phone_number_id` has a partial unique index (RC2.4.2), so at
  most one tenant owns a number
- Binding a number is SUPER_ADMIN-only (RC2.5.19-C)
- Meta sets `metadata.phone_number_id`, and the signature covers the whole body

The WABA id (`entry[].id`) is currently neither stored nor checked.

---

## 7. Message Deduplication

**Source:** `app/routes/webhook.py` — `_claim_inbound()` (Phase RC2.5.19-D)

Meta can deliver the same message more than once. Before any side effect, the
webhook inserts the incoming `ConversationMessage` row **synchronously** and
commits it. That row is the claim:

- the partial unique index `uq_conv_msg_incoming_wa_message_id` on
  `conversation_message(wa_message_id) WHERE wa_message_id IS NOT NULL AND
  direction = 'incoming'` makes a second claim of the same inbound wamid fail;
- a message whose wamid is already claimed — in the same delivery, a later
  redelivery, or a concurrent request that lost the insert race — is skipped:
  no reply, no lead events, no Sheets row.

Unsupported message types are not claimed. A message without an id is stored
with `wa_message_id = NULL` and is not deduplicated.

---

## 8. Opt-Out and Opt-In Handling

**Source:** `app/routes/webhook.py`, lines 100–116 (Phase 11-D1 Task D, Phase 11-D2A)

### Opt-Out Keywords
```
"stop", "unsubscribe", "cancel"
→ ConversationState.is_opted_out = True
→ return 200 (no reply sent)
```

### Opt-In Recovery Keywords
```
"start", "resume", "unstop"
→ ConversationState.is_opted_out = False
→ Continue processing (AI replies resume)
```

### Follow-Up Scheduler Opt-Out Check

The follow-up worker also checks opt-out status before sending:
```python
if state_row and getattr(state_row, 'is_opted_out', False):
    job.done = True  # Cancel all remaining follow-ups for this lead
    continue
```

---

## 9. Message Lifecycle

End-to-end lifecycle of a single inbound message:

```
Lead sends "Hello" to Oxford WhatsApp number
    │
    ▼ Meta delivers to /webhook within ~1 second
webhook_bp.receive_message()
    │
    ├── Parsed: phone="919447XXXXXX", name="Rahul", msg="Hello", wamid="wamid.XXX"
    ├── Routed: tenant_id = Oxford Computers UUID (via phone_number_id lookup)
    ├── Deduplicated: first time seen (wamid not in DB)
    ├── Opt-out check: not opted out
    ├── is_new_lead = True (first contact from this number)
    │
    ├── smart_reply("Hello", "Rahul", "919447XXXXXX", True, tenant_id)
    │       └── returns (welcome_message, "goal_selection")
    │
    ├── Thread 1: send_reply("919447XXXXXX", welcome_message, tenant_id)
    │           └── POST to graph.facebook.com/v19.0/{phone_number_id}/messages
    │
    ├── Thread 2: log_message(phone, "inbound", "text", "Hello", tenant_id)
    │           └── INSERT into message_log
    │
    ├── Thread 3: save_conversation_message(phone, "incoming", "Hello", wamid, tenant_id)
    │           └── INSERT into conversation_message
    │
    ├── Thread 4: save_conversation_message(phone, "outgoing", welcome_message, tenant_id)
    │           └── INSERT into conversation_message
    │
    ├── Thread 5: log_lead_event(phone, "LEAD_CREATED", tenant_id)
    │           └── INSERT into lead_event
    │
    ├── Thread 6: save_lead_to_sheets(name, phone, ...) [legacy Google Sheets]
    │
    └── Thread 7: schedule_followups("919447XXXXXX", "Rahul", tenant_id)
                └── INSERT 3 rows into follow_up_jobs (Day 1, 3, 7)
    │
    ▼
return 200 OK → Meta
    │
    ▼ Day 1 (24 hours later)
follow_up_worker polls DB, finds job.send_at <= now
    └── send_automation("919447XXXXXX", day1_message, tenant_id)
```

---

## 10. Outbound Message Architecture

**Source:** `app/services/whatsapp_service.py`

### `send_text(phone, text, tenant_id)` — AI/Manual replies

Used for real-time replies during WhatsApp conversations.

```python
# Fetches tenant's decrypted WABA token and phone_number_id
# POST https://graph.facebook.com/v19.0/{phone_number_id}/messages
# Body: {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
```

### `send_reply(phone, text, tenant_id)` — Wrapper for `send_text`

Used inside the webhook handler for immediate replies.

### `send_automation(phone, text, tenant_id)` — Follow-up sends

Used by the follow-up worker. Same API call, different source tag for logging.

### Per-Tenant Token Decryption

Every outbound call fetches the tenant's encrypted access token and decrypts it at runtime using Fernet:
```python
token = decrypt_waba_token(tenant.waba_access_token_encrypted)
# Fernet.decrypt(token_bytes) using WABA_ENCRYPTION_KEY
```

---

## 11. Broadcast Architecture

**Source:** `app/routes/broadcast.py`

Broadcasts send template messages to a list of WhatsApp numbers.

### Broadcast Flow
```
Admin selects leads + template message in Marketing Hub
    │
    ▼
POST /broadcast (broadcast_bp)
    │
    ├── Validate BROADCAST_API_KEY header
    ├── Load target phone list from DB (tenant-scoped)
    ├── For each phone:
    │       └── send_text(phone, template_message, tenant_id)
    │
    └── Log campaign results
```

**Authorization:** The broadcast endpoint requires the `BROADCAST_API_KEY` header (separate from login session auth). This prevents accidental mass sends.

---

## 12. Follow-Up Worker Interactions

The follow-up worker (`followup_service.py`) interacts with the WhatsApp layer via `send_automation()`:

```
_followup_worker (daemon thread, every 5 min):
    │
    ├── Queries FollowUpJob WHERE done=False AND send_at <= now
    │
    ├── For each job:
    │   ├── Check opted_out (skip if True)
    │   ├── Check last_msg recency (skip if active in last 6 hours)
    │   ├── send_automation(job.phone, job.message, job.tenant_id)
    │   ├── log_message(...)
    │   ├── save_conversation_message(...)
    │   └── job.done = True
    │
    └── sleep(300) — poll every 5 minutes
```

The worker is fully tenant-aware — `job.tenant_id` is passed through to `send_automation()` which fetches the correct WABA credentials.

---

## 13. WABA Configuration

A tenant's WhatsApp identity is `tenants.waba_phone_number_id` (inbound routing
key, partial unique index since RC2.4.2) plus `tenants.waba_access_token_encrypted`
(MultiFernet: `WABA_ENCRYPTION_KEY`, optional decrypt-only
`WABA_ENCRYPTION_KEYS_PREVIOUS`, since RC2.5.19-C). It is set in one of two ways.

### 13.1 Manual binding (`/tenant/whatsapp`, `POST /tenant/whatsapp/save`)

| Actor | May |
|-------|-----|
| SUPER_ADMIN (`?tenant_id=`) | bind or change a tenant's Phone Number ID and token |
| Tenant ADMIN | replace the token for the number **already bound** to their own tenant; cannot claim or change a number (RC2.5.19-C) |

A number held by another tenant is refused (unique index). `POST
/tenant/whatsapp/clear` releases the number, the token and any Embedded Signup
connection together, and is audited.

### 13.2 Embedded Signup (RC2.5.19-E, flag `WA_EMBEDDED_SIGNUP_ENABLED`, default OFF)

Tech Provider onboarding for a tenant's own ADMIN. Available only when the flag
is on, the configuration is complete, the tenant is ACTIVE or TRIAL, and the
tenant has **no** existing binding (SUPER_ADMIN, impersonating sessions and
bound tenants — including the primary tenant — are refused).

```
POST /tenant/whatsapp/es/start      pre-flight; one-time nonce (session, 10 min,
                                    bound to user + tenant)
FB.login (config_id, response_type=code, Embedded Signup v4)
  message event WA_EMBEDDED_SIGNUP  origin must be facebook.com; waba_id /
                                    phone_number_id are HINTS only
POST /tenant/whatsapp/es/complete   within the code's 30-second lifetime:
  GET  /oauth/access_token          client_id + client_secret + code
                                    -> business integration system user token
  GET  /debug_token                 Authorization: META_SYSTEM_USER_TOKEN;
                                    WABA must be in whatsapp_business_management
                                    target_ids and equal the hint
  GET  /{waba_id}/phone_numbers     number must be listed and equal the hint
  bind (row lock; tenant still unbound; unique phone + WABA indexes)
  -> VERIFIED_PENDING_ACTIVATION
POST /tenant/whatsapp/es/activate   tenant's 6-digit two-step PIN (never stored)
  POST /{phone_number_id}/register
  POST /{waba_id}/subscribed_apps   required for webhook delivery
  -> CONNECTED (tenant must add a payment method in WhatsApp Manager)
```

The tenant always comes from the session, never the request. The code, the
business token, the PIN and `META_SYSTEM_USER_TOKEN` are never logged, audited,
returned or stored in the session; the business token is stored encrypted.
Failures before the bind persist nothing; registration/subscription failures
keep `VERIFIED_PENDING_ACTIVATION` and are retryable. A 401 from the tenant's
Test Connection on a connected Embedded Signup tenant sets
`RECONNECT_REQUIRED` (the customer removed the app).

Connection columns (all NULL for a manual binding): `waba_id` (partial unique),
`whatsapp_connection_status`, `waba_connection_source` (`embedded_signup`),
`waba_token_obtained_at` (audit metadata; business tokens do not expire by
default). Configuration (environment, no defaults): `META_APP_ID`,
`META_ES_CONFIG_ID`, `META_SYSTEM_USER_TOKEN`, plus `META_APP_SECRET`.

Still to be confirmed in the first flagged live-tenant test (not assumed):
whether the exchange needs `redirect_uri`; whether `debug_token` accepts an app
token instead of the System User token; registration idempotency; PIN retry /
lockout; phone-number id stability; Graph v21.0 compatibility with Embedded
Signup v4.

### 13.3 Credential resolution

```
Inbound webhook:  Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
Outbound:         _get_waba_credentials(tenant_id)
                    tenant's DB id + MultiFernet-decrypted token, else
                    env PHONE_NUMBER_ID / ACCESS_TOKEN ONLY for PRIMARY_TENANT_ID,
                    else error (no cross-tenant fallback)
```

---

## 14. Thread Safety Model

The webhook handler deliberately keeps AI processing in the **main request thread** and offloads I/O to daemon threads:

| Operation | Thread | Why |
|-----------|--------|-----|
| Tenant routing | Main | Needs result to continue |
| Inbound claim (dedup) | Main | Must commit before any side effect |
| Opt-out/opt-in check | Main | State affects further processing |
| `smart_reply()` | Main | Reply text needed for send |
| `send_reply()` | Main | Synchronous Graph call, bounded timeouts |
| `log_message()` | Daemon | I/O — non-critical to response time |
| `save_conversation_message()` (outgoing) | Daemon | I/O — non-critical |
| `schedule_followups()` | Main | Writes the follow-up schedule |

The request is not fast by design: the Gemini call in `smart_reply()` and the
Graph send in `send_reply()` both run inside it, before the `200 OK`. Only
logging, lead events, the Sheets write and the outgoing conversation row run
in background threads.

---

## 15. Current Production Status

| Component | Status |
|-----------|--------|
| Webhook verification (`GET /webhook`) | ✅ Live |
| Inbound message processing (`POST /webhook`) | ✅ Live |
| Multi-tenant WABA routing | ✅ Live (Phase 13-B4D2) |
| Deduplication | ✅ Live |
| Opt-out/opt-in handling | ✅ Live |
| AI reply via Gemini | ✅ Live |
| WABA credential encryption | ✅ Live (Phase 13-B4B2) |
| Follow-up worker | ✅ Live |
| Broadcast campaigns | ✅ Live |
| Support for message type: text | ✅ |
| Support for message type: interactive | ✅ |
| Support for message type: button | ✅ |
| Support for message type: image/audio/video | ❌ Ignored |

---

## 16. Known Limitations

| Limitation | Impact | Resolution |
|-----------|--------|-----------|
| Image/audio/video messages ignored | Leads who send media get no response | Phase 16 |
| No pending message queue for 24-hour window | Messages outside session window may fail | `PendingMessage` model exists, queue not wired |
| Follow-up templates hardcoded in English + Malayalam | Cannot be configured per tenant | Phase 16 |

---

## 17. Future Roadmap

| Feature | Phase | Description |
|---------|-------|-------------|
| Image/audio message handling | 16 | Process media messages, extract context |
| Pending message queue activation | 16 | Wire `PendingMessage` model into 24-hour window logic |
| Per-tenant follow-up templates | 16 | Store templates in DB, not hardcoded |
| WhatsApp template message management | 17 | Manage approved templates per tenant |
| Read receipt tracking | 17 | Track message delivery and read status |
| Embedded Signup production enablement | RC2.5.19-E | Flag ON after Meta prerequisites and a live test tenant |
| Graph API upgrade from v21.0 | before 2027-01-21 | Separate authorised phase |

---

## 18. Related Documents

| Document | Relationship |
|----------|-------------|
| `AI_ARCHITECTURE.md` | `smart_reply()` and Gemini integration |
| `TENANT_ARCHITECTURE.md` | WABA isolation per tenant |
| `SYSTEM_ARCHITECTURE.md` | Blueprint and service layer context |
| `04_backend/SERVICES.md` | `whatsapp_service.py` API reference |
| `08_deployment/ENVIRONMENT_VARIABLES.md` | WABA env vars |

---

*Oxford CRM Documentation — docs/02_architecture/WHATSAPP_ARCHITECTURE.md*
*Source-verified against: `app/routes/webhook.py`, `app/services/whatsapp_service.py`, `app/services/followup_service.py`*
