# Oxford CRM — Project Evolution
## From Bot to SaaS

> **Version:** 1.0 | **Phase:** 15B | **Audience:** Architects, AI
> **Reading Time:** 2 minutes | **Expected Knowledge:** Architectural journey
> **Last Updated:** 2026-07-03 | **Update Trigger:** Structural architecture pivot
> **Dependencies:** None
> **Related Documents:** `04_PROJECT_TIMELINE.md`

---

## 1. Purpose

This document explains the conceptual evolution of the codebase. Understanding this evolution helps explain why certain technical debt exists and why specific patterns (like `tenant_query()`) are used.

---

## 2. The Evolutionary Path

### Stage 1: Single Tenant Bot
Originally, this codebase was a standalone Python script handling WhatsApp webhooks for Oxford Computers.
*Artifacts remaining:* Hardcoded references to Oxford courses in `bot/constants.py`.

### Stage 2: CRM Addition
A Flask web interface was bolted onto the bot to allow staff to see the leads generated.
*Artifacts remaining:* The massive `admin.py` file which became the dumping ground for all CRM routes.

### Stage 3: Multi-Tenant Pivot
The business decided to sell the CRM to other institutions. We injected a `tenant_id` into every table and created the `Tenant` root object.
*Artifacts remaining:* The `tenant_query()` wrapper was created instead of rewriting the entire ORM layer. This is why you must explicitly pass context everywhere.

### Stage 4: Billing & SaaS
We added provider-agnostic billing (Razorpay primary) to support subscriptions.

### Stage 5: Kerala Release (Current)
We froze the architecture, patched security holes, and generated this documentation to prepare for the final Super Admin layer.

---

## 3. What This Means For You

Because of this evolution, Oxford CRM is a **retrofitted SaaS**. 
- You cannot rely on Flask globals for tenant isolation.
- You must always respect the `tenant_id` boundaries introduced in Stage 3.
- Do not attempt to "clean up" `admin.py` without explicit permission, as it contains critical retrofitted security logic.
