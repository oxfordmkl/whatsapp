"""Phase RC2.5.15: the registration phone number is stored, not discarded.

WHY
---
templates/public/register.html has rendered a "Phone Number" input since Phase
13-A2B. app/routes/public.py read it into a local variable at register() and
never referenced it again, because User had no column to hold it. Every
business that has ever signed up typed a number that went nowhere.

This phase adds User.phone and User.phone_verified_at, normalises on write,
and persists. It deliberately does NOT add phone login, OTP, passwordless
auth, a uniqueness constraint, or any verification channel -- those are later,
separately authorised phases, and this suite pins their ABSENCE so a later
change cannot arrive unannounced under cover of "foundation".

It also closes the registration enumeration oracle: /register used to answer
"This email is already registered", telling any anonymous visitor whether an
address holds an account -- a fact /crm/login is careful never to reveal.

WHAT THIS SUITE PINS
--------------------
  1. normalisation: domestic numbers match the existing lead-phone rule; an
     explicit international "+" number is NOT mangled into an Indian one;
     unusable input yields "" and is stored as NULL, never "";
  2. persistence: a registration with a phone stores the NORMALISED value;
  3. compatibility: a user created without a phone is valid, and
     phone_verified_at defaults to NULL and is set by nothing;
  4. the existing flow is untouched: tenant created PENDING, ADMIN user,
     provisioning atomic, email verification and SUPER_ADMIN approval intact,
     login behaviour unchanged;
  5. the enumeration oracle is closed, and the duplicate is still not created;
  6. out of scope stays out of scope: no phone login, no OTP, no uniqueness.
"""
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2515_phone_identity.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2515-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2515-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2515-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from werkzeug.security import generate_password_hash                          # noqa: E402
from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, User                                           # noqa: E402
from app.routes.public import normalize_user_phone                            # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


@pytest.fixture(autouse=True)
def _clear_register_throttle():
    """RC2.5.17 Gate A.1 added a per-IP throttle to POST /register.

    _RATE_LIMITS is a process-global dict and every test client here presents
    the same address, so this suite's registrations accumulate into ONE bucket
    and the sixth onward would be refused with 429 -- correct production
    behaviour (one IP registering six businesses in an hour), surfacing here
    only because the suite simulates many businesses from one client.

    Clearing between tests isolates that global; no assertion is relaxed. This
    is the convention test_staff_email_verification_rc256a.py already uses for
    the resend limiter (see its lines 120 and 396).
    """
    import app.routes.public as _public
    _public._RATE_LIMITS.clear()
    yield
    _public._RATE_LIMITS.clear()


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def clean_db():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
    yield
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def _register(client, **over):
    form = {
        "business_name": "Acme Institute",
        "admin_name": "Owner One",
        "email": "owner@rc2515.test",
        "phone": "",
        "industry": "Education",
        "password": "correct horse battery",
    }
    form.update(over)
    # Email dispatch is a real network call in register(); it is already
    # wrapped in try/except there, so it degrades rather than failing the
    # registration. Nothing here asserts on email delivery.
    return client.post("/register", data=form, follow_redirects=False)


# ── 1. normalisation ────────────────────────────────────────────────────────

