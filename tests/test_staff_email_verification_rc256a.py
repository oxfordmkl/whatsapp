"""Phase RC2.5.6a (P1-7): a STAFF account can verify its email and sign in.

WHY
---
/crm/login admits ADMIN and STAFF and refuses both until email_verified_at is
set. /verify-email/<token> already accepts any user's token, but nothing ever
sent a STAFF account one: /tenant/staff created the login and sent nothing,
and /resend-verification looked up role="ADMIN" only. Production STAFF could
sign in only because migration 21aed923eafb bulk-verified every existing user
on 2026-07-11; a STAFF member created after that could never log in.

THE FIX, AND WHAT IT DELIBERATELY DOES NOT TOUCH
------------------------------------------------
  * tenant_staff() sends the EXISTING verification email once, after the
    commit, with registration's try/except -- a failed send never removes
    the account;
  * resend_verification() accepts ADMIN and STAFF (SUPER_ADMIN stays out);
  * the token, /verify-email, /crm/login and email_service are unchanged,
    and this suite pins their behaviour for STAFF so a later change cannot
    quietly weaken it.

No real email is sent: send_verification_email is replaced by a recorder.
Tokens are minted with the real serializer, so what /verify-email accepts is
exactly what production would. Every secret below is a dummy.
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "rc256a_staff_email_verification.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc256a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc256a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc256a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-a")
# Belt and braces: with no key the real provider returns False before any
# network call, so even an unstubbed path in this suite can never send mail.
os.environ["BREVO_API_KEY"] = ""
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from itsdangerous import URLSafeTimedSerializer                              # noqa: E402
from werkzeug.security import generate_password_hash                         # noqa: E402
from app import create_app                                                   # noqa: E402
from app.extensions import db                                                # noqa: E402
from app.models import AuditLog, Tenant, User                                # noqa: E402
from app.services.email_service import email_service                         # noqa: E402

A, B = "t-a", "t-b"
PW = "Rc256a#Login-pw"
RESEND_FLASH = "If an unverified account with that email exists, a verification email has been sent."

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

# The routes import email_service inside the function, at call time, from
# sys.modules. Another test module that purges and re-imports `app` would
# otherwise leave those imports resolving to ITS copies -- not the instance
# this suite stubs, nor the rate-limit dict it resets. Re-pinning this
# module's own copies before every test makes a combined run behave exactly
# like the file-by-file run CI uses.
_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _mk(tenant, username, role, verified, active=True):
    u = User(username=username, display_name=username,
             email=f"{username}@rc256a.test",
             password_hash=generate_password_hash(PW), role=role,
             tenant_id=tenant, is_active=active, require_password_change=False,
             email_verified_at=datetime.utcnow() if verified else None)
    db.session.add(u)
    db.session.commit()
    return u.id


@pytest.fixture()
def ids():
    """Holds no app context across requests (see the 14B.1 fixture defect)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((A, "Tenant A"), (B, "Tenant B")):
            db.session.add(Tenant(id=tid, name=name, slug=f"rc256a-v-{tid}",
                                  status="ACTIVE", billing_exempt=True))
        db.session.commit()
        out = {
            "aadmin": _mk(A, "aadmin", "ADMIN", verified=True),
            "aadmin_unv": _mk(A, "aadmin_unv", "ADMIN", verified=False),
            "astaff_unv": _mk(A, "astaff_unv", "STAFF", verified=False),
            "astaff_ok": _mk(A, "astaff_ok", "STAFF", verified=True),
            "astaff_off": _mk(A, "astaff_off", "STAFF", verified=False, active=False),
            "badmin": _mk(B, "badmin", "ADMIN", verified=True),
            "bstaff_unv": _mk(B, "bstaff_unv", "STAFF", verified=False),
            "super_unv": _mk(None, "super_unv", "SUPER_ADMIN", verified=False),
        }
    # The resend route rate-limits per IP in a module-level dict; every test
    # starts from a clean window so the limit cannot mask a behaviour.
    sys.modules["app.routes.public"]._RATE_LIMITS.clear()
    yield out
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def sent(monkeypatch):
    """Record verification-email attempts instead of sending them."""
    calls = []

    def _record(*args, **kwargs):
        calls.append({"email": kwargs.get("user_email", args[0] if args else None),
                      "name": kwargs.get("user_name", args[1] if len(args) > 1 else None)})
        return True

    monkeypatch.setattr(email_service, "send_verification_email", _record)
    return calls


