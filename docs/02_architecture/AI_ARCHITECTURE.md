# Oxford CRM — AI Architecture
## Gemini Integration, Conversation Engine, and Tenant AI Isolation

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Audience:** Engineers, AI Assistants, Product Team
> **Last Updated:** 2026-07-02 | **Next Review:** Phase 16
> **Source Authority:** Verified against `app/bot/router.py`, `app/services/ai_service.py`, `app/bot/prompts.py`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [AI Architecture Overview](#2-ai-architecture-overview)
3. [Gemini Integration](#3-gemini-integration)
4. [AI Request Lifecycle](#4-ai-request-lifecycle)
5. [The `smart_reply()` Engine](#5-the-smart_reply-engine)
6. [Conversation State Machine](#6-conversation-state-machine)
7. [Keyword Routing Layer](#7-keyword-routing-layer)
8. [AI Fallback Layer](#8-ai-fallback-layer)
9. [Objection Handling](#9-objection-handling)
10. [Tenant AI Isolation](#10-tenant-ai-isolation)
11. [Per-Tenant AI Persona](#11-per-tenant-ai-persona)
12. [AI Prompt Architecture](#12-ai-prompt-architecture)
13. [Event Scoring Integration](#13-event-scoring-integration)
14. [Current Production Status](#14-current-production-status)
15. [Known Limitations](#15-known-limitations)
16. [Future AI Roadmap](#16-future-ai-roadmap)
17. [Related Documents](#17-related-documents)

---

## 1. Purpose and Scope

This document describes the AI architecture of Oxford CRM — how Google Gemini is integrated, how the conversation engine routes messages between keyword rules and AI, and how per-tenant AI configuration is designed.

---

## 2. AI Architecture Overview

```
┌────────────────────────────────────────────────────────────────────┐
│                        AI Architecture                             │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │              smart_reply() — Main Entry Point               │   │
│  │              app/bot/router.py                             │   │
│  │                                                            │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  Layer 1: Special Command Router                     │ │   │
│  │  │  "exit", "demo", "courses", "fees", greeting words   │ │   │
│  │  │  → Exact match → Pre-built response function         │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  │                                                            │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  Layer 2: Objection Handler                          │ │   │
│  │  │  "too expensive", "not now", "already enrolled"      │ │   │
│  │  │  → detect_objection() → handle_objection()           │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  │                                                            │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  Layer 3: Stage-Based Router                         │ │   │
│  │  │  Current conversation stage determines flow          │ │   │
│  │  │  GOAL → COURSE → DEMO → PAYMENT                      │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  │                                                            │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  Layer 4: Gemini AI Fallback                         │ │   │
│  │  │  Unrecognized message → gemini_reply()               │ │   │
│  │  │  → google-genai SDK → gemini-2.0-flash               │ │   │
│  │  │  → Returns natural language reply                    │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  │                                                            │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  Layer 5: smart_fallback()                           │ │   │
│  │  │  Gemini unavailable / quota exceeded                 │ │   │
│  │  │  → Context-aware hardcoded response                  │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  └────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. Gemini Integration

**Source:** `app/services/ai_service.py`

### SDK and Model

```python
from google import genai
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
# Model: gemini-2.0-flash (fast, cost-efficient, multilingual)
```

**Initialization:** The Gemini client is initialized once at module import time. If `GEMINI_API_KEY` is not set, `gemini_client = None` and all AI replies are disabled (fallback to `smart_fallback()`).

### `gemini_reply()` Function

```python
def gemini_reply(user_msg: str, name: str, context: str = "") -> str | None:
    """
    Calls Gemini 2.0 Flash with:
    - The AALIZA_PROMPT as system context
    - Optional conversation history (context)
    - The student's name
    - The current message

    Returns: AI-generated reply text, or None on error/quota
    """
    if not gemini_client:
        return None  # AI disabled — route to smart_fallback()

    prompt = (
        f"{AALIZA_PROMPT}\n\n"
        f"{'Conversation so far:\n' + context + '\n' if context else ''}"
        f"Student name: {name}\n"
        f"Student says: \"{user_msg}\"\n\n"
        f"Reply as Oxford Nova:"
    )

    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    return response.text.strip()
```

### Error Handling

| Error Condition | Response |
|----------------|---------|
| `GEMINI_API_KEY` not set | Returns `None` → `smart_fallback()` used |
| HTTP 429 (quota exceeded) | Logs warning, returns `None` → fallback |
| Any other exception | Logs error, returns `None` → fallback |

---

## 4. AI Request Lifecycle

```
Inbound message: "I want to learn Python"
    │
    ▼
webhook_bp.receive_message()
    ├── Tenant routing → tenant_id resolved
    └── smart_reply("I want to learn Python", "Rahul", "9194...", False, tenant_id)
        │
        ▼ router.py
        ├── Load conversation state from DB (stage, course, goal, last_msg)
        ├── Check special commands → no match
        ├── Check objection keywords → no match
        ├── Check keyword patterns → "python" → course detail response
        │       └── return (course_detail_text, "COURSE")
        │
        OR (if no keyword match)
        │
        ├── Fall to Gemini:
        │   context = recent conversation messages from ConversationMessage
        │   reply = gemini_reply("I want to learn Python", "Rahul", context)
        │   if reply: return (reply, current_stage)
        │   else: return (smart_fallback("Rahul", msg), current_stage)
        │
        ▼
    reply_text = "✅ Python / Full Stack Development — nalla choice!"
    new_stage = "COURSE"
        │
        ▼
    Thread: send_reply("9194...", reply_text, tenant_id)
    Thread: log all events (message_log, conversation_message, lead_event)
```

---

## 5. The `smart_reply()` Engine

**Source:** `app/bot/router.py`, lines 203–475

`smart_reply()` is the **master AI dispatch function**. It is the single entry point called from `webhook_bp.receive_message()`.

**Signature:**
```python
def smart_reply(
    msg_text: str,   # The message text from the lead
    name: str,       # Lead's WhatsApp display name
    phone: str,      # Lead's phone number (tenant-scoped key)
    is_new_lead: bool, # True if first-ever message from this number
    tenant_id: str = None
) -> tuple[str, str | None]:
    # Returns: (reply_text, new_conversation_stage)
```

**Tenant resolution:**
```python
if tenant_id is None:
    from app.services.log_service import _get_default_tenant_id
    tenant_id = _get_default_tenant_id()  # Fallback for single-tenant
```

**State loading:**
```python
st = _state(phone, name, tenant_id=tenant_id)
# Loads or creates ConversationState row for this phone+tenant
# Returns a StateProxy with auto-save on mutation
stage  = st["stage"]   # current pipeline stage
course = st["course"]  # currently selected course
```

---

## 6. Conversation State Machine

The AI engine maintains a **conversation state** for each lead in `ConversationState`.

### Stages

| Stage | Meaning | Next Stage |
|-------|---------|-----------|
| `new` | No conversation yet | `goal_selection` on any message |
| `goal_selection` | Asking lead about their goal | `COURSE` on goal reply |
| `GOAL` | Goal captured | `COURSE` on course selection |
| `COURSE` | Course presented | `demo_time_ask` on demo request |
| `demo_time_ask` | Asking preferred demo time | `demo_date_ask` on time reply |
| `demo_date_ask` | Asking preferred demo date | `enrolled` on date confirmed |
| `enrolled` | Demo booked | `PAYMENT` on payment intent |
| `PAYMENT` / `payment_pending` | Payment link sent | `done` on confirmation |
| `offer_menu` | Offer presented | varies |
| `done` | Conversation complete | `goal_selection` on new greeting |

### Stage Transitions

Transitions are driven by message content. For example:
```python
# New lead always gets welcome message
if is_new_lead:
    st["stage"] = "goal_selection"
    return msg_welcome(name)

# Greetings from completed conversations restart the flow
if low in GREETING_WORDS and stage in ("new", "done", "enrolled", "goal_selection"):
    st["stage"] = "goal_selection"
    return msg_welcome(name)
```

State is persisted to the `ConversationState` DB table via the `StateProxy` object, which auto-saves on assignment.

---

## 7. Keyword Routing Layer

Before Gemini is invoked, `smart_reply()` checks for **exact keyword matches** that map to predefined responses.

### Keyword Categories

| Category | Keywords | Response Function |
|----------|---------|-----------------|
| Exit | `"exit"` | `msg_exit()` — farewell message |
| Greetings | `"hi", "hello", "hai", "hey", "namaskaram"`, etc. | `msg_welcome()` |
| Demo request | `"demo", "free demo", "free class", "book demo"` | `msg_demo_time_ask()` |
| Payment | `"pay", "payment", "enrol", "enroll", "seat"` | `msg_offer_menu()` or payment link |
| Enroll now | `"enroll_now", "enrol_now", "pay_now"` | `msg_payment_link()` |
| Fees | `"fees", "fee", "price", "cost", "ethra", "how much"` | Fee table + event log |
| Placement | `"placement", "job", "work opportunity"` | Placement info |
| Courses | `"courses", "course list"` | Course catalogue |
| Offers | `"offer", "discount", "today offer"` | Offer menu |
| Visit | `"visit", "office", "varam", "address"` | Location info |
| Call | `"call me", "call", "counselor"` | Phone number |

### Stage-Based Routing

After keyword matching, the engine also routes based on the **current stage**:
```python
if stage == "demo_time_ask":
    # Parse "1", "2", "3" → Morning/Afternoon/Evening
    ...
elif stage == "demo_date_ask":
    # Any text → demo date confirmed
    ...
elif stage == "goal_selection":
    # Parse "1"–"5" → goal category
    ...
```

---

## 8. AI Fallback Layer

If no keyword or stage match is found, the message is sent to Gemini:

```python
# From router.py (final fallback block):
context = ""  # Future: load recent conversation history from DB
reply = gemini_reply(msg_text, name, context=context)
if reply:
    return reply, stage
return smart_fallback(name, msg_text), stage
```

### `smart_fallback()` Responses

When Gemini is unavailable (quota, no API key), `smart_fallback()` returns contextual hardcoded responses:

| Message contains | Response |
|-----------------|---------|
| "fee", "price", "cost" | Fee range info (₹1,999 onwards) + contact |
| "job", "placement" | Placement assistance info + contact |
| Anything else | General counselor intro + COURSES/DEMO/FEES options |

---

## 9. Objection Handling

**Source:** `app/bot/objections.py`

Before keyword routing, `smart_reply()` checks for objection patterns:

```python
objection = detect_objection(low)
if objection:
    return handle_objection(objection, name, st)
```

Objections are phrases like:
- "too expensive" / "costly"
- "not now" / "later" / "busy"
- "already enrolled elsewhere"
- "not interested"

Each objection has a specific counter-response designed to address the concern and re-engage the lead.

---

## 10. Tenant AI Isolation

Each tenant's AI context is fully isolated:

| Isolation Layer | Mechanism |
|-----------------|----------|
| Conversation state | `ConversationState` rows are scoped by `tenant_id` + `phone` composite |
| Conversation history | `ConversationMessage` rows are scoped by `tenant_id` |
| Follow-up scheduling | `FollowUpJob` rows are scoped by `tenant_id` |
| AI persona name | `Tenant.ai_persona_name` — per-tenant bot name |
| AI system prompt | `Tenant.ai_prompt_override` — per-tenant custom prompt |

A lead conversation on Tenant A's WhatsApp number can never see or affect Tenant B's conversation state.

---

## 11. Per-Tenant AI Persona

Each tenant can customize their AI bot:

| Field | Default | Configurable Via |
|-------|---------|-----------------|
| `ai_persona_name` | "Oxford Nova" (system default) | Tenant Portal → AI Settings |
| `ai_prompt_override` | `AALIZA_PROMPT` (from `bot/prompts.py`) | Tenant Portal → AI Settings |

### Current Implementation Gap

**⚠️ Important:** As of Phase 15A, `ai_persona_name` and `ai_prompt_override` are:
- ✅ Stored in the database
- ✅ Configurable via `/tenant/ai` form
- ❌ **NOT yet applied in `gemini_reply()`**

The `gemini_reply()` function currently uses only the global `AALIZA_PROMPT`:
```python
prompt = f"{AALIZA_PROMPT}\n\n..."  # ← Always uses global prompt
```

The per-tenant prompt override and persona name are available in the database but have not yet been wired into the AI call path. This will be resolved in Phase 16.

---

## 12. AI Prompt Architecture

**Source:** `app/bot/prompts.py`

The system uses a single global prompt (`AALIZA_PROMPT`) that defines the AI persona:

- Persona: Oxford Nova — a friendly Malayalam-speaking AI counselor for The Oxford Computers
- Language: Responds in Malayalam (Manglish) with some English
- Tone: Friendly, helpful, warm
- Goal: Guide leads toward demo booking and course enrollment
- Constraints: Does not discuss competitors, stays on-topic

**Prompt Structure:**
```
[Persona declaration]
[Tone guidelines]
[Language preference: Malayalam/Manglish]
[Topic constraints]
[Oxford Computers information (courses, fees, placement)]
[Response style guidelines]
```

---

## 13. Event Scoring Integration

The AI engine fires lead events that feed the **intelligence scoring system**:

```python
# From router.py — events fired via daemon threads:
log_lead_event_in_thread(app, phone, "DEMO_REQUESTED", tenant_id)
log_lead_event_in_thread(app, phone, "FEES_REQUESTED", tenant_id)
log_lead_event_in_thread(app, phone, "COURSE_VIEWED", tenant_id)
log_lead_event_in_thread(app, phone, "PLACEMENT_ASKED", tenant_id)
```

**Event → Score mapping** (from `admin.py`):

| Event | Score Added |
|-------|------------|
| `LEAD_CREATED` | +2 |
| `FIRST_MESSAGE_RECEIVED` | +3 |
| `AI_RESPONSE_SENT` | +5 |
| `COURSE_VIEWED` | +10 |
| `PLACEMENT_ASKED` | +15 |
| `FEES_REQUESTED` | +20 |
| `DEMO_REQUESTED` | +25 |
| `PAYMENT_PENDING` | +30 |

Leads are scored automatically as they interact with the AI. Leads scoring ≥ 80 are `HOT`, ≥ 50 are `WARM`.

---

## 14. Current Production Status

| Component | Status |
|-----------|--------|
| Gemini 2.0 Flash integration | ✅ Active |
| `smart_reply()` engine | ✅ Active |
| Keyword routing | ✅ Active |
| Objection handling | ✅ Active |
| Stage-based conversation flow | ✅ Active |
| `gemini_reply()` AI fallback | ✅ Active |
| `smart_fallback()` hardcoded fallback | ✅ Active |
| Lead event scoring | ✅ Active |
| `ai_persona_name` (stored) | ✅ Stored |
| `ai_prompt_override` (stored) | ✅ Stored |
| `ai_prompt_override` (applied in Gemini calls) | ❌ Not yet wired |
| Per-tenant Gemini prompt | ❌ Not yet wired |
| Conversation history context in Gemini | ❌ Not yet passed (context="" always) |

---

## 15. Known Limitations

| Limitation | Impact | Resolution Phase |
|-----------|--------|-----------------|
| `ai_prompt_override` not applied in `gemini_reply()` | All tenants use Oxford Nova prompt | Phase 16 |
| No conversation history passed to Gemini | Gemini has no memory of prior turns within session | Phase 16 |
| AI engine is Oxford-specific (course names, fees hardcoded in `bot/constants.py`) | Cannot be used as-is for different industries | Phase 16 |
| No rate limiting per tenant for AI calls | One tenant's Gemini quota could starve others | Phase 16 |
| `smart_reply()` only handles single-turn messages | No multi-turn context window | Phase 16 |

---

## 16. Future AI Roadmap

| Feature | Phase | Description |
|---------|-------|-------------|
| Apply `ai_prompt_override` in Gemini calls | 16 | Wire `tenant.ai_prompt_override` into `gemini_reply()` |
| Pass conversation history to Gemini | 16 | Load last N messages from `ConversationMessage` as context |
| Per-tenant course catalogue in AI | 16 | Make `bot/constants.py` data tenant-configurable |
| AI persona marketplace | 20 | Tenants choose from pre-built industry personas |
| AI lead scoring (predictive) | 20 | ML model to predict conversion probability |
| AI follow-up suggestions | 20 | Recommend next action for each lead |
| Natural language analytics | 20 | Query analytics dashboard via conversation |

---

## 17. Related Documents

| Document | Relationship |
|----------|-------------|
| `WHATSAPP_ARCHITECTURE.md` | How AI is invoked from webhook handler |
| `TENANT_ARCHITECTURE.md` | Tenant AI isolation details |
| `04_backend/SERVICES.md` | `ai_service.py` function reference |
| `08_deployment/ENVIRONMENT_VARIABLES.md` | `GEMINI_API_KEY` configuration |

---

*Oxford CRM Documentation — docs/02_architecture/AI_ARCHITECTURE.md*
*Source-verified against: `app/bot/router.py`, `app/services/ai_service.py`, `app/bot/prompts.py`, `app/models.py`*
