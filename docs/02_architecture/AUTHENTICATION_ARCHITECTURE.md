# Oxford CRM — Authentication Architecture
## Login Flows, Sessions, Roles, and Route Protection

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Security Team
> **Audience:** Engineers, AI Assistants, Security Reviewers
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 15C
> **Source Authority:** Verified against `app/__init__.py`, `app/routes/admin.py` (lines 4640–4848)

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Authentication Overview](#2-authentication-overview)
3. [User Model](#3-user-model)
4. [Flask-Login Configuration](#4-flask-login-configuration)
5. [Login Gateway 1 — Admin/Staff (`/crm/login`)](#5-login-gateway-1--adminstaff-crmlogin)
6. [Login Gateway 2 — Super Admin (`/crm/super/login`)](#6-login-gateway-2--super-admin-crmsuperlogin)
7. [Logout Flow](#7-logout-flow)
8. [Session Lifecycle](#8-session-lifecycle)
9. [Role Definitions](#9-role-definitions)
10. [Permission Matrix](#10-permission-matrix)
11. [Route Protection Decorators](#11-route-protection-decorators)
12. [Password Change Flow](#12-password-change-flow)
13. [Super Admin Bootstrap](#13-super-admin-bootstrap)
14. [Deep Link (`?next=`) Handling](#14-deep-link-next-handling)
15. [Cache Control — Session Hardening](#15-cache-control--session-hardening)
16. [Current Production Status](#16-current-production-status)
17. [Known Limitations](#17-known-limitations)
18. [Future Authentication Roadmap](#18-future-authentication-roadmap)
19. [Related Documents](#19-related-documents)

---

## 1. Purpose and Scope

This document describes how Oxford CRM authenticates users, manages sessions, and enforces role-based access. Every detail is verified against the production source code.

**Current roles:** `SUPER_ADMIN`, `ADMIN`, `STAFF`
**Planned roles:** `READ_ONLY` (deferred to Phase 15C)

---

## 2. Authentication Overview

Oxford CRM uses **Flask-Login** for session-based authentication. There are two separate login gateways — one for tenant users (ADMIN/STAFF) and one for the platform super administrator.

```
┌─────────────────────────────────────────────────────────┐
│                  Authentication Layer                    │
│                                                         │
│  Gateway 1: /crm/login                                  │
│  Users: ADMIN, STAFF (and SUPER_ADMIN as fallback)      │
│  Login field: username + password                       │
│                                                         │
│  Gateway 2: /crm/super/login                            │
│  Users: SUPER_ADMIN only                                │
│  Login field: email + password                          │
│                                                         │
│  Session: Flask-Login (server-side session cookie)      │
│  Token: Flask SECRET_KEY (validated at boot)            │
└─────────────────────────────────────────────────────────┘
```

---

## 3. User Model

**Source:** `app/models.py` — `class User(UserMixin, db.Model):`

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `username` | String(64) | Login identifier — unique per tenant |
| `password_hash` | String(256) | Werkzeug PBKDF2 hash |
| `role` | String(20) | `SUPER_ADMIN` \| `ADMIN` \| `STAFF` |
| `is_active` | Boolean | Deactivated users cannot log in |
| `require_password_change` | Boolean | If True, forces `/crm/setup-password` on login |
| `tenant_id` | String(36) | FK to `tenants.id`. NULL for SUPER_ADMIN |
| `email` | String(120) | Required for SUPER_ADMIN login. Optional for STAFF |
| `created_at` | DateTime | Account creation timestamp |
| `last_login` | DateTime | Updated on successful login |

**Composite Unique Constraint:**
```python
db.UniqueConstraint('tenant_id', 'username', name='uq_users_tenant_username')
# username must be unique within a tenant, but can repeat across tenants
```

**UserMixin:** `User` inherits from Flask-Login's `UserMixin`, which provides:
- `is_authenticated` — True when logged in
- `is_active` — True when account is active
- `get_id()` — returns `str(user.id)` for session storage

---

## 4. Flask-Login Configuration

**Source:** `app/__init__.py` (lines 49–60)

```python
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin.crm_login'      # Redirect unauthenticated users here
login_manager.login_message = "Please log in to access the CRM."

@login_manager.user_loader
def load_user(user_id):
    return models.User.query.get(int(user_id))
```

**`login_view` behavior:** When an unauthenticated user accesses a `@login_required` route, Flask-Login redirects them to `/crm/login?next=<original_path>`.

**`user_loader`:** Called on every request. Loads the `User` object from the database using the `user_id` stored in the session cookie.

---

## 5. Login Gateway 1 — Admin/Staff (`/crm/login`)

**Source:** `app/routes/admin.py`, lines 4649–4685

**Route:** `GET/POST /crm/login`

### GET Request Flow
```
If user already authenticated:
    If ?next= present and starts with '/':
        redirect to next_page
    Else:
        redirect to /crm/home
Else:
    render crm_login.html (login form)
```

### POST Request Flow (login attempt)
```
username = request.form['username']
password  = request.form['password']

user = User.query.filter_by(username=username).first()

if user AND user.is_active AND check_password_hash(user.password_hash, password):

    if user.role != 'SUPER_ADMIN':
        # Phase 13-B2B: Tenant status gate
        tenant = Tenant.query.get(user.tenant_id)
        if NOT tenant OR tenant.status != 'ACTIVE':
            flash("Account awaiting approval.")
            redirect to /crm/login  ← BLOCKED

    user.last_login = datetime.utcnow()
    db.session.commit()
    login_user(user)  ← Flask-Login session created

    if ?next= present and starts with '/':
        redirect to next_page  ← deep link honored
    else:
        redirect to /crm/home

else:
    flash("Invalid credentials or inactive account.")
    render crm_login.html
```

**Key Security Notes:**
- SUPER_ADMIN can log in via this gateway too (no tenant check applies)
- `?next=` is validated: `next_page.startswith('/')` prevents open redirect
- Non-ACTIVE tenant users are blocked at the login gate (PENDING, SUSPENDED, CANCELLED all blocked)
- Only `ACTIVE` status allows non-SUPER_ADMIN login

---

## 6. Login Gateway 2 — Super Admin (`/crm/super/login`)

**Source:** `app/routes/admin.py`, lines 4749–4775

**Route:** `GET/POST /crm/super/login`

### POST Request Flow (login attempt)
```
email    = request.form['email']
password = request.form['password']

# Note: Super Admin logs in with EMAIL, not username
user = User.query.filter_by(email=email, role='SUPER_ADMIN').first()

if user AND user.is_active AND check_password_hash(user.password_hash, password):
    user.last_login = datetime.utcnow()
    db.session.commit()
    login_user(user)

    if ?next= present and starts with '/':
        redirect to next_page
    else:
        redirect to /crm/super/dashboard

else:
    flash("Invalid credentials or unauthorized.")
    render crm_super_login.html
```

**Key Differences from Gateway 1:**
- Uses `email` field (not `username`) for lookup
- Explicitly filters `role='SUPER_ADMIN'` in the query
- On success: redirects to `/crm/super/dashboard` (not `/crm/home`)
- No tenant status check (SUPER_ADMIN has `tenant_id = None`)

---

## 7. Logout Flow

**Source:** `app/routes/admin.py`, lines 4688–4693

**Route:** `GET /crm/logout` — `@login_required`

```python
def crm_logout():
    logout_user()      # Flask-Login clears session user
    session.clear()    # Also clears impersonation state and any other session keys
    return redirect(url_for('admin.crm_login'))
```

**Why `session.clear()`?** Without this, impersonation session keys (`impersonate_tenant_id`, `impersonate_tenant_name`) would persist after logout, creating a security risk.

---

## 8. Session Lifecycle

```
Login
  │
  ▼ login_user(user)
Flask creates signed session cookie (JWT-like, signed with SECRET_KEY)
  │
  ▼ Per-request
user_loader(user_id) → User.query.get(user_id) → current_user populated
  │
  ▼ Per-request (CRM routes)
add_cache_control_headers() → Cache-Control: no-store (prevents back-button access)
  │
  ▼ Logout
logout_user() + session.clear()
Flask invalidates session cookie
  │
  ▼
redirect to /crm/login
```

### Session Keys in Use

| Key | Set By | Purpose |
|-----|--------|---------|
| `_user_id` | Flask-Login | Stores authenticated user ID |
| `impersonate_tenant_id` | Super Admin impersonation | Active tenant being impersonated |
| `impersonate_tenant_name` | Super Admin impersonation | Display name for UI banner |

---

## 9. Role Definitions

| Role | `tenant_id` | Description |
|------|------------|-------------|
| `SUPER_ADMIN` | NULL | Platform owner. Full access to all tenants. Uses `/crm/super/*` routes. |
| `ADMIN` | Tenant UUID | Tenant administrator. Full access to their tenant's CRM. |
| `STAFF` | Tenant UUID | CRM user. Limited access — leads, follow-ups, personal tasks. No admin operations. |
| `READ_ONLY` | Tenant UUID | **Not yet implemented.** Planned for Phase 15C. |

### Role Comparison

| Capability | SUPER_ADMIN | ADMIN | STAFF |
|-----------|------------|-------|-------|
| Login gateway | `/crm/super/login` | `/crm/login` | `/crm/login` |
| View all tenants | ✅ | ❌ | ❌ |
| Impersonate tenant | ✅ | ❌ | ❌ |
| Approve/Suspend tenant | ✅ | ❌ | ❌ |
| Tenant portal (`/tenant/*`) | ✅ (any) | ✅ (own) | ❌ |
| View all CRM leads | ✅ (via impersonation) | ✅ | ✅ (assigned only by default) |
| Create staff users | ❌ (via impersonation, indirectly) | ✅ | ❌ |
| Access analytics | ✅ | ✅ | ❌ (depends on role config) |

---

## 10. Permission Matrix

| Route Pattern | `@login_required` | `@super_admin_required` | `@tenant_admin_required` | Notes |
|--------------|:-----------------:|:----------------------:|:------------------------:|-------|
| `/crm/home` | ✅ | ❌ | ❌ | All authenticated users |
| `/crm/leads` | ✅ | ❌ | ❌ | All authenticated users |
| `/crm/staff/*` | ✅ | ❌ | ❌ | ADMIN only in logic |
| `/crm/analytics` | ✅ | ❌ | ❌ | All authenticated users |
| `/crm/super/dashboard` | ✅ | ✅ | ❌ | SUPER_ADMIN only |
| `/crm/super/tenant/*` | ✅ | ✅ | ❌ | SUPER_ADMIN only |
| `/tenant/home` | ✅ | ❌ | ✅ | ADMIN + SUPER_ADMIN only |
| `/tenant/staff/*` | ✅ | ❌ | ✅ | ADMIN + SUPER_ADMIN only |
| `/crm/auth-debug` | ✅ | ❌ | ❌ | Protected (Phase 14B fix) |
| `/webhook` | ❌ | ❌ | ❌ | Public (Meta webhook, no auth) |
| `/webhooks/razorpay` | ❌ | ❌ | ❌ | Public (billing webhook) |
| `/health` | ❌ | ❌ | ❌ | Public health check |

---

## 11. Route Protection Decorators

### `@login_required`

Provided by Flask-Login. Redirects unauthenticated users to `login_manager.login_view` (`'admin.crm_login'`).

### `@super_admin_required`

**Source:** `app/routes/admin.py`, lines 4739–4747

```python
def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin.crm_super_login'))
        if getattr(current_user, 'role', None) != 'SUPER_ADMIN':
            abort(403)
        return f(*args, **kwargs)
    return decorated_function
```

### `@tenant_admin_required`

**Source:** `app/routes/tenant.py`, lines 51–64

```python
def tenant_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('admin.crm_login'))
        role = getattr(current_user, 'role', None)
        if role not in ('ADMIN', 'SUPER_ADMIN'):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function
```

---

## 12. Password Change Flow

**Source:** `app/routes/admin.py`, lines 4696–4716

When `user.require_password_change = True` is set (e.g., when an admin creates a staff account):

```
User logs in successfully via /crm/login
  │
  ▼ (Flask-Login middleware check — NOT yet implemented as before_request)
Currently: redirect logic is manual, needs explicit check in route
  │
Route: GET /crm/setup-password
  - Checks require_password_change flag
  - If False: redirect to /crm/home (guard bypassed)
  - If True: show password setup form

POST /crm/setup-password
  - Validates new password (min 6 chars)
  - Updates password_hash
  - Sets require_password_change = False
  - Redirects to /crm/home
```

---

## 13. Super Admin Bootstrap

**Source:** `app/__init__.py`, lines 62–85

Oxford CRM includes a CLI command to create the initial Super Admin account:

```bash
flask seed-superadmin
```

**Behavior:**
- Checks if any `SUPER_ADMIN` user exists
- If not: creates user with:
  - `username = "superadmin"`
  - `email = "super@admin.com"`
  - `password = "supersecret"` (must be changed immediately in production)
  - `role = "SUPER_ADMIN"`
  - `tenant_id = None`
  - `is_active = True`

**⚠️ CRITICAL:** The default password `supersecret` must be changed immediately after the first login in production.

---

## 14. Deep Link (`?next=`) Handling

**Source:** `app/routes/admin.py`, Phase 14B.3 fix

When Flask-Login redirects unauthenticated users to the login page, it appends `?next=/original/path`.

**Implementation in `crm_login`:**
```python
# After successful login:
next_page = request.args.get('next')
if next_page and next_page.startswith('/'):
    return redirect(next_page)   # Restore original destination
return redirect(url_for('admin.crm_home'))  # Default
```

**Security:** The `next_page.startswith('/')` check prevents **open redirect attacks**. External URLs (e.g., `?next=https://evil.com`) are silently ignored.

---

## 15. Cache Control — Session Hardening

**Source:** `app/routes/admin.py`, lines 4719–4731 — `@admin_bp.after_request`

All `/crm/*` responses (except `/crm/login` and static assets) include:
```
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
Expires: 0
```

**Purpose:** Prevents browsers from caching authenticated page content. Without this, a user who presses Back after logout could see their previous CRM page from browser cache.

---

## 16. Current Production Status

| Component | Status |
|-----------|--------|
| ADMIN/STAFF login (`/crm/login`) | ✅ Verified working |
| SUPER_ADMIN login (`/crm/super/login`) | ✅ Verified working |
| Deep-link restore (`?next=`) | ✅ Fixed Phase 14B.3 |
| Logout + session clear | ✅ Working |
| Tenant status gate at login | ✅ Working |
| Cache-control headers | ✅ Working |
| `seed-superadmin` CLI | ✅ Available |
| `require_password_change` flow | ✅ Working |

---

## 17. Known Limitations

| Limitation | Impact | Resolution |
|-----------|--------|-----------|
| No password reset via email | Users locked out if password forgotten | Phase 15C |
| No multi-factor authentication (MFA) | Account takeover risk if credentials exposed | Phase 16+ |
| `READ_ONLY` role not implemented | Cannot create audit-only users | Phase 15C |
| `super@admin.com` default email obvious | Enumerable in password reset scenarios | Change in production |
| No per-staff role granularity | All STAFF have same permissions within a tenant | Phase 17+ |

---

## 18. Future Authentication Roadmap

| Feature | Phase | Priority |
|---------|-------|---------|
| Password reset via email | 15C | HIGH |
| READ_ONLY role | 15C | MEDIUM |
| Email-based ADMIN login | 16 | MEDIUM |
| Google OAuth (ADMIN) | 17 | LOW |
| Multi-factor authentication | 17 | MEDIUM |
| API key auth for mobile app | 19 | HIGH |

---

## 19. Related Documents

| Document | Relationship |
|----------|-------------|
| `TENANT_ARCHITECTURE.md` | Tenant status gate details |
| `07_security/RBAC.md` | Full role matrix |
| `07_security/SECURITY_GUIDE.md` | Security constitution |
| `07_security/SECRETS.md` | SECRET_KEY and ADMIN_KEY rules |
| `03_database/DATABASE_BIBLE.md` | User model definition |

---

*Oxford CRM Documentation — docs/02_architecture/AUTHENTICATION_ARCHITECTURE.md*
*Source-verified against: `app/__init__.py`, `app/routes/admin.py` (lines 4640–4848), `app/routes/tenant.py`*
