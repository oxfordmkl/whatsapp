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
│  │  1. Parse payload                                        │    │
│  │  2. Extract phone_number_id                              │    │
│  │  3. Tenant lookup (WABA routing)                         │    │
│  │  4. Deduplication check                                  │    │
│  │  5. Opt-out / opt-in check                               │    │
│  │  6. smart_reply() → AI response                          │    │
│  │  7. Async: send reply                                    │    │
│  │  8. Async: log all events                                │    │
│  │  9. Async: schedule_followups() [if new lead]            │    │
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

**Source:** `app/routes/webhook.py`, lines 15–23

**Route:** `GET /webhook`

Meta sends a one-time verification request when the webhook is first configured:

```
GET /webhook?hub.mode=subscribe&hub.verify_token=oxford2026&hub.challenge=<random>

if mode == "subscribe" AND token == VERIFY_TOKEN:
    return challenge, 200  ← Confirms webhook ownership to Meta
else:
    return "Forbidden", 403
```

**`VERIFY_TOKEN`** is set via the `VERIFY_TOKEN` environment variable (default: `"oxford2026"`). This must be configured in the Meta Developer Portal to match.

---

## 5. Inbound Webhook — Message Processing

**Source:** `app/routes/webhook.py`, lines 26–240

**Route:** `POST /webhook`

### Step-by-Step Processing

```
POST /webhook (JSON payload from Meta)
│
├── Step 1: Parse JSON
│   data = request.get_json(silent=True) or {}
│   entry → changes → value → messages[], contacts[]
│
├── Step 2: Early exits
│   if "statuses" in value: return 200 (delivery/read receipt — ignore)
│   if not messages: return 200 (no message content)
│
├── Step 3: Extract message fields
│   from_number  = message.from
│   msg_type     = message.type
│   wamid        = message.id  (WhatsApp Message ID — for dedup)
│   contact_name = contacts[0].profile.name
│
├── Step 4: Multi-Tenant Routing (Phase 13-B4D2)
│   phone_number_id = value.metadata.phone_number_id
│   tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
│   if tenant:
│       if tenant.status not in [ACTIVE, TRIAL]: drop + return 200
│       tenant_id = tenant.id
│   else:
│       Grace fallback: if phone_number_id == config PHONE_NUMBER_ID → primary tenant
│       else: log warning + return 200 (unknown WABA)
│
├── Step 5: Deduplication
│   existing = ConversationMessage.query.filter_by(wa_message_id=wamid).first()
│   if existing: return 200 {"reason": "duplicate"}
│
├── Step 6: Message type parsing
│   text: msg_text = message.text.body
│   interactive (button_reply): msg_text = button_reply.id
│   interactive (list_reply): msg_text = list_reply.id
│   button: msg_text = message.button.text
│   other: return 200 (silently ignore)
│
├── Step 7: Opt-Out / Opt-In check
│   if msg == "stop/unsubscribe/cancel": set is_opted_out=True, return 200
│   if msg == "start/resume/unstop": set is_opted_out=False (continue processing)
│
├── Step 8: Lead detection
│   is_new_lead = not phone_exists(from_number, tenant_id=tenant_id)
│
├── Step 9: AI Processing (main thread)
│   reply_text, new_stage = smart_reply(msg_text, name, phone, is_new_lead, tenant_id)
│
├── Step 10: Async operations (daemon threads)
│   Thread: send_reply(from_number, reply_text, tenant_id)
│   Thread: log_message_in_thread(app, phone, "inbound", ...)
│   Thread: save_conversation_message_in_thread(app, phone, "incoming", msg_text, ...)
│   Thread: save_conversation_message_in_thread(app, phone, "outgoing", reply_text, ...)
│   Thread: log_lead_event_in_thread(app, phone, event_type, ...)
│   if is_new_lead:
│       Thread: save_lead_to_sheets(name, phone, ...)
│       Thread: schedule_followups(phone, name, tenant_id)
│
└── return jsonify({"status": "ok"}), 200 → Meta
```

---

## 6. Multi-Tenant Routing

