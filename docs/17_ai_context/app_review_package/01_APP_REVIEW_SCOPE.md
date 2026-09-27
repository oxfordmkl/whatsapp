# 01 — App Review Scope

Source of truth: RC2.5.19-E App Review Readiness Audit (2026-09-27) and the
repository at `42cb4126a1d71be0bba4a4ecb3d0e7dbc68a27ce` (current production
commit). Preparation only — nothing has been submitted or
removed.

## Primary requests

| Permission | Status | Basis |
|---|---|---|
| `whatsapp_business_management` | **VERIFIED — required** | `app/services/embedded_signup_service.py`: `GET /debug_token` (WABA granted to the business token), `GET /{waba_id}/phone_numbers`, `POST /{waba_id}/subscribed_apps` |
| `whatsapp_business_messaging` | **VERIFIED — required** | `app/services/whatsapp_service.py` (`send_text`, `send_interactive`, `send_list`, `send_template`, `upload_media`); `POST /{phone_number_id}/register` in `embedded_signup_service.py`; inbound `POST /webhook` |

Dashboard state (audit): both **"Ready for testing"**, each with **1 outstanding
requirement** (the review video). Both are listed under App Review **New
requests**, status **"Not submitted"**.

## Extra requests already in the draft — decision required, NOTHING removed

A repository search (`app/`, `templates/`) finds **no reference** to any of these
four permissions or to API calls that are specific to them.

| Permission | Used by current code? | Evidence of use | Needed for ES / Tech Provider flow? | Recommendation |
|---|---|---|---|---|
| `business_management` | No reference found | None | Meta's ES docs (checked 2026-09-26) state it is needed only by **Solution Partners sharing a line of credit**; OxfordConnect is on the **Tech Provider** path. The Builder's System User token instruction does list it for the *system user token*, which is not the same as the app requesting it from customers in App Review. | **NEEDS DECISION** |
| `manage_app_solution` | No reference found | None | Not referenced by the verified ES / Tech Provider steps | **NOT EVIDENCED** |
| `whatsapp_business_manage_events` | No reference found | None (no event-logging / conversions calls in code) | No | **NOT EVIDENCED** |
| `public_profile` | Not referenced | Granted automatically to all apps (dashboard wording: "automatically granted to all apps") | Not a WhatsApp-specific need | **NEEDS DECISION** (Meta may keep it regardless) |

Why this matters (dashboard wording, audit): *"we'll now be reviewing the entire
app in every submission, including: New requests on the app; Existing access…"*
and submissions *"can no longer [be edited or cancelled] when they are in
review."* Every request that stays in the draft needs its own justification and
evidence; unevidenced requests are a review risk.

**Decision owner:** operator. Removing a request is a Meta configuration change
and is NOT authorized in this phase.
