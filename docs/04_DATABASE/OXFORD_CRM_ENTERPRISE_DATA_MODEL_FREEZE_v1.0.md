# OXFORD CRM ENTERPRISE DATA MODEL FREEZE v1.1

> **Supersedes:** v1.0 (same file, updated in place per ADR-013, ADR-014)
> **Governance Phase:** K2.1 — Enterprise Architecture Baseline v1.1
> **Ratified:** 2026-07-16
> **Changes from v1.0:**
> - §3: `Offering.metadata` renamed to `Offering.custom_attributes` (ADR-014 — `metadata` is SQLAlchemy-reserved)
> - §3: All JSONB columns re-typed to `SQLAlchemy JSON` (ADR-013 — TEXT on SQLite, JSON on PostgreSQL)
> - §3: `Offering.price` marked nullable=Yes (multi-industry: not all verticals have list pricing)
> - §6: CASCADE constraint policy updated — see ADR-015 (tiered referential integrity)

## 1. Model Inventory
The following models constitute the Enterprise Configuration Foundation. No additional implementation is permitted without architectural review.
- `TenantSettings`: Key-Value/JSON configuration for tenant preferences.
- `PipelineDefinition`: Defines a lifecycle funnel.
- `PipelineStage`: Ordered stages within a pipeline.
- `Offering` (ProductCatalog): Products, courses, or services offered by the tenant.
- `TagCategory`: Logical grouping of tags (e.g., Marketing, System).
- `TagDefinition`: Individual assignable labels.
- `MessageTemplate`: DB representation of omnichannel templates (Meta WhatsApp).
- `AudienceRule`: Dynamic segmentation logic.
- `AutomationRule`: Event-driven trigger/action workflows.
- `AIConfiguration`: Tenant AI persona, RAG document references, and intent maps.

---

## 2. Model Specifications

### TenantSettings
- **Purpose:** Global tenant preferences (Timezone, Currency, Feature Flags).
- **Owner:** Tenant Admin
- **Lifecycle:** Created on tenant registration. Never deleted.
- **Tenant Ownership:** Explicit `tenant_id`.
- **Relationships:** 1:1 with `Tenant`.
- **Indexes:** `tenant_id`.
- **Unique Constraints:** `tenant_id`.
- **Foreign Keys:** `tenant_id` -> `tenants.id`.
- **Cascade Rules:** Delete if Tenant is deleted.
- **Soft Delete Policy:** No (tied to Tenant lifecycle).
- **Audit Requirements:** Log updates.
- **Migration Risk:** Low.
- **Rollback Strategy:** Ignore table.

### PipelineDefinition
- **Purpose:** Defines the overarching lifecycle funnel (e.g., "Sales", "Support").
- **Owner:** Tenant Admin
- **Lifecycle:** Created manually or via industry template.
- **Tenant Ownership:** Explicit `tenant_id`.
- **Relationships:** 1:M with `PipelineStage`.
- **Indexes:** `tenant_id`.
- **Unique Constraints:** `tenant_id` + `internal_key`.
- **Foreign Keys:** `tenant_id`.
- **Cascade Rules:** Delete stages if pipeline deleted.
- **Migration Risk:** High (replaces hardcoded CRM states).

### PipelineStage
- **Purpose:** Distinct steps within a pipeline (e.g., "Lead", "Demo", "Admitted").
- **Owner:** Tenant Admin
- **Lifecycle:** Ordered steps.
- **Tenant Ownership:** Implicit via `PipelineDefinition`.
- **Relationships:** M:1 with `PipelineDefinition`. 1:M with `ConversationState`.
- **Indexes:** `pipeline_id`, `internal_key`.
- **Unique Constraints:** `pipeline_id` + `internal_key`.
- **Foreign Keys:** `pipeline_id`.
- **Migration Risk:** High.

### Offering
- **Purpose:** Generic product/service/course representation.
- **Owner:** Tenant Admin
- **Relationships:** M:M with `ConversationState` (via `ConversationOffering` bridge).
- **Unique Constraints:** `tenant_id` + `internal_key`.
- **Foreign Keys:** `tenant_id`.

