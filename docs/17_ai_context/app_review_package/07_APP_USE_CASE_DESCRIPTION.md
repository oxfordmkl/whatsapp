# 07 — App Use Case Description (reviewer-facing draft)

> OxfordConnect is a multi-tenant CRM for businesses that talk to their
> customers on WhatsApp. Each business has its own isolated workspace.
>
> **Connecting WhatsApp.** A business administrator connects the business's
> WhatsApp Business Account through Meta's Embedded Signup. Our server verifies
> with Meta that the account was granted to our app and that the phone number
> belongs to it, then registers the number (skipped when Meta already reports
> it registered) and subscribes our app to the account. A number or WhatsApp Business Account can be connected to only one
> workspace.
>
> **Conversations.** Incoming customer messages reach our webhook, which checks
> Meta's signature and files each message once, in the workspace that owns the
> receiving number. Staff read and reply from the CRM, and replies are sent
> through the Cloud API with that workspace's own number and credential.
>
> **Security.** Access tokens are stored encrypted. Workspace data is scoped to
> the workspace; administrators act only on their own workspace. Connection
> changes are recorded in an audit log.

## Verification of each claim

| Claim | Status | Source |
|---|---|---|
| Multi-tenant, isolated workspaces | VERIFIED | tenant-scoped models / routes; `test_tenant_isolation.py` |
| Embedded Signup + server-side verification | VERIFIED (code) / VERIFIED for one live canary: Embedded Signup v4 on Graph v21.0, WABA and phone proof, server-side binding (RC2.5.19-E, 2026-09-27; one WABA/number, not general Meta behaviour) | `embedded_signup_service.py`; flag OFF since closure |
| One number / one WABA per workspace | VERIFIED | unique indexes `uq_tenants_waba_phone_number_id`, `uq_tenants_waba_id` |
| Signature-checked webhook, once-only filing | VERIFIED | `webhook.py`, `uq_conv_msg_incoming_wa_message_id` |
| Replies with the workspace's own credential | VERIFIED | `_get_waba_credentials(tenant_id)` |
| Tokens encrypted | VERIFIED | `encryption_service.py` (MultiFernet) |
| Audit log of connection changes | VERIFIED | `TENANT_SETTINGS_CHANGE` events |
