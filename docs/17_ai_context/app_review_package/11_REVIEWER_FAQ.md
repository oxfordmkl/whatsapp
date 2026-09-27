# 11 — Reviewer FAQ (answers from verified implementation only)

**Why is `whatsapp_business_management` required?**
To complete Embedded Signup for the customer who authorized us: confirm via
`debug_token` which WhatsApp Business Account was granted, confirm the phone
number belongs to it (`/{waba_id}/phone_numbers`), and subscribe our app to it
(`/{waba_id}/subscribed_apps`). — VERIFIED

**Why is `whatsapp_business_messaging` required?**
To receive the business's customer messages at our webhook, send the
business's replies from the CRM, and register the customer's number
(`/{phone_number_id}/register`) when Meta does not already report it
registered; if it does, registration is skipped. — VERIFIED (code and tests)

**Why does the app need Embedded Signup?**
So each business connects its own WhatsApp Business Account itself, and we
receive a token scoped to that business, instead of businesses sharing
credentials manually. — VERIFIED (implemented; disabled in production until
review is complete)

**Who are the customers being onboarded?**
Businesses that use OxfordConnect as their CRM. — NEEDS INPUT (describe the
target businesses, e.g. education institutes, if that is the intended market)

**How does a customer authorize OxfordConnect?**
The business's own administrator, signed in to their OxfordConnect workspace,
completes Meta's Embedded Signup and then enters their number's two-step PIN
to activate it. The PIN is sent to Meta only if the number still needs
registering; if Meta already reports it registered, only the subscription is
made. Platform staff and impersonated sessions cannot run it. —
VERIFIED

**How are customer WhatsApp accounts isolated?**
Each phone number and each WhatsApp Business Account can be connected to only
one workspace (unique database constraints); the workspace is taken from the
signed-in session, never from the request; incoming messages are filed by the
receiving phone number only. — VERIFIED

**How does the app send messages?**
`POST /{phone_number_id}/messages` with the workspace's own number and
encrypted-at-rest credential. — VERIFIED

**How does the app receive messages?**
Meta webhook `POST /webhook`, verified with `X-Hub-Signature-256` before any
processing; each message id is recorded once. — VERIFIED

**What happens if a customer disconnects?**
The workspace admin's "Disconnect WhatsApp" removes the phone number binding,
stored token and connection details; conversation history is kept. If the
customer removes our app in Meta Business Settings, the token stops working and
the workspace is marked "reconnect required" when detected. — VERIFIED

**How is access controlled?**
Login required; roles (admin / staff / platform admin); admins act only on their
own workspace; connection changes are audited. — VERIFIED