class TestNormalisation:

    @pytest.mark.parametrize("raw,expected", [
        ("9847312534", "919847312534"),        # bare domestic
        ("09847312534", "919847312534"),       # leading zero stripped
        ("919847312534", "919847312534"),      # already prefixed
        ("98473 12534", "919847312534"),       # spaces
        ("98473-12534", "919847312534"),       # punctuation
        ("(098473) 12534", "919847312534"),    # brackets + zero
    ])
    def test_domestic_matches_the_existing_lead_phone_rule(self, raw, expected):
        assert normalize_user_phone(raw) == expected

    def test_the_domestic_rule_agrees_with_normalize_lead_phone(self):
        """One rule, not two. A user who types a bare Indian number must be
        stored in the SAME form the lead tables use."""
        from app.routes.admin import normalize_lead_phone
        for raw in ("9847312534", "09847312534", "919847312534", "98473 12534"):
            assert normalize_user_phone(raw) == normalize_lead_phone(raw), raw

    @pytest.mark.parametrize("raw,expected", [
        ("+15550123456", "15550123456"),       # US, NOT prefixed with 91
        ("+1 555 012 3456", "15550123456"),
        ("+44 20 7946 0958", "442079460958"),  # UK
        ("+919847312534", "919847312534"),     # explicit India, unchanged
    ])
    def test_an_international_number_is_not_mangled_into_an_indian_one(
            self, raw, expected):
        """THE reason this is not normalize_lead_phone(). That function would
        turn "+15550123456" into "9115550123456" -- a different, real, Indian
        number. A corrupted identity is worse than an absent one."""
        assert normalize_user_phone(raw) == expected

    def test_an_international_number_does_not_gain_a_91_prefix(self):
        assert not normalize_user_phone("+15550123456").startswith("91")

    @pytest.mark.parametrize("raw", ["", "   ", None, "abc", "---", "+", "000", "+abc"])
    def test_unusable_input_yields_empty_string(self, raw):
        assert normalize_user_phone(raw) == ""

    def test_an_overlong_number_is_refused_not_truncated(self):
        """String(20) would truncate silently, and a cut number is a WRONG
        number. Refusing stores NULL instead, which is honest."""
        assert normalize_user_phone("+" + "1" * 25) == ""
        assert normalize_user_phone("9" * 30) == ""

    def test_every_result_fits_the_column(self):
        for raw in ("9847312534", "+442079460958", "09847312534", "+15550123456"):
            assert len(normalize_user_phone(raw)) <= 20, raw


# ── 2. persistence through the real registration flow ───────────────────────

class TestRegistrationPersistsThePhone:

    def test_a_supplied_phone_is_stored_normalised(self, clean_db):
        _register(_APP.test_client(), phone="098473 12534")
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u is not None, "registration did not create the user"
            assert u.phone == "919847312534"

    def test_an_international_phone_survives_registration_intact(self, clean_db):
        _register(_APP.test_client(), email="intl@rc2515.test",
                  phone="+1 555 012 3456")
        with _APP.app_context():
            u = User.query.filter_by(email="intl@rc2515.test").first()
            assert u.phone == "15550123456"

    def test_a_blank_phone_stores_null_not_empty_string(self, clean_db):
        """Two spellings of "no phone" would defeat any future uniqueness
        rule, so "" must never reach the column."""
        _register(_APP.test_client(), phone="")
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u.phone is None

    def test_an_unusable_phone_stores_null_and_does_not_block_signup(self, clean_db):
        """Phone is optional. Garbage in the field must not cost the tenant
        its registration."""
        r = _register(_APP.test_client(), phone="not a phone")
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u is not None, "an unusable phone blocked registration"
            assert u.phone is None
        assert r.status_code in (301, 302)

    def test_phone_verified_at_is_never_set_by_registration(self, clean_db):
        """Supplying a number proves nothing about holding it."""
        _register(_APP.test_client(), phone="9847312534")
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u.phone_verified_at is None

    def test_nothing_in_the_application_sets_phone_verified_at(self):
        """No verification channel exists yet; this phase must not imply one."""
        import glob
        for path in glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                              recursive=True):
            src = open(path, encoding="utf-8", errors="replace").read()
            assert "phone_verified_at =" not in src.replace(
                "phone_verified_at = db.Column", ""), path


# ── 3. compatibility with existing rows ─────────────────────────────────────

