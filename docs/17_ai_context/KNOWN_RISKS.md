# Oxford CRM — Known Risks
## Platform Risk Register

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Engineering Team
> **Last Updated:** 2026-07-02 | **Next Review:** Quarterly or after any incident

---

## Risk Classification

| Level | Definition |
|-------|-----------|
| **CRITICAL** | Immediate action required. Platform or data at risk. |
| **HIGH** | Action required before next major release. |
| **MEDIUM** | Action required within current major version. |
| **LOW** | Monitor. Address in future phases. |

---

## Active Risks

### 🔴 CRITICAL Risks
None.

### 🟠 HIGH Risks
None.

---

### 🟡 MEDIUM Risks

#### R-001 — `admin.py` Mega-File
| Field | Value |
|-------|-------|
| **ID** | R-001 |
| **Severity** | MEDIUM |
| **Description** | `app/routes/admin.py` contains 4,800+ lines covering authentication, leads, analytics, staff, campaigns, and Super Admin routes. Any unintended edit could cause regression across multiple features. |
| **Impact** | High regression risk during any future modifications to this file |
| **Mitigation** | Strict surgical-edit policy. Every change must touch only the specific function being modified. Use `py_compile` after every edit. |
| **Resolution** | Modularize into separate route files in Phase 16 (post-Kerala) |
| **Rollback** | Revert specific function to its pre-edit state |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 15A |

---

#### R-002 — No Tenant DELETE/ARCHIVE UI Endpoint
| Field | Value |
|-------|-------|
| **ID** | R-002 |
| **Severity** | MEDIUM |
| **Description** | The Super Admin dashboard has no route to delete or archive a tenant. If a tenant needs to be removed, direct database intervention is required. |
| **Impact** | Operational inefficiency; risk of manual DB errors |
| **Mitigation** | Document the manual SQL procedure in `13_operations/RUNBOOK.md` |
| **Resolution** | Implement in Phase 15C |
| **Rollback** | N/A — it's the absence of a feature |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 15A |

---

#### R-007 — No Verified Privileged Password Recovery
| Field | Value |
|-------|-------|
| **ID** | R-007 |
| **Severity** | MEDIUM |
| **Description** | No verified privileged password recovery/reset workflow exists. Passwords are hashed and cannot be retrieved by design. |
| **Impact** | OPERATIONAL ACCESS RECOVERY RISK — Loss of credentials requires direct CLI/DB intervention. |
| **Mitigation** | Securely store the single set of provisioned Super Admin credentials offline. |
| **Resolution** | Implement a separately audited secure privileged password-reset CLI or equivalent approved recovery flow in a future hardening phase. Do not recreate accounts as default recovery. |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 15C.1 |

---

#### R-008 — Background Worker Suspension Enforcement
| Field | Value |
|-------|-------|
| **ID** | R-008 |
| **Severity** | MEDIUM |
| **Description** | Some background outbound workers may not consistently enforce `Tenant.status` before execution. A SUSPENDED tenant may still have previously scheduled outbound work processed. |
| **Impact** | VERIFIED CODE-PATH RISK — Outbound messages could fire for suspended tenants |
| **Mitigation** | Review worker logic to explicitly require `ACTIVE` or `TRIAL` status |
| **Resolution** | Pending audit/fix in automation phase |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 15C.3 |

### 🟢 LOW Risks

#### R-003 — No READ_ONLY Role
| Field | Value |
|-------|-------|
| **ID** | R-003 |
| **Severity** | LOW |
| **Description** | The `READ_ONLY` role was planned but not implemented. Currently, audit users must be given STAFF access (which allows edits). |
| **Impact** | Cannot create proper read-only auditor accounts |
| **Mitigation** | Document limitation. Use STAFF role as closest alternative. |
| **Resolution** | Phase 15C |
| **Owner** | Engineering Team |

---

#### R-004 — `_get_default_tenant_id()` Single-Tenant Assumption
| Field | Value |
|-------|-------|
| **ID** | R-004 |
| **Severity** | LOW |
| **Description** | In `app/services/log_service.py`, background thread functions call `_get_default_tenant_id()` which returns the first tenant found. This is acceptable for Kerala (single tenant) but becomes incorrect with multiple active tenants. |
| **Impact** | Low for Kerala (single tenant). Will cause incorrect tenant assignment in multi-tenant future if not addressed. |
| **Mitigation** | All primary paths pass `tenant_id` explicitly. This fallback only triggers in edge cases. |
| **Resolution** | Remove fallback in Phase 16 when multi-tenant is active |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 14B.1 |

---

#### R-005 — WABA_ENCRYPTION_KEY Missing in Dev Mode
| Field | Value |
|-------|-------|
| **ID** | R-005 |
| **Severity** | LOW |
| **Description** | If `WABA_ENCRYPTION_KEY` is not set in the local development environment, the application boot fails when any WABA operation is attempted. |
| **Impact** | Developer experience friction |
| **Mitigation** | Add `WABA_ENCRYPTION_KEY` to local `.env` file. Use the `.env.example` template. |
| **Resolution** | Document in `08_deployment/ENVIRONMENT_VARIABLES.md` |
| **Owner** | Engineering Team |

---

#### R-006 — POST-LOGIN Destination Visible in URL
| Field | Value |
|-------|-------|
| **ID** | R-006 |
| **Severity** | LOW |
| **Description** | After Phase 14B.3, the `?next=` parameter is honored. The destination path is visible in browser address bar and server logs during login. |
| **Impact** | Minimal — paths are not sensitive in this application. Validated to be local-only. |
| **Mitigation** | `next_page.startswith('/')` check prevents open redirect. |
| **Resolution** | Acceptable as-is for Kerala |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 14B.3 |

---

#### R-009 — Tenant.status Contract Inconsistency
| Field | Value |
|-------|-------|
| **ID** | R-009 |
| **Severity** | LOW |
| **Description** | `PENDING` is used at runtime but omitted from model docstring. `DELETED` is documented but unused. Transition enforcement is partial/loose. |
| **Impact** | Confusion over allowed lifecycle states |
| **Mitigation** | Rely on explicit application logic in `admin.py` |
| **Resolution** | Pending Phase 16 refactor |
| **Owner** | Engineering Team |
| **Phase Identified** | Phase 15C.3 |

---

## Resolved Risks

| ID | Description | Resolved In | How |
|----|-------------|------------|-----|
| R-OLD-001 | `SECRET_KEY` using insecure default in production | Phase 14B | Boot-time validation added |
| R-OLD-002 | `auth-debug` endpoint exposed without authentication | Phase 14B | `@login_required` added |
| R-OLD-003 | Duplicate `/crm/leads` decorator causing route override | Phase 14B | Rogue decorator removed |
| R-OLD-004 | Deep links broken (next URL ignored) | Phase 14B.3 | `next_page` logic added |
| R-OLD-005 | Tenant portal unreachable from sidebar | Phase 14B.3 | Sidebar link added |
| R-OLD-006 | Billing blueprint unregistered | Phase 14B | Registered in `__init__.py` |

---

## Risk Review Schedule

| Review | Trigger |
|--------|---------|
| Immediate | Any new security finding |
| After every phase | Check if risks created or resolved |
| Quarterly | Full risk register review |

---

*Oxford CRM Documentation — docs/17_ai_context/KNOWN_RISKS.md*
*Cross-references: `PROJECT_STATE.md` · `17_ai_context/NEXT_PHASE.md` · `07_security/SECURITY_GUIDE.md`*
