---
KnowledgeID: DOC-ADRINDEX
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# ARCHITECTURE DECISION RECORDS (ADR) INDEX

This registry catalogs every formal architectural decision made in the Oxford CRM Enterprise repository.

| ADR File | Decision Name | Status | Phase | Supersedes | Dependencies |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `ADR-001-why-flask.md` | Why Flask | ACTIVE | Foundation | None | None |
| `ADR-002-why-railway.md` | Why Railway | ACTIVE | Foundation | None | None |
| `ADR-003-why-postgresql.md`| Why PostgreSQL | ACTIVE | Foundation | None | None |
| `ADR-004-why-multi-tenant.md` | Why Multi-Tenant | ACTIVE | 16.0 | None | None |
| `ADR-005-why-razorpay.md` | Why Razorpay | ACTIVE | 16.0 | None | None |
| `ADR-006-why-billing-provider-abstraction.md` | Billing Abstraction | ACTIVE | 16.0 | None | `ADR-005` |
| `ADR-007-why-tenant-isolation.md` | Tenant Isolation | ACTIVE | 16.0 | None | `ADR-004` |
| `ADR-008-why-documentation-freeze.md` | Documentation Freeze | ACTIVE | K1.0 | None | None |
| `ADR-009-why-bootstrap-pack.md` | Bootstrap Pack | ACTIVE | K1.0 | None | None |
| `ADR-010-why-enterprise-knowledge-layer.md` | Enterprise Knowledge | ACTIVE | K1.2 | None | `ADR-008` |
| `ADR-011-why-tenant-lifecycle-and-data-permanence.md` | Tenant Data Permanence | ACTIVE | 16.0 | None | `ADR-007` |
| `ADR-012-KNOWLEDGE-BASELINE.md` | Knowledge Baseline v1.0 Freeze | ACTIVE | K1.x | None | None |
| `ADR-013-json-column-standard.md` | JSON Column Standard | ACTIVE | K2.1 | None | `ADR-012` |
| `ADR-014-reserved-orm-attribute-naming.md` | Reserved ORM Attribute Naming | ACTIVE | K2.1 | None | `ADR-013` |
| `ADR-018-business-conversion-independence.md` | Business Conversion Independence | ACTIVE | 16.5A5-J | Freeze v1.1 §7 (`is_admitted`) | `ADR-012` |
| `ADR-019-compatibility-pipeline-standard.md` | Compatibility Pipeline Standard | ACTIVE | 16.5A5-J | None | `ADR-018` |
| `ADR-020-course-offering-synchronization.md` | Course–Offering Synchronization | ACTIVE | 16.5A6-J | 16.5A5-I `_sync_offering_link` no-op contract | `ADR-019` |

*Note: ADR-015 (Tiered Cascade), ADR-016 (Pricing Standard) and ADR-017 (Architecture
Governance) were proposed in Phase K2.1 and remain PENDING ratification. The numbering
gap is intentional — 16.5A5-J required ADR-018/019 ahead of them.*

## References
- [Repository Manifest](../REPOSITORY_MANIFEST.md)
- [Master Index](../MASTER_INDEX.md)