### TagCategory & TagDefinition
- **Purpose:** Grouping and defining CRM labels.
- **Relationships:** `TagCategory` 1:M `TagDefinition`. `TagDefinition` M:M `ConversationState` (via `ConversationTag` bridge).
- **Unique Constraints:** `tenant_id` + `name`.

### MessageTemplate
- **Purpose:** Caches Meta WhatsApp templates.
- **Unique Constraints:** `tenant_id` + `provider_template_id`.

### AudienceRule & AutomationRule
- **Purpose:** Dynamic evaluation rules for segments and actions.
- **Unique Constraints:** `tenant_id` + `internal_key`.

### AIConfiguration
- **Purpose:** Prompts, personas, and intent mapping.
- **Relationships:** 1:1 with `Tenant`.

---

## 3. Column Freeze

> **v1.1 type standard:** All structured JSON columns use `SQLAlchemy JSON` (stored as `TEXT` on SQLite,
> `JSON` on PostgreSQL). The term `JSONB` in v1.0 is superseded — see ADR-013.
> `Offering.metadata` is renamed to `Offering.custom_attributes` — see ADR-014.

| Model | Column Name | Data Type | Nullable | Default | Unique | Indexed | Mutable | Validation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TenantSettings** | `settings` | JSON | No | `{}` | No | No | Yes | Valid JSON object |
| **PipelineDefinition**| `internal_key` | String(50) | No | None | Yes(w/tenant)| Yes | No | Regex `^[a-z0-9_]+$` |
| **PipelineStage** | `order` | Integer | No | 0 | No | No | Yes | >= 0 |
| **PipelineStage** | `stage_category` | String(20) | No | 'open' | No | Yes | Yes | Enum: open, won, lost |
| **Offering** | `price` | Numeric(12,2) | **Yes** | None | No | No | Yes | >= 0 (app-enforced) |
| **Offering** | `custom_attributes` | JSON | Yes | None | No | No | Yes | Valid JSON object |
| **TagDefinition** | `color_hex` | String(7) | No | '#EEEEEE'| No | No | Yes | Regex `^#[0-9A-Fa-f]{6}$` |
| **MessageTemplate** | `variables` | JSON | No | `[]` | No | No | Yes | Array of variable names |
| **AudienceRule** | `logic_tree` | JSON | No | `{}` | No | No | Yes | Valid rule JSON |
| **AutomationRule** | `trigger_event` | String(50) | No | None | No | Yes | Yes | Valid event string |

---

## 4. Relationship Freeze

- **One-to-One:**
  - `Tenant` - `TenantSettings`
  - `Tenant` - `AIConfiguration`
- **One-to-Many:**
  - `Tenant` - `PipelineDefinition`
  - `PipelineDefinition` - `PipelineStage`
  - `PipelineStage` - `ConversationState`
- **Many-to-Many Bridge Tables (NEW DECISION):**
  - `ConversationTag`: Links `ConversationState.id` <-> `TagDefinition.id`.
  - `ConversationOffering`: Links `ConversationState.id` <-> `Offering.id`.

---

## 5. Index Strategy

- **`tenant_id` on all top-level models:** Mandatory. All queries use `tenant_filter(Model, tenant_id)`.
- **`internal_key`:** Indexed for fast business logic lookups (e.g., `pipeline_stage.internal_key == 'admitted'`).
- **`stage_category`:** Indexed for fast dashboard aggregation (e.g., count all 'won' leads).
- **`trigger_event` (AutomationRule):** Indexed to instantly fetch relevant rules when a system event fires.

---

## 6. Constraint Strategy

