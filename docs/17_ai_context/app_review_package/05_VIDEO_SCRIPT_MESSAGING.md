# 05 — Review Video 2: `whatsapp_business_messaging`

Meta requirement (dashboard wording): *"record a video showing a message sending
from your app to a WhatsApp number. The video should show your app sending the
message, as well as the WhatsApp interface (either web or mobile app) receiving
the same message."*

## A. Objective

Show a message sent from the OxfordConnect CRM (lead conversation screen) and
the same message arriving in WhatsApp on the recipient's phone or WhatsApp Web.
Optionally show the reverse: a reply from WhatsApp appearing in the CRM.

## B. Preconditions

- A **dedicated test** OxfordConnect workspace with a connected test WhatsApp
  number — NEEDS INPUT. (Production Oxford data must NOT be used unless
  explicitly authorized later. A connected RC2.5.19-E canary tenant now
  exists, but reviewer use of that tenant is not authorized. See
  `12_NEEDS_INPUT.md` for the remaining reviewer workspace/test-user/test-number
  decision.)
- A test recipient WhatsApp number that has messaged the business within the
  last 24 hours, so a free-form message can be sent — NEEDS INPUT.
  *(The customer-service window is a Meta rule; outside it a template message
  would be needed.)*
- A test CRM user with access to that lead — NEEDS INPUT.

**Operational warning — automatic follow-ups (VERIFIED, RC2.5.19-E smoke
test).** A message from a new lead triggers the application's normal
follow-up scheduling: the smoke test created day 1, day 3 and day 7 follow-up
jobs for the test number. The application currently has no built-in cancel
function for these jobs. Use an explicitly controlled test lead/number, and
the operator must account for the scheduled jobs after recording.

## C. CRM screen sequence (VERIFIED route)

1. Log in to the CRM as the test user (`/crm/login`).
2. Open the test lead's detail page (`/crm/lead/<phone>`).
3. Type a clearly identifiable message, e.g. "OxfordConnect review test
   [time]".
4. Send it — this posts to `POST /crm/lead/<phone>/send`, which calls
   `send_text(..., tenant_id=lead.tenant_id)` with the workspace's own number.
5. The message appears in the lead's conversation timeline.

## D. WhatsApp recipient sequence

1. Split screen, or cut to the recipient's WhatsApp (mobile or WhatsApp Web).
2. The chat with the business number is open.
3. The same message arrives, with visible timestamp.

## E–F. Send and receipt

Keep the CRM and WhatsApp on screen together if possible; otherwise show the
send, then immediately the receipt, with matching text and time.

## G. Evidence the message came from OxfordConnect

- The CRM URL/domain and the OxfordConnect workspace are visible at send time.
- The unique message text and timestamp match on both sides.
- Optional: the business display name in WhatsApp matches the connected number.

## Optional inbound segment (VERIFIED behaviour)

Reply from WhatsApp; the reply appears in the CRM conversation timeline
(webhook → conversation history).

## H. Narration (draft)

> "This is OxfordConnect, a CRM for WhatsApp customer conversations. From this
> customer's conversation in the CRM, a staff member sends a reply. The CRM
> sends it through the WhatsApp Cloud API using this business's connected
> number, and here it arrives in WhatsApp on the customer's phone. When the
> customer replies, the message is delivered to our webhook and appears in
> the same conversation."

## I. Duration

About 60–90 seconds.

## J. Evidence checklist

- [ ] OxfordConnect CRM visible, test workspace only
- [ ] Message composed and sent from the CRM
- [ ] Same message received in WhatsApp (web or mobile)
- [ ] Matching text + timestamp
- [ ] No real customer data, no tokens, no phone numbers beyond the test ones
- [ ] Separate file from Video 1