def client_for(user_id=None):
    c = _APP.test_client()
    if user_id is not None:
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
    return c


def user(uid):
    with _APP.app_context():
        u = db.session.get(User, uid)
        return {"role": u.role, "tenant_id": u.tenant_id, "is_active": u.is_active,
                "verified_at": u.email_verified_at, "email": u.email}


def by_email(email):
    with _APP.app_context():
        u = User.query.filter_by(email=email).first()
        return None if u is None else {"role": u.role, "tenant_id": u.tenant_id,
                                       "verified_at": u.email_verified_at,
                                       "require_password_change": u.require_password_change}


def token_for(email):
    with _APP.app_context():
        return email_service.generate_verification_token(email)


def flashes(client):
    with client.session_transaction() as s:
        return [m for _cat, m in s.get("_flashes", [])]


def signed_in(client):
    with client.session_transaction() as s:
        return "_user_id" in s


def create_staff(admin_id, email, username="newstaff"):
    c = client_for(admin_id)
    r = c.post("/tenant/staff", data={"action": "create", "username": username,
                                      "email": email, "password": PW})
    return c, r


def login(email):
    c = client_for()
    r = c.post("/crm/login", data={"email": email, "password": PW})
    return c, r


# ── 1-4. staff creation sends the existing verification email ───────────────

class TestCreationSendsVerification:

    def test_creation_attempts_exactly_one_verification_email(self, ids, sent):
        _c, r = create_staff(ids["aadmin"], "fresh@rc256a.test")
        assert r.status_code in (302, 303)
        assert len(sent) == 1

    def test_email_goes_to_the_new_staff_address(self, ids, sent):
        create_staff(ids["aadmin"], "fresh@rc256a.test", username="freshname")
        assert sent == [{"email": "fresh@rc256a.test", "name": "freshname"}]

    def test_new_staff_is_created_unverified_in_the_callers_tenant(self, ids, sent):
        create_staff(ids["aadmin"], "fresh@rc256a.test")
        row = by_email("fresh@rc256a.test")
        assert row["role"] == "STAFF" and row["tenant_id"] == A
        assert row["verified_at"] is None
        assert row["require_password_change"] is True

    def test_send_returning_false_keeps_the_staff_and_warns(self, ids, monkeypatch):
        monkeypatch.setattr(email_service, "send_verification_email", lambda **kw: False)
        c, _r = create_staff(ids["aadmin"], "nosend@rc256a.test")
        assert by_email("nosend@rc256a.test") is not None
        msgs = flashes(c)
        assert any("created successfully" in m for m in msgs)
        assert any("could not be sent" in m for m in msgs)

    def test_send_raising_keeps_the_staff_and_warns(self, ids, monkeypatch):
        def _boom(**kw):
            raise RuntimeError("provider down")
        monkeypatch.setattr(email_service, "send_verification_email", _boom)
        c, r = create_staff(ids["aadmin"], "boom@rc256a.test")
        assert r.status_code in (302, 303)
        assert by_email("boom@rc256a.test") is not None, "a failed send removed the account"
        assert any("could not be sent" in m for m in flashes(c))

    def test_refused_creation_sends_nothing(self, ids, sent):
        # duplicate email (global uniqueness) -- the create is refused
        create_staff(ids["aadmin"], "astaff_ok@rc256a.test", username="dup")
        # missing password -- refused by validation
        client_for(ids["aadmin"]).post("/tenant/staff", data={
            "action": "create", "username": "nopw", "email": "nopw@rc256a.test", "password": ""})
        assert sent == []


