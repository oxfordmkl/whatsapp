"""Phase RC2.5.17 Gate A.1: trusted client-IP resolution and /register throttle.

WHY
---
Two independent pre-existing weaknesses, both surfaced by the RC2.5.17 Gate A
audit:

  1. Client-IP resolution existed TWICE -- public.get_client_ip() and
     audit_service.request_ip() -- each taking the leftmost X-Forwarded-For
     entry and returning it UNVALIDATED. Whatever string arrived became a
     rate-limit bucket key and was persisted to audit_log.ip_address.
  2. /register had no rate limit at all, while three lesser routes have had
     one since 15C.5-B. Each accepted POST creates a Tenant, a User, a sales
     pipeline, a settings blob and an outbound email in one transaction.

WHAT THE TRUST BOUNDARY RESTS ON, AND WHAT THESE TESTS CAN PROVE
----------------------------------------------------------------
Railway staff state that their edge STRIPS client-supplied X-Forwarded-For and
that the FIRST entry is the real connecting IP, with X-Real-IP as a single
source of truth. Production data corroborates it: of 70 distinct addresses
across 5,593 audit rows, all 70 are public IPv4 and none is private, loopback
or CGNAT -- an internal hop would have shown 100.x.

These tests CANNOT verify the platform. A Flask test client is not Railway's
edge, so nothing here proves what the real proxy does with a hostile header.
What they DO prove is the application's own behaviour given a set of headers:
which source wins, that malformed values are rejected rather than propagated,
and that the two former implementations now agree. That distinction is the
point -- the platform assumption is documented in audit_service, not asserted
here as though a unit test had established it.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2517a1_ip.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2517a1-admin")
os.environ.setdefault("SECRET_KEY", "rc2517a1-secret")
os.environ.setdefault("BROADCAST_API_KEY", "rc2517a1-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, User                                           # noqa: E402
from app.services.audit_service import request_ip, _valid_ip                  # noqa: E402
import app.routes.public as public                                            # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


@pytest.fixture(autouse=True)
def _clear_limiter():
    """The limiter is process-global; a leaked bucket would silently make a
    later test's first request its fourth."""
    public._RATE_LIMITS.clear()
    yield
    public._RATE_LIMITS.clear()


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _ip_for(headers=None, remote="203.0.113.7"):
    """Resolve the client IP the application would use for these headers."""
    with _APP.test_request_context("/register", headers=headers or {},
                                   environ_base={"REMOTE_ADDR": remote}):
        return request_ip()


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


def _register(client, ip=None, **over):
    form = {
        "business_name": "Acme Institute",
        "admin_name": "Owner One",
        "email": "owner@rc2517a1.test",
        "phone": "",
        "industry": "Education",
        "password": "correct horse battery",
    }
    form.update(over)
    headers = {"X-Real-IP": ip} if ip else {}
    return client.post("/register", data=form, headers=headers,
                       follow_redirects=False)


# ── 1. required header scenarios (section 3 of the authorisation) ───────────

class TestIpResolutionScenarios:

    def test_a_no_forwarded_headers_uses_remote_addr(self):
        assert _ip_for({}, remote="203.0.113.7") == "203.0.113.7"

    def test_b_single_forwarded_value(self):
        assert _ip_for({"X-Forwarded-For": "198.51.100.4"}) == "198.51.100.4"

    def test_c_multiple_forwarded_values_take_the_first(self):
        """On THIS edge the first entry is the real connecting IP and the
        later entries are internal hops. Taking the last would collapse every
        client onto one internal address and turn a per-IP limit into a global
        lock -- which is why the audit explicitly rejected `split(',')[-1]`."""
        assert _ip_for({"X-Forwarded-For": "198.51.100.4, 100.64.0.1, 100.64.0.2"}) \
            == "198.51.100.4"

    def test_d_x_real_ip_outranks_forwarded_for(self):
        """Railway designates X-Real-IP the single source of truth."""
        assert _ip_for({"X-Real-IP": "198.51.100.9",
                        "X-Forwarded-For": "203.0.113.200"}) == "198.51.100.9"

    def test_e_x_real_ip_alone(self):
        assert _ip_for({"X-Real-IP": "198.51.100.9"}) == "198.51.100.9"

    def test_f_unknown_forwarded_header_is_ignored(self):
        """RFC 7239 `Forwarded` is not part of this edge's contract, so it must
        not be honoured -- reading a header the proxy does not control would
        reintroduce exactly the weakness this gate closes."""
        assert _ip_for({"Forwarded": "for=198.51.100.77"}) == "203.0.113.7"

    def test_ipv6_is_accepted(self):
        assert _ip_for({"X-Real-IP": "2001:db8::1"}) == "2001:db8::1"