- **Composite Unique Keys:** `(tenant_id, internal_key)` ensures that a tenant cannot have two pipelines or tags with the same programmatic identifier, while allowing different tenants to share names.
- **Foreign Keys:** v1.1 tiered referential integrity (ADR-015, supersedes blanket CASCADE from v1.0):
  - Tenant-root FKs: `RESTRICT` — no cascade. Offboarding handled by explicit `TenantOffboardingService`.
  - Composition FKs (pipeline → stage, category → tag): soft-delete default; optional CASCADE where no soft-delete semantics exist.
  - Bridge table FKs (conversation ↔ tag/offering): `CASCADE` on the conversation leg (bridge row has no independent value).
  - `SCHEMA_RULES.md §4/§12` prohibition on blanket CASCADE is preserved — this tiered policy is the reconciled standard.
- **Bridge Table Constraints:** `(conversation_id, tag_id)` must be unique.

---

## 7. Backward Compatibility Matrix

| Legacy Field (ConversationState) | Future Model / Field | Adapter Strategy | Removal Phase |
| :--- | :--- | :--- | :--- |
| `course` | `Offering` (via Bridge) | `@property` getter returns first Offering name | Never (Abstracted) |
| `offer_course` | `Offering` | Map to JSON `custom_attributes['offer_course']` | Never |
| `batch_time` | `custom_attributes` | Map to JSON `custom_attributes['batch_time']` | Never |
| `is_admitted` | `PipelineStage` | `@property` checks `stage.category == 'won'` | Never |
| `stage` | `PipelineStage` | Map directly to `PipelineStage.display_name` | Never |

---

## 8. Query Impact Review

- **Lead Search:** Improved. Searching by tags/offerings utilizes relational JOINs rather than text wildcards.
- **Audience Engine:** Significant rewrite required. Will execute dynamic SQLAlchemy queries parsed from `AudienceRule.logic_tree`.
- **Analytics:** Dashboards will group by `PipelineStage.stage_category` instead of hardcoded strings, making them instantly compatible with any industry.

---

## 9. Performance Review

- **Read vs Write:** Heavily read-optimized (95% Read / 5% Write). Configuration changes rarely.
- **Caching:** Configuration models (`TenantSettings`, `PipelineDefinition`) are prime candidates for Redis caching per `tenant_id`.
- **FK Usage:** Bridge tables guarantee fast index scans over millions of rows compared to querying arrays inside JSONB.
- **Scalability:** The model is structurally capable of supporting 1,000,000+ tenants by leveraging explicit `tenant_id` partitioning.

---

## 10. Multi-Tenant Review

- Every single model generated in this freeze explicitly contains `tenant_id` (except Bridge tables which inherit isolation via `ConversationState` and `TagDefinition`).
- Cross-tenant leakage is prevented via global SQLAlchemy `tenant_filter`.

---

## 11. Security Review (RBAC)

| Model | SUPER_ADMIN | TENANT_ADMIN | STAFF | SYSTEM |
| :--- | :--- | :--- | :--- | :--- |
| `TenantSettings` | Read/Update | Read/Update | Read | Read |
| `PipelineDefinition`| Read | CRUD | Read | Read |
| `Offering` | Read | CRUD | Read | Read |
| `TagDefinition` | Read | CRUD | Read | Read |
| `ConversationTag` | Read | CRUD | CRUD | CRUD |
| `MessageTemplate` | Read | Read (Sync Meta)| Read | CRUD |
| `AutomationRule` | Read | CRUD | Read | Execute |

---

## 12. Future AI Compatibility

- **RAG:** `AIConfiguration` will support a `documents` array referencing embedded knowledge base chunks.
- **Intent Mapping:** `AutomationRule` combined with `AIConfiguration` will allow tenants to map natural language intents directly to `PipelineStage` transitions.

---

## 13. Enterprise Scalability Review

This schema completely removes Education-specific constraints.
- **Hospital:** Uses `Pipeline` (Inquiry -> Appointment), `Offering` (Consultation), `Tags` (Urgent).
- **Ecommerce:** Uses `Pipeline` (Cart -> Checkout -> Shipped), `Offering` (Physical Product), `Tags` (VIP Customer).
- **Real Estate:** Uses `Pipeline` (Site Visit -> Booked), `Offering` (Villa/Plot).

The schema supports all target verticals without requiring any future `ALTER TABLE` schema changes.