# ── 5-8, 21-22. /verify-email for a STAFF account ────────────────────────────

class TestVerifyEmailForStaff:

    def test_staff_token_sets_email_verified_at(self, ids):
        r = client_for().get(f"/verify-email/{token_for('astaff_unv@rc256a.test')}")
        assert r.status_code in (302, 303)
        assert user(ids["astaff_unv"])["verified_at"] is not None

    def test_verification_does_not_modify_role_tenant_or_active(self, ids):
        before = user(ids["astaff_unv"])
        client_for().get(f"/verify-email/{token_for('astaff_unv@rc256a.test')}")
        after = user(ids["astaff_unv"])
        assert after["role"] == before["role"] == "STAFF"
        assert after["tenant_id"] == before["tenant_id"] == A

    def test_verification_does_not_activate_an_inactive_account(self, ids):
        client_for().get(f"/verify-email/{token_for('astaff_off@rc256a.test')}")
        assert user(ids["astaff_off"])["is_active"] is False

    def test_token_reuse_is_idempotent(self, ids):
        tok = token_for("astaff_unv@rc256a.test")
        client_for().get(f"/verify-email/{tok}")
        first = user(ids["astaff_unv"])["verified_at"]
        client_for().get(f"/verify-email/{tok}")
        assert user(ids["astaff_unv"])["verified_at"] == first

    def test_tenant_a_verification_does_not_touch_tenant_b(self, ids):
        with _APP.app_context():
            b_before = sorted((u.id, u.email_verified_at, u.role) for u in User.query.filter_by(tenant_id=B))
        client_for().get(f"/verify-email/{token_for('astaff_unv@rc256a.test')}")
        with _APP.app_context():
            assert sorted((u.id, u.email_verified_at, u.role)
                          for u in User.query.filter_by(tenant_id=B)) == b_before
        assert user(ids["bstaff_unv"])["verified_at"] is None


# ── 18-20. tokens that must be refused ───────────────────────────────────────

class TestRejectedTokens:

    def test_expired_token_is_rejected(self, ids, monkeypatch):
        tok = token_for("astaff_unv@rc256a.test")
        monkeypatch.setitem(_APP.config, "VERIFY_EMAIL_EXPIRY_SECONDS", -1)
        client_for().get(f"/verify-email/{tok}")
        assert user(ids["astaff_unv"])["verified_at"] is None

    def test_tampered_token_is_rejected(self, ids):
        tok = token_for("astaff_unv@rc256a.test")
        client_for().get(f"/verify-email/{tok[:-2]}xx")
        client_for().get("/verify-email/not-a-token")
        assert user(ids["astaff_unv"])["verified_at"] is None

    def test_token_signed_with_another_key_is_rejected(self, ids):
        forged = URLSafeTimedSerializer("not-the-app-secret").dumps(
            "astaff_unv@rc256a.test", salt="email-verify")
        client_for().get(f"/verify-email/{forged}")
        assert user(ids["astaff_unv"])["verified_at"] is None

    def test_password_reset_token_is_not_a_verification_token(self, ids):
        with _APP.app_context():
            u = db.session.get(User, ids["astaff_unv"])
            reset_tok = email_service.generate_password_reset_token(u.id, u.password_hash)
            # the same payload shape as a verification token, under the reset salt
            reset_salted = email_service.get_serializer().dumps(u.email, salt="password-reset")
        client_for().get(f"/verify-email/{reset_tok}")
        client_for().get(f"/verify-email/{reset_salted}")
        assert user(ids["astaff_unv"])["verified_at"] is None


# ── 9-12. /crm/login for a STAFF account ─────────────────────────────────────

