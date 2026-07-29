# Campaign Engine V2 — Operator Guide & Navigation Map

**Status:** ACTIVE
**Introduced:** Phase 9.6A (Production Readiness Remediation)
**Covers:** Phases 9.1–9.6A
**Contracts:** ADR-023 (auth), ADR-024 (dispatch), ADR-025 (audience materialisation)

---

## 1. Purpose

Campaign Engine V2 is the supported surface for sending WhatsApp marketing
campaigns. It replaces the legacy V1 builder as the operator's primary path.

This document records the navigation model and operator flow established in
Phase 9.6A. It exists because Phases 9.3–9.5 shipped UI that no navigation
path reached — the audit that opened Phase 9.6A found the sidebar still
pointing at legacy V1.

---

## 2. Navigation map

All campaign surfaces live under the **Marketing** section of the CRM sidebar,
which is visible only to `ADMIN` and `SUPER_ADMIN`.

```
Sidebar → Marketing
  ├── Marketing Hub                /crm/marketing
  ├── Campaign Center              /crm/campaigns/center      ← V2 primary
  ├── Campaign History             /crm/campaigns/history     ← V2
  └── Campaign Builder (Legacy)    /crm/campaigns             ← V1, de-emphasised
```

Campaign Details is reached **from** History, not from the sidebar — it is
always scoped to one campaign:

```
Campaign History
  ├── campaign name (link)        → /crm/campaigns/details/<id>
  └── expand row → "View Full Details" → /crm/campaigns/details/<id>
```

### Cross-links between V2 surfaces

| From | To | Control |
|---|---|---|
| Center | History | "Campaign History" button, page header |
| History | Center | "Campaign Center" button, page header |
| History | Details | Campaign-name link, and "View Full Details" in the expanded row |
| Details | History | "Back to History" link, page header |

Every V2 page is reachable from every other V2 page in at most two clicks.

---

## 3. Operator flow

### Creating and sending a campaign

1. **Campaign Center** → enter name, message body, choose an audience segment
2. **Create** → campaign is saved as `draft`
3. **Preview** → shows audience size, reachability, and template readiness
   (ADR-025 D6/D7). Materialises nothing.
4. **Validate** → `draft` → `validated`
5. **Acknowledge** → required when any recipient needs an approved template
   (ADR-025 D6.2)
6. **Launch** → `validated` → `running`. The audience is materialised into
   `campaign_recipients` at this moment and is immutable thereafter (D1).

### Monitoring a running campaign

Open **Campaign Details** for live state:

- **Summary** (`LIVE`) — sent / failed / pending / awaiting-retry / cancelled
- **Progress Breakdown** (`LIVE`) — full recipient status distribution
- **Recipient Inspector** — per-recipient rows, filterable, paginated
- **Recent Activity** — last 10 send attempts

> **Dispatch is bursty, not continuous.** The worker sweeps every **300s**
> (`POLL_INTERVAL`) and sends up to **50 recipients per tenant per cycle**
> (`CLAIM_BATCH`) at 1.5s intervals. A campaign will appear frozen for
> ~4 minutes between bursts. This is normal. The "Next worker sweep in ~X"
> countdown on Campaign Details exists to make that visible.

### Reviewing past campaigns

**Campaign History** lists all campaigns newest-first, with status filter
pills and pagination.

---

## 4. Live vs. worker-updated data

Two sources report campaign progress, and they can disagree:

| Source | Freshness | Used by |
|---|---|---|
| `GET /<id>/progress` | **Live** — recomputed per request | Details Summary + Breakdown (tagged `LIVE`) |
| `campaigns.sent_count` / `failed_count` | Lags up to one 300s worker cycle | History table columns |

Phase 9.6A moved every figure on Campaign Details onto the live source.

> ⚠️ The denormalised columns do **not** mean what their names suggest.
> Per `_sync_counters()` (ADR-025 D10):
> - `sent_count` = sent + delivered + read
> - `failed_count` = failed + **cancelled**
>
> Do not render `failed_count` as the "failed" bucket alongside a separate
> cancelled figure — that double-counts cancelled recipients.

---

## 5. Lifecycle and available actions

The transition map in `app/marketing/campaign_service.py` is the **single
authority**. The UI mirrors it and must never offer an action the server will
refuse.

```
draft ──► validated ──┬──► scheduled ──► running ──┬──► completed ──► archived
                      │                            ├──► failed    ──► archived
                      └──────────────► running     └──► cancelled ──► archived
```

| Status | Cancel | Archive |
|---|---|---|
| draft | ✗ | ✗ |
| validated | ✗ | ✗ |
| scheduled | ✗ | ✗ |
| running | ✅ | ✗ |
| completed / failed / cancelled | ✗ | ✅ |
| archived | ✗ | ✗ |