# ── 2. validation: a header value is not automatically an identity ──────────

class TestMalformedValuesAreRejected:

    @pytest.mark.parametrize("bad", [
        "not-an-ip", "", "   ", "999.999.999.999", "1.2.3.4; DROP TABLE users",
        "<script>", "a" * 60, "1.2.3.4 5.6.7.8",
    ])
    def test_a_malformed_value_never_becomes_an_identity(self, bad):
        """Unvalidated, this string became a limiter bucket key AND was
        persisted to audit_log.ip_address. Junk must degrade to the next
        source, never propagate."""
        assert _ip_for({"X-Real-IP": bad}) == "203.0.113.7"

    def test_a_malformed_forwarded_falls_through_to_remote_addr(self):
        assert _ip_for({"X-Forwarded-For": "garbage, also-garbage"}) == "203.0.113.7"

    def test_a_malformed_real_ip_falls_back_to_a_valid_forwarded(self):
        assert _ip_for({"X-Real-IP": "nonsense",
                        "X-Forwarded-For": "198.51.100.4"}) == "198.51.100.4"

    def test_everything_malformed_yields_empty_not_junk(self):
        assert _ip_for({"X-Real-IP": "nope", "X-Forwarded-For": "also-nope"},
                       remote="still-not-an-ip") == ""

    def test_valid_ip_helper_is_strict(self):
        assert _valid_ip("203.0.113.7") == "203.0.113.7"
        assert _valid_ip("2001:db8::1") == "2001:db8::1"
        for bad in ("", None, "   ", "1.2.3", "1.2.3.4.5", "abc", "x" * 50):
            assert _valid_ip(bad) == "", bad

    def test_an_over_length_value_is_refused(self):
        """audit_log.ip_address is String(45); a longer value would be
        truncated on write, and a truncated address is a wrong address."""
        assert _valid_ip("1" * 46) == ""


# ── 3. one rule, not two ────────────────────────────────────────────────────

class TestSingleImplementation:

    def test_get_client_ip_delegates_to_request_ip(self):
        src = _read("app/routes/public.py")
        fn = src.split("def get_client_ip")[1].split("\ndef ")[0]
        assert "request_ip()" in fn
        assert "X-Forwarded-For" not in fn.split('"""')[-1]

    def test_the_two_helpers_agree_on_every_scenario(self):
        cases = [
            {}, {"X-Real-IP": "198.51.100.9"},
            {"X-Forwarded-For": "198.51.100.4, 100.64.0.1"},
            {"X-Real-IP": "bad", "X-Forwarded-For": "198.51.100.4"},
            {"X-Real-IP": "nope", "X-Forwarded-For": "nope"},
        ]
        for headers in cases:
            with _APP.test_request_context("/", headers=headers,
                                           environ_base={"REMOTE_ADDR": "203.0.113.7"}):
                assert public.get_client_ip() == request_ip(), headers

    def test_no_second_leftmost_split_survives_in_the_app(self):
        """The old one-liner existed twice. Neither copy may come back.

        Docstrings are stripped before scanning, not just `#` comments: the
        replacement docstring QUOTES the removed line to explain what changed,
        so a naive scan matches its own explanation. That false positive has
        now bitten this programme in three separate suites.
        """
        import ast
        import glob
        for path in glob.glob(os.path.join(_ROOT, "app", "**", "*.py"),
                              recursive=True):
            src = open(path, encoding="utf-8", errors="replace").read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc:
                        src = src.replace(doc, "")
            code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
            # The ONLY place allowed to read a forwarding header is the one
            # canonical resolver in audit_service.
            if "audit_service" in path:
                continue
            assert "X-Forwarded-For" not in code, path
            assert "X-Real-IP" not in code, path

    def test_the_trust_boundary_is_documented_where_the_decision_lives(self):
        src = _read("app/services/audit_service.py")
        assert "TRUST BOUNDARY" in src
        assert "X-Real-IP" in src