class TestStaffLogin:

    def test_staff_login_fails_before_verification(self, ids):
        c, r = login("astaff_unv@rc256a.test")
        assert r.headers["Location"].endswith("/crm/login")
        assert not signed_in(c)

    def test_staff_login_succeeds_after_verification(self, ids):
        client_for().get(f"/verify-email/{token_for('astaff_unv@rc256a.test')}")
        c, r = login("astaff_unv@rc256a.test")
        assert r.headers["Location"].endswith("/crm/home")
        assert signed_in(c)
        with _APP.app_context():
            row = AuditLog.query.filter_by(action="LOGIN_SUCCESS").order_by(AuditLog.id.desc()).first()
            assert row is not None and row.tenant_id == A

    def test_staff_login_blocked_when_tenant_suspended(self, ids):
        client_for().get(f"/verify-email/{token_for('astaff_unv@rc256a.test')}")
        with _APP.app_context():
            db.session.get(Tenant, A).status = "SUSPENDED"
            db.session.commit()
        c, r = login("astaff_unv@rc256a.test")
        assert r.headers["Location"].endswith("/crm/login")
        assert not signed_in(c)

    def test_staff_login_blocked_when_user_inactive(self, ids):
        client_for().get(f"/verify-email/{token_for('astaff_off@rc256a.test')}")
        c, r = login("astaff_off@rc256a.test")
        # Existing behaviour: an inactive account falls through to the failure
        # branch, which re-renders the login page (200) rather than redirecting.
        assert r.status_code == 200
        assert not signed_in(c)
        with _APP.app_context():
            row = AuditLog.query.order_by(AuditLog.id.desc()).first()
            assert row.action == "LOGIN_FAILURE"

    def test_end_to_end_created_staff_can_verify_and_sign_in(self, ids, sent):
        create_staff(ids["aadmin"], "e2e@rc256a.test", username="e2e")
        assert [s["email"] for s in sent] == ["e2e@rc256a.test"]
        c, _r = login("e2e@rc256a.test")
        assert not signed_in(c)
        client_for().get(f"/verify-email/{token_for('e2e@rc256a.test')}")
        c, r = login("e2e@rc256a.test")
        assert signed_in(c) and r.headers["Location"].endswith("/crm/home")


# ── 13-17. /resend-verification eligibility ──────────────────────────────────

def resend(email):
    c = client_for()
    r = c.post("/resend-verification", data={"email": email})
    return r, flashes(c)


class TestResend:

    def test_staff_resend_works(self, ids, sent):
        resend("astaff_unv@rc256a.test")
        assert [s["email"] for s in sent] == ["astaff_unv@rc256a.test"]

    def test_staff_resend_in_another_tenant_works(self, ids, sent):
        resend("bstaff_unv@rc256a.test")
        assert [s["email"] for s in sent] == ["bstaff_unv@rc256a.test"]

    def test_admin_resend_still_works(self, ids, sent):
        resend("aadmin_unv@rc256a.test")
        assert [s["email"] for s in sent] == ["aadmin_unv@rc256a.test"]

    def test_super_admin_resend_stays_excluded(self, ids, sent):
        resend("super_unv@rc256a.test")
        assert sent == []

    def test_already_verified_users_get_nothing(self, ids, sent):
        resend("astaff_ok@rc256a.test")
        resend("aadmin@rc256a.test")
        assert sent == []

    def test_unknown_email_gets_nothing(self, ids, sent):
        resend("nobody@rc256a.test")
        assert sent == []

    def test_response_is_identical_whatever_the_account(self, ids, sent):
        seen = set()
        for email in ("astaff_unv@rc256a.test", "aadmin_unv@rc256a.test",
                      "super_unv@rc256a.test", "astaff_ok@rc256a.test",
                      "nobody@rc256a.test"):
            sys.modules["app.routes.public"]._RATE_LIMITS.clear()
            r, msgs = resend(email)
            seen.add((r.status_code, r.headers.get("Location"), tuple(msgs)))
        assert len(seen) == 1, "resend responses differ -- account enumeration"
        (status, location, msgs), = seen
        assert status in (302, 303) and location.endswith("/crm/login")
        assert msgs == (RESEND_FLASH,)

    def test_resend_rate_limit_is_unchanged(self, ids, sent):
        codes = [resend("astaff_unv@rc256a.test")[0].status_code for _ in range(4)]
        assert codes[:3] == [302, 302, 302] and codes[3] == 429
