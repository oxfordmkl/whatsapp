# 12 — Needs Input / Decisions

| # | Item | Type | Owner |
|---|---|---|---|
| 1 | Keep or remove `business_management`, `manage_app_solution`, `whatsapp_business_manage_events`, `public_profile` from the draft | DECISION | Operator |
| 2 | App icon 1024×1024 (none in repo) | INPUT | Operator |
| 3 | Real data-deletion page URL + content (entity, contact, process, response time) | INPUT | Operator |
| 4 | Privacy policy discloses Gemini / Google Sheets processing | VERIFICATION | Operator |
| 5 | Retention period | INPUT | Operator |
| 6 | Dedicated test workspace + reviewer test user + test WhatsApp number | INPUT (needs its own authorization — a connected RC2.5.19-E canary tenant now exists, but reviewer use of it is NOT authorized; operator decision) | Operator |
| 7 | Reviewer test URL (production host vs separate deployment) | DECISION | Operator |
| 8 | Whether reviewers get a flagged workspace to run Embedded Signup | DECISION | Operator |
| 9 | Video 1 API tool (Graph API Explorer vs Postman) and exact template-creation payload from current Meta docs | DECISION + VERIFICATION | Operator |
| 10 | Test WABA for Video 1 (not production unless authorized) | INPUT | Operator |
| 11 | Access Verification (deadline 26/11/2026) | ACTION (separately authorized) | Operator |
| 12 | Customer description for the FAQ | INPUT | Operator |
| 13 | Valid OAuth Redirect URIs currently empty — RESOLVED: the RC2.5.19-E live canary's code exchange succeeded without `redirect_uri` | RESOLVED (live canary) | — |

---

## Final Readiness Matrix

| Item | Status | Evidence | Next action |
|---|---|---|---|
| Business Verification | READY | Verification page: "OxfordBiz … Verified"; Tech Provider step 1 "Approved" | None |
| Tech Provider onboarding | NOT READY | "1 of 2 steps complete"; step 2 App Review outstanding | Complete App Review |
| App icon | NOT READY | "Currently ineligible for submission … App icon (1024 x 1024)" | Provide asset (#2) |
| Privacy Policy URL | READY | `https://theoxfordedu.com/privacy-policy` | Confirm disclosures (#4) |
| Terms URL | READY | `https://theoxfordedu.com/terms` | None |
| Data deletion URL | NOT READY | Placeholder `https://www.facebook.com/` | Build page (#3) |
| App category | READY | Education | None |
| ES configuration | READY | "Oxford WhatsApp Embeded Signup" 1495109199334833; App Setup green | None |
| ES v4 | READY | Builder: ES Version v4 | None |
| Session Info v3 | READY | Builder: Session Info Version 3 | None |
| Allowed domain | READY | `https://web-production-d03fb.up.railway.app/` | Add any future domain |
| OAuth Redirect URI | READY | Valid OAuth Redirect URIs empty; live canary code exchange succeeded without `redirect_uri` (#13) | None |
| Admin System User | READY | `oxford-whatsapp` Role Admin; app + WABA Full access | None |
| System User token | READY (Meta) / PROVISIONED (Railway) | Debugger valid, 3 scopes (operator); `META_SYSTEM_USER_TOKEN` set and used successfully for `debug_token` in the RC2.5.19-E live canary (value not recorded here) | None |
| whatsapp_business_management | NOT READY | "Ready for testing", 1 requirement | Video 1 + submit |
| whatsapp_business_messaging | NOT READY | "Ready for testing", 1 requirement | Video 2 + submit |
| Review video 1 | NOT READY | Not recorded; script prepared | #9, #10 |
| Review video 2 | NOT READY | Not recorded; script prepared | #6 |
| Testing instructions | NEEDS INPUT | Draft prepared | #6, #7, #8 |
| Access Verification | NOT READY | Not started; deadline 26/11/2026 | #11 |
| App Review submission | NOT READY | "Not submitted"; app ineligible (icon) | After all above |
