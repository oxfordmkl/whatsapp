---
KnowledgeID: DOC-IMPINDEX
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# IMPLEMENTATION REGISTRY

This registry maps abstract Modules from the Knowledge Registry down to physical codebase artifacts using Stable IDs.

## Implementation Map

| Stable ID | Implementation Target | Artifact Type | Parent Module |
| :--- | :--- | :--- | :--- |
| `IMP-MDL-001` | `app/models.py` | SQLAlchemy Model | Cross-Module (DB Layer) |
| `IMP-SRV-001` | `app/services/crm_service.py` | Service | `MOD-CRM-LEAD` |
| `IMP-SRV-002` | `app/services/ai_service.py` | Service | `MOD-AINT-ROUT`|
| `IMP-API-001` | `app/routes/webhook.py` | Blueprint/API | `MOD-MRKT-CAMP`|
| `IMP-UI-001` | `templates/crm_lead_detail.html`| Jinja Template | `MOD-CRM-LEAD` |
| `IMP-WKR-001` | `app/services/followup_service` | Thread Worker | `MOD-AUTO-SCHED`|
| `IMP-TST-001` | `tests/test_ai_router.py` | PyTest Suite | `MOD-AINT-ROUT`|

| `IMP-MIG-001` | `migrations/versions/258025fe9676_feat_phase_16_5a3_enterprise_.py` | Alembic Migration | Cross-Module |
| `IMP-MIG-002` | `migrations/versions/a5f0c3e91b7d_phase_16_5a5_orm_adapter_foundation.py` | Alembic Migration | `IMP-MDL-001` |
| `IMP-MIG-003` | `migrations/versions/b6e1d4f82c9e_phase_16_5a5_h1_enterprise_baseline_v1_1.py` | Alembic Migration | `IMP-MDL-001` |
| `IMP-ADR-013` | `docs/21_decision_records/ADR-013-json-column-standard.md` | ADR | `DOM-INFR` |
| `IMP-ADR-014` | `docs/21_decision_records/ADR-014-reserved-orm-attribute-naming.md` | ADR | `DOM-INFR` |
| `IMP-ADR-018` | `docs/21_decision_records/ADR-018-business-conversion-independence.md` | ADR | `DOM-CORE` |
| `IMP-ADR-019` | `docs/21_decision_records/ADR-019-compatibility-pipeline-standard.md` | ADR | `DOM-CORE` |
| `IMP-ADR-020` | `docs/21_decision_records/ADR-020-course-offering-synchronization.md` | ADR | `DOM-CORE` |
| `IMP-ADR-021` | `docs/21_decision_records/ADR-021-task-notification-foundation.md` | ADR | `DOM-CORE` |
| `IMP-SRV-003` | `app/services/backfill_service.py` | Service (Backfill Engine) | `IMP-MDL-001` |
| `IMP-SRV-004` | `app/services/task_service.py` | Service (Task Engine) | `IMP-MDL-001` |
| `IMP-SRV-005` | `app/services/notification_service.py` | Service (Notifications) | `IMP-MDL-001` |
| `IMP-SCR-001` | `scripts/run_backfill_16_5a6.py` | Migration Runner | `IMP-SRV-003` |
| `IMP-MIG-004` | `migrations/versions/c7a2f19d4e88_phase_16_5a7_task_notification_foundation.py` | Alembic Migration | `IMP-MDL-001` |
| `IMP-UI-002` | `templates/crm_notifications.html` | Jinja Template (Notification Centre) | `IMP-SRV-005` |
| `IMP-UI-003` | `templates/crm_sidebar.html` | Jinja Partial (Notification Bell) | `IMP-SRV-005` |
| `IMP-TST-002` | `tests/test_adapter_sync_16_5a6j.py` | Mutation Test Suite | `IMP-MDL-001` |
| `IMP-TST-003` | `tests/test_task_notification_16_5a7.py` | Task/Notification Test Suite | `IMP-SRV-004` |
| `IMP-DOC-16A6-1` | `docs/09_MIGRATIONS/PHASE_16_5A6_ENTERPRISE_BACKFILL_RUNBOOK.md` | Runbook | `DOM-INFR` |
| `IMP-DOC-16A6-2` | `docs/09_MIGRATIONS/PHASE_16_5A6_MIGRATION_CHECKLIST.md` | Checklist | `DOM-INFR` |
| `IMP-DOC-16A6-3` | `docs/09_MIGRATIONS/PHASE_16_5A6_ROLLBACK_CHECKLIST.md` | Checklist | `DOM-INFR` |
| `IMP-DOC-16A6-4` | `docs/09_MIGRATIONS/PHASE_16_5A6_PRODUCTION_VERIFICATION_CHECKLIST.md` | Checklist | `DOM-INFR` |
| `IMP-DOC-16A6-5` | `docs/09_MIGRATIONS/PHASE_16_5A6_OPERATIONAL_RUNBOOK.md` | Runbook | `DOM-INFR` |

*Note: This index is a starting foundation and will be automatically expanded in Phase K1.4.*

## Runtime Initialization Registry

1. **Tenant Initialization**: Load `Tenant` IDs from DB.
2. **Service Initialization**: Load Gemini API keys and Meta Cloud credentials.
3. **Blueprint Registration**: Attach `admin_bp`, `webhook_bp`, `public_bp` to Flask app.
4. **Scheduler Startup**: Spin off threading (`followup_worker`, `analytics_aggregator`).
5. **AI Initialization**: Load global prompts into memory.

## References
- [Repository Manifest](REPOSITORY_MANIFEST.md)
- [Master Index](MASTER_INDEX.md)
- [Knowledge Registry](KNOWLEDGE_REGISTRY_INDEX.md)