# ── 4. /register throttle ───────────────────────────────────────────────────

class TestRegisterThrottle:

    def test_the_sixth_attempt_from_one_ip_is_refused(self, clean_db):
        c = _APP.test_client()
        codes = [_register(c, ip="198.51.100.10",
                           email=f"u{i}@rc2517a1.test",
                           business_name=f"Biz {i}").status_code
                 for i in range(6)]
        assert codes[:5] == [302] * 5, codes
        assert codes[5] == 429, codes

    def test_the_fifth_attempt_still_succeeds(self, clean_db):
        """The boundary, pinned exactly: an off-by-one here silently refuses a
        legitimate registration."""
        c = _APP.test_client()
        for i in range(4):
            _register(c, ip="198.51.100.11", email=f"a{i}@rc2517a1.test",
                      business_name=f"A{i}")
        assert _register(c, ip="198.51.100.11", email="a4@rc2517a1.test",
                         business_name="A4").status_code == 302

    def test_the_429_body_matches_the_existing_limiters(self, clean_db):
        c = _APP.test_client()
        for i in range(5):
            _register(c, ip="198.51.100.12", email=f"b{i}@rc2517a1.test",
                      business_name=f"B{i}")
        r = _register(c, ip="198.51.100.12", email="b5@rc2517a1.test")
        assert r.status_code == 429
        assert b"Too many requests" in r.data

    def test_a_different_ip_has_its_own_budget(self, clean_db):
        c = _APP.test_client()
        for i in range(5):
            _register(c, ip="198.51.100.13", email=f"c{i}@rc2517a1.test",
                      business_name=f"C{i}")
        assert _register(c, ip="198.51.100.14", email="other@rc2517a1.test",
                         business_name="Other").status_code == 302

    def test_the_throttle_runs_before_any_database_write(self, clean_db):
        """A flood must cost a dict lookup, not a five-table transaction."""
        c = _APP.test_client()
        for i in range(5):
            _register(c, ip="198.51.100.15", email=f"d{i}@rc2517a1.test",
                      business_name=f"D{i}")
        with _APP.app_context():
            before = (Tenant.query.count(), User.query.count())
        _register(c, ip="198.51.100.15", email="blocked@rc2517a1.test",
                  business_name="Blocked")
        with _APP.app_context():
            assert (Tenant.query.count(), User.query.count()) == before

    def test_a_throttled_attempt_creates_nothing(self, clean_db):
        c = _APP.test_client()
        for i in range(5):
            _register(c, ip="198.51.100.16", email=f"e{i}@rc2517a1.test",
                      business_name=f"E{i}")
        _register(c, ip="198.51.100.16", email="never@rc2517a1.test",
                  business_name="Never")
        with _APP.app_context():
            assert User.query.filter_by(email="never@rc2517a1.test").count() == 0
            assert Tenant.query.filter_by(name="Never").count() == 0

    def test_the_throttle_is_keyed_on_ip_not_email(self, clean_db):
        """An attacker picks the email freely, so an email-keyed bucket would
        reset on every request and enforce nothing."""
        src = _read("app/routes/public.py")
        reg = src.split("def register()")[1].split("def ")[0]
        assert "register_ip_" in reg
        assert "register_email_" not in reg

    def test_an_unresolvable_ip_is_not_throttled_into_one_bucket(self, clean_db):
        """"" must never become a shared identity: one client with a malformed
        header would otherwise lock out every other unknown-IP visitor."""
        src = _read("app/routes/public.py")
        reg = src.split("def register()")[1].split("def ")[0]
        assert "if _ip and not check_rate_limit" in reg