### Known limitations

- **Drafts and validated campaigns cannot be removed.** Neither state has a
  path to `cancelled` or `archived`. Campaign #1 (`test_v2`) is permanently
  stuck for this reason. Changing this requires an ADR-025 amendment and is
  **not** in Phase 9.6A's scope.
- **Scheduling is inert.** `POST /schedule` sets `scheduled`, but nothing
  promotes `scheduled` → `running`; only the manual launch route calls
  `mark_running()`. A scheduled campaign never sends and cannot be cancelled.
  The Campaign Center UI deliberately does **not** expose scheduling, so this
  is reachable only by direct API call. Resolution is a separate phase.
- **`delivered` / `read` are never written.** The statuses and timestamp
  columns exist and `wa_message_id` is stored as the join key, but no Meta
  delivery-status webhook exists yet. These columns show "—" for every
  recipient; this does not indicate a delivery failure.
- **Maximum 100 recipients per campaign** (`MAX_RECIPIENTS`), enforced at
  launch.

---

## 6. Roles

| Capability | STAFF | ADMIN | SUPER_ADMIN |
|---|---|---|---|
| See campaigns in sidebar | ✗ | ✅ | ✅ |
| View Center / History / Details (direct URL) | ✅ read-only | ✅ | ✅ |
| Read campaign list, detail, progress | ✅ | ✅ | ✅ |
| Create / validate / launch | ✗ | ✅ | ✅ |
| Cancel / archive | ✗ | ✅ | ✅ |
| Recipient Inspector (phone numbers) | ✗ | ✅ | ✅ |

The Marketing sidebar section is ADMIN-gated, so STAFF has no navigation route
to these pages. The shells remain reachable by direct URL and render
**read-only**: a banner states the restriction and every mutating control is
disabled up front, rather than failing with a 403 after submission
(Phase 9.6A).

Recipient PII is admin-gated deliberately (Phase 9.4). The legacy CRM
restricts STAFF to their assigned leads; an authn-only recipient list would
have let STAFF enumerate phone numbers for leads they are not assigned to.

---

## 7. Legacy V1 — retained, de-emphasised

Legacy V1 (`/crm/campaigns`, Phase 6G) remains live and reachable. It is
**not** removed, and it is **not** gated by `CAMPAIGN_ENGINE_V2`.

It appears in the sidebar as "Campaign Builder (Legacy)", visually muted and
listed below the V2 entries.

**Do not use V1 for production sends.** It bypasses every ADR-023/024/025
control:

| | V2 | Legacy V1 |
|---|---|---|
| Delivery ledger | ✅ `campaign_recipients` | ✗ fire-and-forget |
| Retry | ✅ transient/permanent classified | ✗ none |
| Cancel | ✅ | ✗ thread runs to completion |
| Lifecycle / audit | ✅ | ✗ |
| Dispatch path | `send_campaign_message` | `send_automation` — rejected by ADR-024 R1 |
| Concurrency guard | ✅ `WEB_CONCURRENCY` assertion | ✗ unbounded thread per campaign |

Retiring V1 is a separate phase with its own approval.

---

## 8. Operational constraints

- **`WEB_CONCURRENCY` must be 1.** The campaign worker runs as a daemon thread
  inside the gunicorn web process (`init_campaign_worker()` from
  `create_app()`). `claim_next_batch()` takes no row locks, so two processes
  would claim and send the same recipients. The worker refuses to start if
  `WEB_CONCURRENCY > 1` — the app still runs, but no campaigns dispatch.
  This caps the whole CRM at one web process.
- **`CAMPAIGN_ENGINE_V2`** gates all V2 routes (404 when off) and the worker
  loop, re-read live on every request and every worker iteration.
- **`AUTH_MODE` must be `SESSION_ONLY`** — the blueprint 404s otherwise
  (ADR-023 D1).

---

## 9. Change history

| Phase | Change |
|---|---|
| 9.1A/B | Campaign Center V2 UI; serializer field exposure |
| 9.1F | Fix: `audience_segment` was discarded at create |
| 9.1G | Fix: permanent failures no longer enter the retry queue (ADR-024 R4) |
| 9.2 | Campaign History |
| 9.3 | Campaign Details |
| 9.4 | Recipient Inspector (admin-gated) |
| 9.5 | Live progress: 30s poll, sweep countdown, pending/retry split, segmented bar, recent activity |
| 9.6 | Production readiness audit |
| 9.6A | Navigation remediation; legacy separation; cancel-transition fix; live-counter labelling; STAFF read-only UX |
