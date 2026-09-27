# 08 — Data Handling Description (reviewer-facing draft)

| Topic | Description | Status |
|---|---|---|
| Data received | Incoming WhatsApp messages (sender number, profile name, message text / interactive replies, message id) for the business's connected number | VERIFIED (`webhook.py`) |
| Why processed | To show conversations in the business's CRM, send the business's replies, and track leads | VERIFIED |
| Tenant association | Each message is filed under the workspace that owns the receiving phone number ID; unknown numbers are dropped | VERIFIED |
| Credentials | WhatsApp access tokens stored encrypted (Fernet / MultiFernet); decrypted only to call the API | VERIFIED (`encryption_service.py`) |
| Secrets in logs | Tokens, authorization codes and PINs are not logged, audited or returned; phone numbers masked in webhook logs; exception text not logged | VERIFIED (RC2.5.19-C/D/E code + tests) |
| PIN | The two-step PIN is used only for phone registration, is sent to Meta only when the number is not already registered, and is never stored, logged or audited. It is not sent when Meta already reports the number `CONNECTED` (registration is skipped) | VERIFIED (`tenant.py` / `embedded_signup_service.py`) |
| Access control | CRM access requires login; workspace admins act only on their own workspace; the webhook requires Meta's signature | VERIFIED |
| Messages sent | Sent via the Cloud API with the workspace's own number | VERIFIED |
| Third-party processing | Message text may be sent to Google Gemini to generate replies, and lead data to Google Sheets | VERIFIED in code (`ai_service`, `crm_service`) — **NEEDS INPUT:** confirm this is disclosed in the privacy policy |
| Retention period | Not defined in code | NEEDS INPUT (do not invent) |
| Deletion on request | No deletion mechanism exists in the app | NEEDS INPUT — see `09_DATA_DELETION_PAGE_SPEC.md` |