# ── 5. nothing else about registration changed ──────────────────────────────

class TestRegistrationUnchanged:

    def test_a_normal_registration_still_works(self, clean_db):
        _register(_APP.test_client(), ip="198.51.100.20")
        with _APP.app_context():
            u = User.query.filter_by(email="owner@rc2517a1.test").first()
            t = Tenant.query.filter_by(name="Acme Institute").first()
            assert u is not None and u.role == "ADMIN"
            assert t is not None and t.status == "PENDING"

    def test_the_rc2515_enumeration_fix_is_intact(self, clean_db):
        """A duplicate email must still be indistinguishable from success."""
        c = _APP.test_client()
        first = _register(c, ip="198.51.100.21")
        second = _register(c, ip="198.51.100.21")
        assert first.status_code == second.status_code
        assert first.headers.get("Location") == second.headers.get("Location")
        with _APP.app_context():
            assert User.query.filter_by(email="owner@rc2517a1.test").count() == 1

    def test_the_rc2515_phone_persistence_is_intact(self, clean_db):
        _register(_APP.test_client(), ip="198.51.100.22", phone="098473 12534")
        with _APP.app_context():
            assert User.query.filter_by(
                email="owner@rc2517a1.test").first().phone == "919847312534"

    def test_missing_fields_are_still_refused(self, clean_db):
        _register(_APP.test_client(), ip="198.51.100.23", email="", password="")
        with _APP.app_context():
            assert User.query.count() == 0

    def test_get_register_is_not_throttled(self, clean_db):
        """Only the POST creates anything; throttling the page itself would
        break a legitimate visitor who simply reloads."""
        c = _APP.test_client()
        codes = [c.get("/register").status_code for _ in range(10)]
        assert codes == [200] * 10


# ── 6. scope ────────────────────────────────────────────────────────────────

class TestScope:

    def test_the_otp_service_was_not_modified(self):
        src = _read("app/services/otp_service.py")
        assert "rate_limit" not in src.replace("rate limiter", "")
        assert "get_client_ip" not in src
        assert "request_ip" not in src

    def test_no_otp_rate_limit_table_was_added(self):
        src = _read("app/models.py")
        for banned in ("class RateLimit", "rate_limits", "otp_rate"):
            assert banned not in src, banned

    def test_crm_login_route_was_not_modified(self):
        """Out of scope for this gate. request_ip() hardening reaches it only
        through the shared helper, which records a BETTER ip, never a
        different authentication outcome."""
        src = _read("app/routes/admin.py")
        assert "User.query.filter_by(email=email).first()" in src
        assert "check_rate_limit" not in src

    def test_the_existing_limiter_was_not_rewritten(self):
        """Its process-local storage, deploy reset and unbounded key growth
        belong to the durable-limiter phase, not here."""
        src = _read("app/routes/public.py")
        fn = src.split("def check_rate_limit")[1].split("\ndef ")[0]
        assert "_RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key]" in fn
        assert "time.time()" in fn

    def test_no_migration_was_added_by_this_gate(self):
        import glob
        names = [os.path.basename(p) for p in
                 glob.glob(os.path.join(_ROOT, "migrations", "versions", "*.py"))]
        assert not [n for n in names if "rc2_5_17" in n or "rc2517" in n], names
