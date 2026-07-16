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

## References
- [Repository Manifest](../REPOSITORY_MANIFEST.md)
- [Master Index](../MASTER_INDEX.md)
