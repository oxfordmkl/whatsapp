# 02 — Justification: `whatsapp_business_management`

## Reviewer-facing draft (VERIFIED against the implementation)

> OxfordConnect is a multi-tenant CRM used by businesses to manage their
> customer conversations on WhatsApp. We are onboarding as a Tech Provider:
> each business customer connects its own WhatsApp Business Account to its
> own OxfordConnect workspace through Meta's Embedded Signup (v4).
>
> We need `whatsapp_business_management` to complete that onboarding
> server-side for the customer who has just authorized our app:
>
> 1. **Verify which WhatsApp Business Account was shared.** After the
>    customer finishes Embedded Signup, our server exchanges the returned code
>    for the customer's business token and calls `GET /debug_token` to confirm
>    the WhatsApp Business Account appears in the token's granted
>    `whatsapp_business_management` scope. We bind nothing the customer did not
>    grant.
> 2. **Confirm the phone number belongs to that account.** We call
>    `GET /{waba_id}/phone_numbers` and connect the number only if it is listed
>    under the verified account.
> 3. **Subscribe our app to the customer's account** with
>    `POST /{waba_id}/subscribed_apps`, so that the customer's incoming
>    messages are delivered to our webhook and appear in their CRM.
>
> Each customer's account is connected to exactly one OxfordConnect workspace,
> and the connection is performed only by that workspace's own administrator.

## Do NOT claim (not implemented)

- Creating, editing or deleting message templates **from OxfordConnect** —
  NOT IMPLEMENTED. The app only *lists* templates (`GET
  /{WABA_ID}/message_templates`, primary tenant only). See
  `04_VIDEO_SCRIPT_MANAGEMENT.md` for how the required template video is
  handled.
- Managing QR codes, business profiles, or account settings — NOT IMPLEMENTED.

## Status

| Item | Status |
|---|---|
| Endpoints above | VERIFIED (`embedded_signup_service.py`) |
| Live behaviour with real Meta accounts | VERIFIED for one live canary (RC2.5.19-E, 2026-09-27): `debug_token` WABA proof, `/{waba_id}/phone_numbers` phone proof and `subscribed_apps` all succeeded. One WABA and one number only; not evidence for every account. E feature flag OFF again since closure. |
