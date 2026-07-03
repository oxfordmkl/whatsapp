# Oxford CRM — Project Scope
## What Is and Is Not In Scope

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Project Leadership
> **Last Updated:** 2026-07-02 | **Next Review:** Phase 16.0 launch

---

## Kerala Release Scope (Current)

### IN SCOPE — Implemented and Active

| Feature | Status | Phase Delivered |
|---------|--------|----------------|
| WhatsApp Cloud API integration | ✅ Live | Phase 11–12 |
| Gemini AI conversation engine | ✅ Live | Phase 10N |
| Lead management and CRM pipeline | ✅ Live | Phase 9–10 |
| Staff management and allocation | ✅ Live | Phase 10 |
| Marketing broadcast hub | ✅ Live | Phase 11 |
| Campaign management | ✅ Live | Phase 12 |
| Revenue and funnel analytics | ✅ Live | Phase 10M |
| Follow-up automation | ✅ Live | Phase 12 |
| Tenant portal (admin self-service) | ✅ Live | Phase 13-B3B |
| Multi-tenant architecture | ✅ Live | Phase 12 |
| Flask-Login authentication | ✅ Live | Phase 10 |
| Razorpay billing foundation | ✅ Live (foundation only) | Phase 13-B4.1 |
| WABA credential encryption | ✅ Live | Phase 13-B4B2 |
| Dynamic webhook tenant routing | ✅ Live | Phase 13-B4D2 |
| Production secret validation | ✅ Live | Phase 14B |
| Deep-link authentication restore | ✅ Live | Phase 14B.3 |

### IN SCOPE — Approved but Not Complete

| Feature | Status | Target Phase |
|---------|--------|-------------|
| Super Admin — Delete Tenant | ⚠️ Planned | Phase 15C |
| Super Admin — Archive Tenant | ⚠️ Planned | Phase 15C |
| Automated tenant approval flow | ⚠️ Planned | Phase 15C |
| Enterprise documentation system | ⚠️ In Progress | Phase 15B |

---

## Kerala Release Scope — EXPLICITLY OUT OF SCOPE

The following are **forbidden** from implementation until explicitly approved:

| Feature | Reason | Future Phase |
|---------|--------|-------------|
| Stripe payment integration | India-only release uses Razorpay | Phase 16+ |
| International billing | India-first strategy | Phase 16+ |
| Public tenant registration | Manual onboarding only for now | Phase 15C |
| LMS (Learning Management System) | Future module | Phase 17 |
| Student Portal | Future module | Phase 18 |
| Mobile App | Future module | Phase 19 |
| Global SaaS multi-region | Future expansion | Phase 20 |
| READ_ONLY user role | Technical debt item | Phase 15C |
| Tenant DELETE/ARCHIVE via UI | Technical debt item | Phase 15C |

---

## Scope Change Process

To add anything to scope:
1. Raise it as a user request
2. Receive explicit "implement this" approval
3. Document it in `15_decisions/DECISION_LOG.md`
4. Update this document

**Nothing enters scope without documented approval.**

---

*Oxford CRM Documentation — docs/01_project/PROJECT_SCOPE.md*
*Cross-references: `PROJECT_BIBLE.md` · `ROADMAP.md` · `17_ai_context/NEXT_PHASE.md`*