**Source:** `app/routes/webhook.py`, lines 49–72 (Phase 13-B4D2)

This is the **core tenant isolation mechanism** for WhatsApp.

```python
phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()

if tenant:
    # Known WABA — route to this tenant
    if tenant.status not in ["ACTIVE", "TRIAL"]:
        # Tenant suspended/cancelled — drop the message silently
        return jsonify({"status": "ok"}), 200
    tenant_id = tenant.id

else:
    # Unknown WABA — check if it matches the primary Oxford phone number
    if phone_number_id == current_app.config.get("PHONE_NUMBER_ID"):
        tenant_id = current_app.config.get("PRIMARY_TENANT_ID")
        # Grace-period fallback for legacy/primary tenant
    else:
        # Completely unknown — drop message
        return jsonify({"status": "ok"}), 200
```

**Why this works:**
- Every tenant registers their unique `waba_phone_number_id` in the tenant portal
- Meta always includes the `phone_number_id` in the webhook metadata
- This `phone_number_id` uniquely maps to one tenant

---

## 7. Message Deduplication

**Source:** `app/routes/webhook.py`, lines 74–79 (Phase 11-D1 Task C)

Meta can occasionally deliver the same message twice (network retries). Deduplication prevents double-processing:

```python
if wamid:  # wamid = WhatsApp Message ID (e.g., "wamid.HBgLOTE...")
    existing = ConversationMessage.query.filter_by(wa_message_id=wamid).first()
    if existing:
        return jsonify({"status": "ok", "reason": "duplicate"}), 200
```

The `wa_message_id` is stored in the `ConversationMessage` record when the message is first logged. Subsequent deliveries of the same `wamid` are caught here and silently returned.

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

Each tenant configures their WABA credentials via the Tenant Portal at `/tenant/whatsapp`.

| Field | Description | Storage |
|-------|-------------|---------|
| `waba_phone_number_id` | Meta-assigned phone number ID | Plaintext in `tenants` table |
| `waba_access_token_encrypted` | Meta Graph API access token | Fernet-encrypted in `tenants` table |

### Credential Flow

```
Tenant Admin enters WABA credentials in /tenant/whatsapp
    │
    ▼ POST /tenant/whatsapp
token_encrypted = Fernet(WABA_ENCRYPTION_KEY).encrypt(token.encode())
tenant.waba_access_token_encrypted = token_encrypted
tenant.waba_phone_number_id = phone_number_id
db.session.commit()
    │
    ▼ Inbound webhook uses:
Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
    │
    ▼ Outbound uses:
token = Fernet(WABA_ENCRYPTION_KEY).decrypt(tenant.waba_access_token_encrypted)
```

---

## 14. Thread Safety Model

The webhook handler deliberately keeps AI processing in the **main request thread** and offloads I/O to daemon threads:

| Operation | Thread | Why |
|-----------|--------|-----|
| Tenant routing | Main | Needs result to continue |
| Deduplication check | Main | Must block before processing |
| Opt-out/opt-in check | Main | State affects further processing |
| `smart_reply()` | Main | Reply text needed for send |
| `send_reply()` | Daemon | I/O — don't block main thread |
| `log_message()` | Daemon | I/O — non-critical to response time |
| `save_conversation_message()` | Daemon | I/O — non-critical |
| `schedule_followups()` | Daemon | I/O — non-critical |

Meta requires `200 OK` within 20 seconds. The main thread completes in < 500ms (just the `smart_reply()` Gemini call). All database writes happen asynchronously.

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
| Grace-period fallback uses config `PHONE_NUMBER_ID` | Won't work cleanly in multi-tenant future | Phase 16 |

---

## 17. Future Roadmap

| Feature | Phase | Description |
|---------|-------|-------------|
| Image/audio message handling | 16 | Process media messages, extract context |
| Pending message queue activation | 16 | Wire `PendingMessage` model into 24-hour window logic |
| Per-tenant follow-up templates | 16 | Store templates in DB, not hardcoded |
| WhatsApp template message management | 17 | Manage approved templates per tenant |
| Read receipt tracking | 17 | Track message delivery and read status |

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
