# 09 — Data Deletion Page Specification (NOT created, NOT deployed)

**Current value (audit):** User data deletion → Data deletion instructions URL =
`https://www.facebook.com/` — a placeholder; must not be submitted.

**VERIFIED:** the application has **no** data-deletion endpoint or flow (no
deletion/erasure route in `app/`). The page must therefore be an
**instructions page** (Meta's "Data deletion instructions URL" option), not an
automated callback — unless a deletion callback is built later (out of scope).

## Suggested URL

`https://theoxfordedu.com/data-deletion` — NEEDS DECISION (same site as the
existing privacy policy and terms URLs).

## Page content requirements

1. Who the data controller is: [LEGAL ENTITY NAME, ADDRESS] — NEEDS INPUT.
2. What data OxfordConnect holds from WhatsApp / Meta (per `08_DATA_HANDLING_DESCRIPTION.md`).
3. How a person requests deletion: [CONTACT EMAIL / FORM] — NEEDS INPUT.
4. What information to include in the request (e.g. the WhatsApp number the
   person used) — NEEDS INPUT.
5. Who performs the deletion and how requests are verified — NEEDS INPUT
   (manual process; no in-app mechanism exists).
6. Expected response time — NEEDS INPUT (do not invent).
7. How businesses (workspace owners) disconnect WhatsApp: the admin
   "Disconnect WhatsApp" action releases the number and stored token —
   VERIFIED (`/tenant/whatsapp/clear`); note it does **not** delete
   conversation history — VERIFIED.
8. Link to the privacy policy.
