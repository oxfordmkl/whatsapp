# 06 — Testing Instructions (reviewer-facing draft)

All bracketed values are **NEEDS INPUT**. No credentials are invented here.

---

**About OxfordConnect.** OxfordConnect is a multi-tenant CRM that lets
businesses manage customer conversations on WhatsApp. Each business has its own
workspace and connects its own WhatsApp Business Account.

**Access.**
1. Open [REVIEWER TEST URL] — NEEDS INPUT (current production host is
   `https://web-production-d03fb.up.railway.app`; whether reviewers use it or a
   separate test deployment is NEEDS DECISION).
2. Log in at `/crm/login` with [TEST USER EMAIL] / [TEST PASSWORD] — NEEDS INPUT
   (a dedicated reviewer test user in a dedicated test workspace; not created
   yet).

**Messaging test (`whatsapp_business_messaging`).**
1. Send a WhatsApp message to the workspace's number [TEST WHATSAPP NUMBER]
   (NEEDS INPUT) from your own WhatsApp.
2. In the CRM, open the new conversation (Leads → the contact).
   Expected: your message appears in the conversation, and the CRM's automated
   reply is delivered to your WhatsApp.
3. From the conversation screen, type a message and send it.
   Expected: the message arrives in your WhatsApp.

**Account connection (`whatsapp_business_management`).**
[Embedded Signup test path — NEEDS DECISION. Embedded Signup is implemented but
disabled in production (feature flag OFF) and requires a test workspace with no
existing number. Whether reviewers are given a flagged test workspace to run it
is an operator decision.]

**Notes.**
- WhatsApp's 24-hour customer-service window applies to free-form replies.
- Automatic follow-ups: a message from a new lead can trigger the
  application's automatic follow-up jobs. The current workflow schedules
  day 1, day 3 and day 7 follow-up messages to that number. The application
  has no built-in function to cancel them, so the reviewer and operator should
  account for this before performing a live messaging test.
- Please use only the provided test workspace.
