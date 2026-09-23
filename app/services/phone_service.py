"""Phase RC2.5.17 Gate B: the ONE canonical form of a phone destination.

PROMOTED, NOT REWRITTEN
-----------------------
This is app.routes.public.normalize_user_phone moved verbatim, as RC2.5.15
said it should be:

    "NOT promoted to a service module yet: registration is the only writer in
     this phase. The phase that adds phone login should move it, with its
     tests."

That phase is now. The durable rate limiter keys buckets on a destination, and
a limiter that canonicalises differently from the writer would meter a
different subject than the challenge records -- so the rule has to live in one
place that both a service and a route may import. A route module was the wrong
home for it the moment anything below the route layer needed it.

WHY CANONICALISATION IS A SECURITY CONTROL HERE, NOT A TIDINESS CONCERN
-----------------------------------------------------------------------
Without it "+919847312534", "919847312534" and "09847312534" are three bucket
identities for ONE phone, and the approved 10-per-hour ceiling silently becomes
30 for anyone who varies the spelling. The bypass needs no skill and leaves no
trace.
"""

#: Maximum stored length -- matches User.phone and OtpChallenge.destination,
#: both String(20). A longer input is refused rather than truncated: a silently
#: cut number is a WRONG number, and a wrong number is worse than an absent one.
_PHONE_MAX_LEN = 20


def normalize_destination(raw):
    """Normalise a phone destination to a stored digit string. "" if unusable.

    Deliberately NOT admin.normalize_lead_phone(). That function serves the
    CUSTOMER domain and unconditionally prefixes "91", which is correct there:
    an Indian education business's leads are domestic, and the rule exists so a
    hand-typed walk-in collides with the same row as an inbound WhatsApp
    message. Applying it here would silently turn a tenant owner's "+1 555 012
    3456" into "915550123456" -- a real, different, Indian number. Storing a
    corrupted identity is worse than storing none, so this rule differs in
    exactly one respect:

        input begins with "+"  -> already international; keep the digits as-is
        otherwise              -> domestic: strip leading zeros, prefix 91

    The "+" case is not hypothetical: the registration form's own placeholder
    reads "+91 98765 43210", so the form actively invites that spelling.

    The domestic branch is byte-for-byte the existing rule, so a user who
    enters a bare Indian number is stored in the SAME form as the lead tables
    use. That matters for a later phase that may need to relate the two.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    international = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if international:
        # Trust the caller's country code. Leading zeros are not stripped:
        # in an E.164 number there are none to strip, and removing a digit
        # from an explicit international number would corrupt it.
        return digits if len(digits) <= _PHONE_MAX_LEN else ""
    digits = digits.lstrip("0")
    if not digits:
        return ""
    if not digits.startswith("91"):
        digits = "91" + digits
    return digits if len(digits) <= _PHONE_MAX_LEN else ""


def mask_destination(destination: str) -> str:
    """Last 3 digits only, for logs. Never the full number.

    Duplicated deliberately from otp_service rather than imported: this module
    must not depend on the OTP primitive, because the rate limiter uses it and
    the limiter is explicitly NOT allowed to couple to otp_service. The rule is
    three characters long and identical in both places; a shared import would
    buy less than the coupling costs.
    """
    s = str(destination or "")
    return ("*" * max(0, len(s) - 3)) + s[-3:] if s else ""
