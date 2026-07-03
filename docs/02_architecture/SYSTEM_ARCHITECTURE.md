# Oxford CRM — System Architecture
## Complete System Design Reference

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Audience:** Engineers, AI Assistants, Technical Architects
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 16.0
> **Source Authority:** Verified against `app/__init__.py`, `app/routes/`, `app/services/`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [High-Level System Overview](#2-high-level-system-overview)
3. [Component Diagram](#3-component-diagram)
4. [Layered Architecture](#4-layered-architecture)
5. [Flask Application Architecture](#5-flask-application-architecture)
6. [Request Lifecycle](#6-request-lifecycle)
7. [Blueprint Organization](#7-blueprint-organization)
8. [Service Layer Architecture](#8-service-layer-architecture)
9. [Worker Architecture](#9-worker-architecture)
10. [Current Production Topology](#10-current-production-topology)
11. [Known Limitations](#11-known-limitations)
12. [Future Modularization Strategy](#12-future-modularization-strategy)
13. [Related Documents](#13-related-documents)

---

## 1. Purpose and Scope

This document describes the **complete technical architecture** of Oxford CRM as it exists in production today. It covers the Flask application structure, blueprint organization, service interactions, worker architecture, and current deployment topology.

**Scope:** Kerala Production Candidate v1.0 — single tenant deployment.
**Out of Scope:** Mobile app, LMS, Student Portal (future phases).

---

## 2. High-Level System Overview

Oxford CRM is an **Enterprise Multi-Tenant SaaS CRM** built on Python Flask. It serves as a complete business operations platform for education institutions.

**Primary Functions:**
1. Receive inbound WhatsApp messages from leads
2. Reply automatically using Google Gemini AI
3. Track leads through a CRM pipeline
4. Automate follow-up sequences
5. Enable staff to manage, assign, and respond to leads
6. Provide analytics and revenue intelligence to managers
7. Support marketing broadcast campaigns
8. Manage billing and subscriptions

**Technology Stack:**

| Layer | Technology |
|-------|-----------|
| Language | Python 3.x |
| Framework | Flask (with blueprints) |
| Database | PostgreSQL (Railway-managed) |
| ORM | SQLAlchemy + Flask-Migrate (Alembic) |
| Auth | Flask-Login |
| AI | Google Gemini 2.0 Flash (via `google-genai` SDK) |
| Messaging | Meta WhatsApp Cloud API |
| Billing | Razorpay (foundation-level) |
| Hosting | Railway |
| WSGI | Gunicorn (`Procfile`) |
| Templates | Jinja2 (Flask built-in) |

---

## 3. Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL SYSTEMS                            │
│                                                                     │
│  ┌─────────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ WhatsApp (Meta) │  │ Google Gemini│  │ Razorpay             │  │
│  │ Cloud API       │  │ 2.0 Flash    │  │ Billing (Foundation) │  │
│  └────────┬────────┘  └──────┬───────┘  └──────────┬───────────┘  │
│           │                  │                       │              │
└───────────┼──────────────────┼───────────────────────┼─────────────┘
            │ HTTP             │ HTTPS API             │ Webhook HTTP
            ▼                  ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       OXFORD CRM (Railway)                          │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                  Flask Application (Gunicorn)                 │  │
│  │                                                              │  │
│  │  ┌─────────────┐ ┌───────────┐ ┌──────────┐ ┌───────────┐  │  │
│  │  │ webhook_bp  │ │ admin_bp  │ │ tenant_bp│ │billing_bp │  │  │
│  │  │ /webhook    │ │ /crm/*    │ │ /tenant/*│ │/webhooks/*│  │  │
│  │  └──────┬──────┘ └─────┬─────┘ └────┬─────┘ └─────┬─────┘  │  │
│  │         │              │             │              │         │  │
│  │  ┌──────▼──────────────▼─────────────▼──────────────▼──────┐ │  │
│  │  │              Service Layer                                │ │  │
│  │  │  whatsapp_service │ log_service │ crm_service │ ai_service│ │  │
│  │  └──────────────────────────┬──────────────────────────────┘ │  │
│  │                             │                                 │  │
│  │  ┌──────────────────────────▼──────────────────────────────┐ │  │
│  │  │              bot/router.py  (AI Engine)                 │ │  │
│  │  │              smart_reply() → gemini_reply()             │ │  │
│  │  └─────────────────────────────────────────────────────────┘ │  │
│  │                             │                                 │  │
│  │  ┌──────────────────────────▼──────────────────────────────┐ │  │
│  │  │              Follow-Up Worker (Daemon Thread)           │ │  │
│  │  │              _followup_worker() — polls every 5 min     │ │  │
│  │  └─────────────────────────────────────────────────────────┘ │  │
│  │                                                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                   PostgreSQL Database                        │  │
│  │   tenants │ users │ conversation_state │ conversation_message│  │
│  │   message_log │ lead_event │ follow_up_jobs │ billing_invoices│  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Layered Architecture

Oxford CRM is organized into 5 clear layers:

```
┌─────────────────────────────────────┐
│   Layer 5: Presentation             │
│   Jinja2 Templates (/templates/)    │
│   HTML + CSS + JavaScript           │
└─────────────────────────────────────┘
┌─────────────────────────────────────┐
│   Layer 4: Route Handlers (Blueprints)│
│   admin_bp, tenant_bp, webhook_bp   │
│   billing_bp, broadcast_bp          │
└─────────────────────────────────────┘
┌─────────────────────────────────────┐
│   Layer 3: Service Layer            │
│   whatsapp_service, log_service     │
│   crm_service, ai_service           │
└─────────────────────────────────────┘
┌─────────────────────────────────────┐
│   Layer 2: AI / Bot Engine          │
│   bot/router.py (smart_reply)       │
│   bot/prompts.py, bot/constants.py  │
└─────────────────────────────────────┘
┌─────────────────────────────────────┐
│   Layer 1: Data Layer               │
│   SQLAlchemy Models + PostgreSQL    │
│   Flask-Migrate (Alembic)           │
└─────────────────────────────────────┘
```

---

## 5. Flask Application Architecture

### Application Factory

The application is created using the **factory pattern** in `app/__init__.py` via `create_app()`. This enables testing isolation and multiple configurations.

**Initialization sequence inside `create_app()`:**
```
1. Flask(__name__, template_folder=...)
2. app.config["SECRET_KEY"] = SECRET_KEY
3. app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
4. app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {pool_pre_ping, pool_recycle}
5. app.config["WABA_ENCRYPTION_KEY"] = ... (fail-fast if missing)
6. db.init_app(app)
7. migrate.init_app(app, db)
8. LoginManager().init_app(app)  ← Flask-Login
9. login_manager.login_view = 'admin.crm_login'
10. from app import models  ← registers models with Alembic
11. @login_manager.user_loader → User.query.get(user_id)
12. @app.cli.command("seed-superadmin")
13. Register all 7 blueprints
14. init_followup_service(app)  ← starts background daemon thread
15. return app
```

### Entry Point

```
run.py → create_app() → Gunicorn (Procfile: web: gunicorn run:app)
```

### Database Connection Configuration

```python
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,    # Detects stale connections (Railway reconnect)
    "pool_recycle":  1800,    # Recycles connections every 30 minutes
}
```

The `pool_pre_ping` option is critical for Railway's PostgreSQL, which drops idle connections.

---

## 6. Request Lifecycle

### Web Request (CRM Dashboard)

```
Browser
  │
  ▼
Railway (HTTPS load balancer)
  │
  ▼
Gunicorn (WSGI server)
  │
  ▼
Flask (create_app())
  │
  ├── Flask-Login @before_request → current_user populated
  ├── tenant_bp @before_request  → billing status check (for /tenant/ routes)
  │
  ▼
Blueprint Router (admin_bp, tenant_bp, etc.)
  │
  ▼
Route Handler Function
  │
  ├── @login_required / @super_admin_required / @tenant_admin_required
  ├── tenant_query(Model, tenant_id) → scoped DB query
  ├── Service calls if needed
  │
  ▼
render_template(...)  OR  redirect(...)  OR  jsonify(...)
  │
  ▼
HTTP Response → Browser
```

### Inbound WhatsApp Request

```
Meta WhatsApp Cloud API
  │
  ▼ POST /webhook
webhook_bp.receive_message()
  │
  ├── Parse JSON payload
  ├── Extract phone_number_id → tenant routing
  ├── Tenant.query.filter_by(waba_phone_number_id=...) → tenant_id
  ├── Deduplication check (wa_message_id)
  ├── Opt-out / opt-in check
  ├── is_new_lead = not phone_exists(phone, tenant_id)
  │
  ├── smart_reply(msg, name, phone, is_new_lead, tenant_id) [main thread]
  │     └── returns (reply_text, new_stage)
  │
  ├── send_reply(phone, reply_text, tenant_id) [daemon thread]
  ├── log_message_in_thread(...) [daemon thread]
  ├── save_conversation_message_in_thread(...) [daemon thread]
  ├── schedule_followups(phone, name, tenant_id) [if new lead]
  │
  ▼
return jsonify({"status": "ok"}), 200 → Meta
```

---

## 7. Blueprint Organization

All blueprints are registered in `create_app()`. Blueprint registration order matters — routes are matched in registration order.

| Blueprint | Python Module | URL Prefix | Primary Role |
|-----------|-------------|-----------|-------------|
| `public_bp` | `app/routes/public.py` | (none) | Registration, public pages |
| `webhook_bp` | `app/routes/webhook.py` | (none) | `/webhook` (WhatsApp) |
| `admin_bp` | `app/routes/admin.py` | (none) | `/crm/*`, auth, leads, analytics |
| `broadcast_bp` | `app/routes/broadcast.py` | (none) | Marketing campaigns |
| `health_bp` | `app/routes/health.py` | (none) | `/health` check endpoint |
| `tenant_bp` | `app/routes/tenant.py` | `/tenant` | `/tenant/*` portal |
| `billing_bp` | `app/routes/billing.py` | `/webhooks` | `/webhooks/razorpay`, `/webhooks/stripe` |

**Important:** `admin_bp` has no URL prefix. All `/crm/*` routes are defined inside it with full paths (e.g., `@admin_bp.route('/crm/login')`).

### admin.py Scale Warning

`app/routes/admin.py` is **4,848 lines** as of Phase 15A. It contains:
- Authentication routes
- All lead management routes
- All staff management routes
- All analytics routes
- Super Admin routes
- Tenant utility functions (`tenant_query`, `tenant_filter`, billing guards)

This is documented technical debt (TD-001). It must be modified with extreme surgical care.

---

## 8. Service Layer Architecture

The service layer provides reusable business logic functions called from route handlers.

| Service | File | Functions |
|---------|------|----------|
| WhatsApp | `app/services/whatsapp_service.py` | `send_text()`, `send_reply()`, `send_automation()` |
| Log | `app/services/log_service.py` | `log_message()`, `save_conversation_message()`, `log_lead_event()` |
| CRM | `app/services/crm_service.py` | `update_lead_status()`, `save_lead_to_sheets()` |
| AI | `app/services/ai_service.py` | `gemini_reply()`, `smart_fallback()` |
| Follow-Up | `app/services/followup_service.py` | `schedule_followups()`, `init_followup_service()` |

### Thread Safety

Many service functions are called from **daemon threads**. Functions ending in `_in_thread` are designed for this pattern:

```python
# Pattern: offload I/O-bound work to daemon threads with app context
_app = current_app._get_current_object()
threading.Thread(
    target=log_message_in_thread,
    args=(_app, phone, ...),
    daemon=True,
).start()
```

This keeps the WhatsApp webhook response fast (< 200ms) while logging happens asynchronously.

---

## 9. Worker Architecture

### Follow-Up Scheduler

The only background worker is the **follow-up scheduler**, implemented as a daemon thread.

**Initialization:** `init_followup_service(app)` is called once at the end of `create_app()`.

**Architecture:**
```
create_app()
  └── init_followup_service(app)
        ├── Stores app reference globally (_app = app)
        └── threading.Thread(target=_followup_worker, daemon=True).start()
```

**Worker Loop:**
```
_followup_worker():
  while True:
    with _app.app_context():
      pending = FollowUpJob.query.filter_by(done=False)
                          .filter(FollowUpJob.send_at <= now).all()

      for job in pending:
        1. Skip if lead is opted out (is_opted_out == True)
        2. Skip if lead was active in last 6 hours (last_msg recency check)
        3. send_automation(job.phone, job.message, tenant_id=job.tenant_id)
        4. log_message(..., tenant_id=job.tenant_id)
        5. save_conversation_message(..., tenant_id=job.tenant_id)
        6. job.done = True; db.session.commit()

    time.sleep(300)  ← polls every 5 minutes
```

**Retry Logic:**
- On failure: `retry_count += 1`
- Exponential backoff: `send_at = now + timedelta(minutes=15 * retry_count)`
- Max retries: 3 (then `done = True` — permanently failed)

**Follow-Up Templates (3 messages):**
- Day 1 (24 hours): Initial re-engagement
- Day 3 (72 hours): Urgency message (next batch filling)
- Day 7 (168 hours): Final opportunity message

**Thread Safety:** The worker opens its own `_app.app_context()` per cycle, which gives it a fresh SQLAlchemy session and avoids cross-request state contamination.

---

## 10. Current Production Topology

```
┌─────────────────────────────────────────────────────────┐
│                    Railway Platform                      │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │  Oxford CRM Service (Gunicorn)                  │   │
│  │  Python 3.x, Flask, SQLAlchemy                  │   │
│  │                                                  │   │
│  │  Workers: 1 (default Gunicorn)                  │   │
│  │  Follow-Up Thread: 1 daemon thread              │   │
│  └──────────────────────┬──────────────────────────┘   │
│                         │                               │
│  ┌──────────────────────▼──────────────────────────┐   │
│  │  PostgreSQL Service (Railway managed)            │   │
│  │  DATABASE_URL auto-injected by Railway           │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  Environment Variables (Railway config):                │
│  SECRET_KEY, ADMIN_KEY, BROADCAST_API_KEY              │
│  GEMINI_API_KEY, DATABASE_URL                           │
│  PHONE_NUMBER_ID, ACCESS_TOKEN, VERIFY_TOKEN            │
│  WABA_ENCRYPTION_KEY                                    │
└─────────────────────────────────────────────────────────┘
       │                    │
       ▼                    ▼
  Meta WABA API        Google Gemini API
  (WhatsApp)           (AI replies)
```

**Single Tenant Reality (Kerala):** One active tenant (Oxford Computers). All `tenant_id` values point to the same UUID for the Oxford Computers record.

---

## 11. Known Limitations

| Limitation | Impact | Resolution |
|-----------|--------|-----------|
| `admin.py` is 4,848 lines (TD-001) | High regression risk per edit | Modularize in Phase 16 |
| Single Gunicorn worker | Cannot horizontally scale the follow-up thread | Scale in Phase 16 |
| Follow-up templates are hardcoded in `followup_service.py` | Cannot be customized per tenant | Template per tenant in Phase 16 |
| `ai_prompt_override` stored in DB but not applied in `gemini_reply()` | Per-tenant prompt not active in AI fallback path | See AI_ARCHITECTURE.md |
| No background job queue (Celery/Redis) | Follow-up worker is a daemon thread — not fault-tolerant on restart | Phase 16+ |

---

## 12. Future Modularization Strategy

### Phase 16 — `admin.py` Decomposition

Target: Split `admin.py` into logical route modules:
- `app/routes/crm/leads.py`
- `app/routes/crm/staff.py`
- `app/routes/crm/analytics.py`
- `app/routes/crm/campaigns.py`
- `app/routes/crm/super_admin.py`

Each will register under a sub-blueprint merged into `admin_bp`.

### Phase 16 — Celery + Redis Background Jobs

Replace the daemon thread follow-up worker with a Celery task queue backed by Redis, enabling:
- Fault-tolerant retry on server restart
- Horizontal scaling
- Job monitoring UI

### Phase 17+ — Microservice Considerations

At scale (100+ tenants), the WhatsApp webhook and AI processing layer may be extracted into a dedicated microservice. This is planned but not yet designed.

---

## 13. Related Documents

| Document | Relationship |
|----------|-------------|
| `TENANT_ARCHITECTURE.md` | Tenant isolation details |
| `AUTHENTICATION_ARCHITECTURE.md` | Login and session details |
| `WHATSAPP_ARCHITECTURE.md` | Webhook and messaging details |
| `AI_ARCHITECTURE.md` | Gemini integration details |
| `BILLING_ARCHITECTURE.md` | Billing layer details |
| `04_backend/BLUEPRINTS.md` | Blueprint route catalog |
| `04_backend/SERVICES.md` | Service layer API reference |
| `17_ai_context/AI_MEMORY.md` | Project-level quick reference |

---

*Oxford CRM Documentation — docs/02_architecture/SYSTEM_ARCHITECTURE.md*
*Source-verified against: `app/__init__.py`, `app/routes/`, `app/services/`, `app/bot/`*
