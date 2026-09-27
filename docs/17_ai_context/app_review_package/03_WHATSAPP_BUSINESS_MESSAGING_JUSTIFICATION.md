# 03 — Justification: `whatsapp_business_messaging`

## Reviewer-facing draft (VERIFIED against the implementation)

> OxfordConnect is a WhatsApp-enabled CRM. Businesses use it to answer and
> manage conversations with their own customers through the WhatsApp Cloud
> API.
>
> We need `whatsapp_business_messaging` to:
>
> 1. **Receive customer messages.** Incoming messages arrive at our webhook,
>    which verifies Meta's `X-Hub-Signature-256` signature, identifies the
>    business by the receiving phone number, and records each message once
>    in that business's conversation history.
> 2. **Send replies from the CRM.** Business staff reply from the lead's
>    conversation screen; the CRM sends the message through
>    `POST /{phone_number_id}/messages` using that business's own connected
>    number and credential. Automated replies to incoming messages use the
>    same path.
> 3. **Register the customer's phone number** during onboarding when it is
>    not yet registered (`POST /{phone_number_id}/register`, with the two-step
>    PIN the customer enters), which is required before the number can use the
>    Cloud API. We first read the number's status from Meta; if it is already
>    registered, we skip registration and do not send the PIN.
>
> Messages are always sent with the business's own connected number and
> credential; one business's messages are never sent or received under
> another business.

## Implementation references (VERIFIED)

- Inbound: `app/routes/webhook.py` (`verify_meta_signature`, per-change tenant
  resolution by `metadata.phone_number_id`, wamid idempotency).
- Outbound: `app/services/whatsapp_service.py` (`send_text`, `send_interactive`,
  `send_list`, `send_template`, `upload_media`), credentials resolved per tenant
  by `_get_waba_credentials(tenant_id)`.
- Manual CRM send: `POST /crm/lead/<phone>/send` (`app/routes/admin.py`,
  `crm_lead_send`) → `send_text(..., tenant_id=lead.tenant_id)`.
- Registration: `embedded_signup_service.activate()` (since `42cb4126`) first
  reads `GET /{phone_id}?fields=status`:
  - `CONNECTED` → `/register` skipped (PIN not sent), then `subscribed_apps`;
  - any other status → `register_phone` (with PIN), then `subscribed_apps`;
  - status read fails → nothing sent; tenant stays pending activation.
  Covered by tests and mutation checks. In the RC2.5.19-E live canary the
  recovery path completed with one request; the skipped `/register` is
  supported by strong but indirect evidence (Meta reported `CONNECTED`
  beforehand; successful Graph calls are not logged), not by a log line.

## Do NOT claim

- Calling / voice features — NOT IMPLEMENTED.
- Media *download* of inbound media — NOT IMPLEMENTED (inbound image/audio/video
  messages are currently ignored).
