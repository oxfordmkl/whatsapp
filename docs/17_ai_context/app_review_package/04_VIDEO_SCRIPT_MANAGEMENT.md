# 04 — Review Video 1: `whatsapp_business_management`

Meta requirement (dashboard wording, Tech Provider onboarding step 2):
*"For the required whatsapp_business_management permission, make API test calls
and record a separate unique video of creating a message template."*

## Key constraint — VERIFIED

OxfordConnect **does not create message templates**. The repository has no
`POST /{waba_id}/message_templates` call; it only lists templates
(`GET /{WABA_ID}/message_templates`, primary tenant). The video therefore has to
show the template being created **via an API test call**, as the dashboard
wording itself says ("make API test calls"), not via an OxfordConnect screen.
Presenting an OxfordConnect template editor would claim functionality that does
not exist — **do not**.

**NEEDS DECISION:** where the API test call is made — Meta's **Graph API
Explorer** (listed under the Builder's Tools & Resources) or Meta's WhatsApp
Business Management API Postman collection (also listed there). Both are Meta
tools; pick one.

## A. Objective

Show that OxfordConnect's app, with `whatsapp_business_management`, can (1)
read the connected WhatsApp Business Account's phone numbers — the call
OxfordConnect uses during Embedded Signup — and (2) create a message template on
that account via an API call.

## B. Preconditions (all NEEDS INPUT unless stated)

- A **test** WhatsApp Business Account the app has access to — NEEDS INPUT (not
  Oxford's production WABA unless explicitly authorized).
- Its WABA ID — NEEDS INPUT.
- An access token for the OxfordConnect app with `whatsapp_business_management`
  (e.g. the `oxford-whatsapp` system user token) — exists (VERIFIED by operator's
  debugger), must stay masked on screen.
- The chosen API tool open and signed in — NEEDS DECISION.

## C. Screen-by-screen sequence

1. Title card: "OxfordConnect — whatsapp_business_management" (2–3 s).
2. API tool with the **OxfordConnect** app selected (app name visible).
3. **Call 1 (read):** `GET /{waba_id}/phone_numbers` → response listing the
   test phone number(s). *(VERIFIED — the exact call OxfordConnect makes.)*
4. **Call 2 (create template):** `POST /{waba_id}/message_templates` with a
   template name, language, category and body. **NEEDS VERIFICATION:** confirm
   the exact request body against Meta's current Message Templates reference
   before recording; not written here to avoid inventing a payload.
5. Response showing the new template's `id` and `status` (e.g. PENDING).
6. **Call 3 (confirm):** `GET /{waba_id}/message_templates` showing the new
   template listed — the same edge OxfordConnect reads.
7. Optional: WhatsApp Manager → Message templates showing the same template.

## D–E. API calls

| # | Call | Status |
|---|---|---|
| 1 | `GET /{waba_id}/phone_numbers` | VERIFIED (used by OxfordConnect) |
| 2 | `POST /{waba_id}/message_templates` | NEEDS VERIFICATION (payload from current Meta docs) |
| 3 | `GET /{waba_id}/message_templates` | VERIFIED (used by OxfordConnect) |

Use the app's configured Graph version (**v21.0**, `app/config.py`) or state the
version used on screen.

## F. Visible on screen

App name OxfordConnect; the WABA ID (test); request method and path; responses;
the created template's id/status.

## G. Must NOT be visible

- Any access token (keep the token field masked / collapsed; blur if needed).
- App secret, System User token, PINs.
- Production customer data or Oxford's production WABA details (unless
  authorized).

## H. Narration (draft)

> "This is OxfordConnect, a CRM that onboards businesses as a WhatsApp Tech
> Provider. With whatsapp_business_management, first we read the phone numbers
> of the WhatsApp Business Account the customer shared — the same call our
> server uses during Embedded Signup to verify the number. Next we create a
> message template on that account through the API, and finally we list the
> account's templates to confirm it was created."

## I. Duration

About 60–120 seconds.

## J. Evidence checklist

- [ ] App name OxfordConnect visible
- [ ] Phone-numbers call and response
- [ ] Template creation call and response (id/status)
- [ ] Template listed afterwards
- [ ] No token / secret / PIN visible anywhere
- [ ] Separate file from Video 2 ("separate unique video")