class TestExistingRowCompatibility:

    def test_a_user_without_a_phone_is_valid(self, clean_db):
        """All 21 production users get NULL. If NULL were rejected the
        migration would break every existing login."""
        with _APP.app_context():
            db.session.add(Tenant(id="t-a", name="A", slug="rc2515-a",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
            u = User(username="legacy", email="legacy@rc2515.test",
                     password_hash=generate_password_hash("pw"),
                     role="ADMIN", tenant_id="t-a", is_active=True)
            db.session.add(u)
            db.session.commit()
            assert u.phone is None and u.phone_verified_at is None

    def test_two_users_may_share_a_phone_today(self, clean_db):
        """NOT an endorsement of duplicates -- a pin on the DELIBERATE absence
        of a uniqueness constraint. The policy is undecided (email is globally
        unique, username is per-tenant), and nothing authenticates on phone
        yet. If a later phase adds a constraint, this test must be updated
        DELIBERATELY, which is the point."""
        with _APP.app_context():
            db.session.add(Tenant(id="t-a", name="A", slug="rc2515-a",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
            for i in (1, 2):
                db.session.add(User(
                    username=f"u{i}", email=f"u{i}@rc2515.test",
                    password_hash=generate_password_hash("pw"),
                    role="ADMIN", tenant_id="t-a", is_active=True,
                    phone="919847312534"))
            db.session.commit()
            assert User.query.filter_by(phone="919847312534").count() == 2


# ── 4. the existing registration flow is unchanged ──────────────────────────

class TestExistingFlowPreserved:

    def test_the_tenant_is_still_created_pending(self, clean_db):
        """SUPER_ADMIN approval must still stand between signup and access."""
        _register(_APP.test_client(), phone="9847312534")
        with _APP.app_context():
            t = Tenant.query.filter_by(name="Acme Institute").first()
            assert t is not None and t.status == "PENDING"

    def test_the_first_user_is_still_an_admin_of_that_tenant(self, clean_db):
        _register(_APP.test_client())
        with _APP.app_context():
            t = Tenant.query.filter_by(name="Acme Institute").first()
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u.role == "ADMIN" and u.tenant_id == t.id
            assert u.is_active is True

    def test_provisioning_still_ran_in_the_same_transaction(self, clean_db):
        """provision_tenant() joins registration's transaction; a tenant that
        exists must be fully provisioned."""
        from app.services.tenant_provisioning_service import is_provisioned
        _register(_APP.test_client())
        with _APP.app_context():
            t = Tenant.query.filter_by(name="Acme Institute").first()
            assert is_provisioned(t.id), "tenant created but not provisioned"

    def test_email_verification_is_still_required(self, clean_db):
        _register(_APP.test_client())
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2515.test").first()
            assert u.email_verified_at is None

    def test_a_registration_missing_required_fields_is_still_refused(self, clean_db):
        _register(_APP.test_client(), email="", password="")
        with _APP.app_context():
            assert User.query.count() == 0
            assert Tenant.query.count() == 0

    def test_login_still_refuses_a_pending_tenant(self, clean_db):
        """The four login gates are untouched by this phase."""
        _register(_APP.test_client())
        c = _APP.test_client()
        r = c.post("/crm/login", data={"email": "owner@rc2515.test",
                                       "password": "correct horse battery"},
                   follow_redirects=False)
        assert r.status_code in (301, 302)
        assert c.get("/crm/home").status_code in (302, 303, 403)

    def test_the_login_route_still_resolves_users_by_email(self):
        """This phase adds a phone COLUMN, not a phone LOGIN."""
        src = _read("app/routes/admin.py")
        assert "User.query.filter_by(email=email).first()" in src


# ── 5. the enumeration oracle is closed ─────────────────────────────────────

class TestEnumerationOracleClosed:

    def test_a_duplicate_email_no_longer_announces_itself(self, clean_db):
        """Asserted against what the route FLASHES, not the raw file text: the
        comment recording why this changed necessarily quotes the old string,
        and a naive substring check matches its own explanation."""
        src = _read("app/routes/public.py")
        flashed = re.findall(r"flash\(\s*(['\"])(.*?)\1", src, re.S)
        messages = [m[1] for m in flashed]
        assert not any("already registered" in m for m in messages), messages

    def test_the_duplicate_response_is_indistinguishable_from_success(self, clean_db):
        c = _APP.test_client()
        first = _register(c)
        second = _register(c)
        assert first.status_code == second.status_code
        assert first.headers.get("Location") == second.headers.get("Location")

    def test_the_duplicate_is_still_not_created(self, clean_db):
        """Closing the oracle must not have made the route permissive."""
        c = _APP.test_client()
        _register(c)
        _register(c, business_name="Second Attempt")
        with _APP.app_context():
            assert User.query.filter_by(email="owner@rc2515.test").count() == 1
            assert Tenant.query.filter_by(name="Second Attempt").count() == 0

    def test_a_genuinely_new_registration_still_succeeds(self, clean_db):
        c = _APP.test_client()
        _register(c)
        _register(c, email="second@rc2515.test", business_name="Beta Institute")
        with _APP.app_context():
            assert User.query.count() == 2
            assert Tenant.query.count() == 2


# ── 6. out of scope stays out of scope ──────────────────────────────────────

class TestScope:

    def test_the_model_declares_both_columns_nullable_and_not_unique(self):
        src = _read("app/models.py")
        assert "phone = db.Column(db.String(20), nullable=True, index=True)" in src
        assert "phone_verified_at = db.Column(db.DateTime, nullable=True, index=True)" in src
        i = src.index("phone = db.Column")
        assert "unique=True" not in src[i:i + 200]

    def test_no_otp_concept_was_introduced(self):
        """INVERTED BY RC2.5.16, which is the phase authorised to introduce
        the OTP primitive. This test did its job first: it failed, and blocked
        that phase until the exception was made deliberately here.

        RC2.5.15's actual intent survives unchanged -- OTP must not reach the
        registration or login paths -- so the assertion narrows from "nowhere"
        to "only in the primitive's own files". A stray OTP reference in
        public.py or admin.py still fails, which is what this ever guarded.

        Word boundaries are still required: a bare substring search hits
        "fo-otp-rint" in app/state.py:56, the false positive the RC2.5.14 Gate
        A audit had to rule out before it could report that no OTP existed.

        WIDENED AGAIN BY RC2.5.17 Gate B, by two files and for prose only:
        phone_service.py and rate_limit_service.py each EXPLAIN, in their
        docstrings, why they do not depend on the OTP primitive -- the
        limiter's whole separation argument is written there. Neither contains
        OTP code, and test_the_limiter_does_not_import_the_otp_primitive in
        test_durable_rate_limit_rc2517b.py asserts that against docstring-
        stripped source, which is the stronger check. The line below stays
        untouched: OTP must still not reach public.py or admin.py.
        """
        import glob
        allowed = {"app/services/otp_service.py", "app/models.py",
                   "app/config.py", "app/services/phone_service.py",
                   "app/services/rate_limit_service.py"}
        for path in glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                              recursive=True):
            rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
            if rel in allowed:
                continue
            src = open(path, encoding="utf-8", errors="replace").read()
            hits = re.findall(r"\bOTP\b|\botp\b", src)
            assert not hits, f"{rel}: {hits}"

    def test_otp_did_not_reach_registration_or_login(self):
        """The half of the RC2.5.15 pin that must never be relaxed."""
        for rel in ("app/routes/public.py", "app/routes/admin.py"):
            src = _read(rel)
            assert not re.findall(r"\bOTP\b|\botp\b", src), rel

    def test_password_hash_is_still_required(self):
        """Passwordless auth belongs to a later phase."""
        assert "password_hash = db.Column(db.String(256), nullable=False)" in \
               _read("app/models.py")

    def test_auth_mode_and_admin_key_are_untouched(self):
        cfg = _read("app/config.py")
        assert 'ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")' in cfg
        assert 'if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):' in cfg
        assert _read("app/routes/admin.py").count(
            'config.get("AUTH_MODE", "SESSION_ONLY")') == 2

    def test_the_migration_is_additive_only(self):
        src = _read("migrations/versions/b7d2e4f91a35_rc2_5_15_user_phone_identity.py")
        assert "add_column" in src
        assert "down_revision = 'e6d1b9a37f24'" in src
        for banned in ("drop_table", "alter_column", "op.execute"):
            assert banned not in src.split("def downgrade")[0], banned

    def test_the_migration_adds_no_unique_index(self):
        src = _read("migrations/versions/b7d2e4f91a35_rc2_5_15_user_phone_identity.py")
        up = src.split("def downgrade")[0]
        assert "unique=True" not in up
