"""Phase RC2.5.5c-1: tenant business identity in the deterministic flow.

THE DEFECT
-----------
RC2.5.2 built resolve_business_identity() and production-validated it, but its
only consumer was prompt_composer -- the AI path. Every deterministic reply
builder read Oxford's constants directly, so a second tenant's customer was
told to visit Oxford's address, call Oxford's phone number, and turn up at
Oxford's building for their demo class.

Same shape as the payment defect RC2.5.5b fixed: the tenant-owned source
already existed; the deterministic path simply did not read it.

WHAT THIS PHASE COVERS, AND WHAT IT DOES NOT
---------------------------------------------
Converted here: visit_reply, call_reply (cta_handlers) and booked_reply
(booking_handlers).

Deliberately NOT converted, and each for a stated reason:

  * payment_link_reply and enroll_reply's counselor branch, and
    offer_handlers.payment_confirmed_reply -- these are PAYMENT PATHS, which
    this phase is explicitly not authorised to touch. They still render
    Oxford's name, locality and phone. Tracked by
    TestKnownRemainingIdentityLeaks below so the gap cannot be forgotten.
  * screens.py -- one of the twelve protected pre-existing working-tree
    modifications, which must stay byte-identical.
  * constants.py -- forbidden this phase (it is catalogue data, and derives
    INST_* from BUSINESS_PROFILE).

FAIL-SAFE, NOT FAIL-CLOSED
---------------------------
The opposite polarity from the payment resolver, on purpose. A missing
payment URL must produce NO link, because a wrong link takes money to the
wrong account. A missing address must still produce an address, because the
alternative is an empty message. resolve_business_identity() never raises and
falls back per field.

OXFORD PARITY IS BY CONSTRUCTION
---------------------------------
tenant_identity_service's defaults are derived from business_profile.py, and
no tenant in production has authored a business_profile section, so an
unconfigured tenant resolves to exactly the values these builders previously
read as constants. TestOxfordParity pins that rather than assuming it.

Import isolation follows test_platform_security_14c.py.

PHASE 2A UPDATE (tenant identity safety)
----------------------------------------
"Parity by construction" rested on the defaults BEING Oxford's facts -- which
is exactly why every unconfigured tenant was told Oxford's phone and address.
Phase 2A makes the defaults neutral and Oxford authors its own profile (as it
now does in production), so Oxford is seeded with that profile here and its
parity tests still pin byte-identical output. The gaps this file tracked
(payment paths, screens.py) are converted and their tripwires inverted.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc255c1_identity.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantSettings                           # noqa: E402
from app.bot import cta_handlers as cta                                 # noqa: E402
from app.bot import booking_handlers as bh                              # noqa: E402
from app.bot import business_profile as bp                              # noqa: E402
from app.services import tenant_identity_service as tis                 # noqa: E402

OX = "t-ox"        # name matches INSTITUTE_NAME, as in production
B = "t-b"          # a second tenant with its own configured profile

_APP = create_app()
_APP.config["TESTING"] = True

# Phase 2A: The Oxford Computers' own business_profile, as saved in
# production -- the business_profile.py values plus region "Kerala".
OX_PROFILE = {
    "legal_name": bp.INSTITUTE_NAME,
    "address": {"line": bp.ADDRESS, "locality": bp.LOCALITY,
                "city": bp.CITY, "region": "Kerala"},
    "location_url": bp.MAPS_URL,
    "contact": {"phone": bp.PHONE, "whatsapp": bp.WHATSAPP,
                "email": bp.EMAIL, "website": bp.WEBSITE},
    "hours": {"general": bp.OFFICE_HOURS, "extended": bp.COUNSELLOR_HOURS},
}

OXFORD_LITERALS = ("The Oxford Computers", "9447329972", "maps.app.goo.gl",
                   "Malayinkeezhu", "theoxfordedu.com")

B_PROFILE = {
    "address": {"line": "12 Beta Road", "locality": "Betaville",
                "city": "Betacity"},
    "location_url": "https://maps.example.com/beta",
    "contact": {"phone": "0800111222", "website": "beta.example.com"},
    "hours": {"general": "10-6 Mon-Fri", "extended": "8-9 daily"},
}


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name=bp.INSTITUTE_NAME, slug="ox",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=B, name="Beta Institute", slug="beta",
                              status="ACTIVE", billing_exempt=True))
        import json
        db.session.add(TenantSettings(   # Phase 2A: Oxford's own profile
            tenant_id=OX, settings=json.dumps({"business_profile": OX_PROFILE})))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def b_configured(seeded):
    import json
    with _APP.app_context():
        db.session.add(TenantSettings(
            tenant_id=B, settings=json.dumps({"business_profile": B_PROFILE})))
        db.session.commit()
    yield


# ── Oxford parity: nothing changed for the tenant already live ──────────────

class TestOxfordParity:
    """Byte-identical output for the primary tenant -- now sourced from the
    profile Oxford authored (Phase 2A), not from anybody's fallback."""

    def test_visit_reply_unchanged(self, seeded):
        with _APP.app_context():
            text, preset = cta.visit_reply(OX)
        assert preset == "COURSE"
        for v in (bp.INSTITUTE_NAME, bp.ADDRESS, bp.MAPS_URL,
                  bp.OFFICE_HOURS, bp.PHONE):
            assert v in text, f"{v!r} missing from visit_reply"

    def test_call_reply_unchanged(self, seeded):
        with _APP.app_context():
            text, preset = cta.call_reply("Alice", OX)
        assert preset is None
        for v in (bp.PHONE, bp.COUNSELLOR_HOURS, bp.INSTITUTE_NAME,
                  bp.LOCALITY):
            assert v in text

    def test_booked_reply_unchanged(self, seeded):
        with _APP.app_context():
            text, preset = bh.booked_reply("PGDCA", "10 AM", "Mon", OX)
        assert preset == "AFTER_BOOKING"
        for v in (bp.INSTITUTE_NAME, bp.LOCALITY, bp.PHONE, bp.WEBSITE):
            assert v in text

    def test_visit_reply_is_byte_identical_to_the_constant_form(self, seeded):
        """Reconstructs the pre-c-1 text from the constants and compares the
        whole string, not just field presence."""
        expected = (
            "🏢 *Office Visit — Always Welcome!*\n\n"
            f"📍 *{bp.INSTITUTE_NAME}*\n"
            f"{bp.ADDRESS}\n\n"
            f"🗺️ Google Maps:\n{bp.MAPS_URL}\n\n"
            f"⏰ Office Hours: {bp.OFFICE_HOURS}\n"
            f"📞 {bp.PHONE}\n\n"
            "Eppol varananu convenient?\n"
            "Morning / Afternoon / Evening? 😊"
        )
        with _APP.app_context():
            assert cta.visit_reply(OX) == (expected, "COURSE")

    def test_call_reply_is_byte_identical_to_the_constant_form(self, seeded):
        expected = (
            "😊 Sure Alice!\n\n"
            "Nigalkkayi Oru nalla counselorne connect cheyyam.\n"
            f"📞 *{bp.PHONE}* — direct vilikkaamo!\n\n"
            f"⏰ Available: {bp.COUNSELLOR_HOURS}\n"
            f"📍 {bp.INSTITUTE_NAME}, {bp.LOCALITY}\n\n"
            "Ivideyum message cheyyoo — ready aanu! 🙌"
        )
        with _APP.app_context():
            assert cta.call_reply("Alice", OX) == (expected, None)

    def test_handle_cta_still_routes_visit_and_call(self, seeded):
        st = {"course": "", "stage": "s", "offer_course": ""}
        with _APP.app_context():
            v = cta.handle_cta("VISIT", "Alice", st, "+911", OX)
            c = cta.handle_cta("CALL", "Alice", st, "+911", OX)
        assert bp.ADDRESS in v[0] and bp.COUNSELLOR_HOURS in c[0]


# ── a second tenant no longer inherits Oxford ───────────────────────────────

class TestSecondTenantDoesNotInheritOxford:

    def test_visit_reply_uses_tenant_bs_address(self, b_configured):
        with _APP.app_context():
            text, _ = cta.visit_reply(B)
        assert "12 Beta Road" in text and "https://maps.example.com/beta" in text
        assert bp.ADDRESS not in text
        assert bp.MAPS_URL not in text
        assert bp.INSTITUTE_NAME not in text

    def test_visit_reply_does_not_leak_oxfords_phone(self, b_configured):
        with _APP.app_context():
            text, _ = cta.visit_reply(B)
        assert "0800111222" in text and bp.PHONE not in text

    def test_call_reply_uses_tenant_bs_contact(self, b_configured):
        with _APP.app_context():
            text, _ = cta.call_reply("Bob", B)
        assert "0800111222" in text and "8-9 daily" in text
        assert bp.PHONE not in text and bp.INSTITUTE_NAME not in text
        assert bp.COUNSELLOR_HOURS not in text

    def test_booked_reply_uses_tenant_bs_venue(self, b_configured):
        with _APP.app_context():
            text, _ = bh.booked_reply("Course", "10 AM", "Mon", B)
        assert "Betaville" in text and "beta.example.com" in text
        assert bp.LOCALITY not in text and bp.WEBSITE not in text

    @pytest.mark.parametrize("literal", ["The Oxford Computers", "9447329972",
                                         "maps.app.goo.gl", "Malayinkeezhu",
                                         "theoxfordedu.com"])
    def test_no_oxford_literal_reaches_tenant_b(self, b_configured, literal):
        with _APP.app_context():
            blobs = [cta.visit_reply(B)[0], cta.call_reply("Bob", B)[0],
                     bh.booked_reply("C", "10 AM", "Mon", B)[0]]
        for text in blobs:
            assert literal not in text, f"{literal!r} leaked to tenant B"

    def test_two_tenants_get_different_output(self, b_configured):
        with _APP.app_context():
            assert cta.visit_reply(OX) != cta.visit_reply(B)
            assert cta.call_reply("X", OX) != cta.call_reply("X", B)

    def test_unconfigured_second_tenant_gets_its_own_name_and_no_oxford(self, seeded):
        """PHASE 2A closes the gap this test documented ("contact still
        defaults to Oxford"): with no profile authored, the tenant gets its
        own name and NOTHING of Oxford's -- lines it has no value for are
        omitted, not filled with another tenant's facts."""
        with _APP.app_context():
            blobs = [cta.visit_reply(B)[0], cta.call_reply("Bob", B)[0],
                     bh.booked_reply("C", "10 AM", "Mon", B)[0]]
        assert "Beta Institute" in blobs[0]
        for text in blobs:
            for literal in OXFORD_LITERALS:
                assert literal not in text, literal
            assert "📞 \n" not in text and "None" not in text


# ── fail-safe ───────────────────────────────────────────────────────────────

class TestFailsSafeNotClosed:

    # Phase 2A: still fail-SAFE (a reply always renders), but the fallback is
    # neutral -- these used to assert Oxford's address and phone came back.

    def test_no_tenant_still_renders_a_reply(self, seeded):
        with _APP.app_context():
            text, preset = cta.visit_reply(None)
        assert preset == "COURSE" and "Eppol varananu convenient?" in text
        for literal in OXFORD_LITERALS:
            assert literal not in text, literal

    def test_unknown_tenant_still_renders(self, seeded):
        with _APP.app_context():
            text, _ = cta.call_reply("Alice", "t-does-not-exist")
        assert "Ivideyum message cheyyoo" in text
        for literal in OXFORD_LITERALS:
            assert literal not in text, literal

    def test_db_error_still_renders(self, seeded, monkeypatch):
        """The polarity that distinguishes this from the payment resolver."""
        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("db down")
        with _APP.app_context():
            monkeypatch.setattr(Tenant, "query", Boom())
            text, _ = cta.visit_reply(OX)
        # Phase 2A: renders, neutrally -- never with a guessed identity.
        assert "Eppol varananu convenient?" in text
        assert bp.PHONE not in text

    def test_never_renders_the_string_None(self, b_configured):
        with _APP.app_context():
            for text, _ in (cta.visit_reply(B), cta.call_reply("B", B),
                            bh.booked_reply("C", "1", "2", B)):
                assert "None" not in text

    def test_default_argument_does_not_crash(self, seeded):
        with _APP.app_context():
            assert cta.visit_reply()[0]
            assert cta.call_reply("A")[0]
            assert bh.booked_reply("C", "1", "2")[0]


# ── structural: the constants are no longer read by these builders ──────────

class TestBuildersNoLongerReadOxfordConstants:

    @pytest.mark.parametrize("fn_name,mod", [
        ("visit_reply", "app/bot/cta_handlers.py"),
        ("call_reply", "app/bot/cta_handlers.py"),
        ("booked_reply", "app/bot/booking_handlers.py"),
    ])
    def test_builder_reads_no_identity_constant(self, fn_name, mod):
        SYMS = {"INSTITUTE_NAME", "ADDRESS", "MAPS_URL", "PHONE", "WEBSITE",
                "LOCALITY", "CITY", "OFFICE_HOURS", "COUNSELLOR_HOURS",
                "WHATSAPP", "EMAIL"}
        tree = ast.parse(_src(mod))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == fn_name)
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} & SYMS
        assert not used, f"{fn_name} still reads {sorted(used)}"

    @pytest.mark.parametrize("fn_name,mod", [
        ("visit_reply", "app/bot/cta_handlers.py"),
        ("call_reply", "app/bot/cta_handlers.py"),
        ("booked_reply", "app/bot/booking_handlers.py"),
    ])
    def test_builder_resolves_identity(self, fn_name, mod):
        tree = ast.parse(_src(mod))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == fn_name)
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_identity" in called, f"{fn_name} does not resolve identity"

    def test_booking_handlers_imports_no_identity_constant(self):
        tree = ast.parse(_src("app/bot/booking_handlers.py"))
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)
                    and n.module == "app.bot.business_profile" for a in n.names}
        assert imported == set()

    def test_callers_pass_tenant_id(self):
        for mod, caller, callee in [
                ("app/bot/cta_handlers.py", "handle_cta", "visit_reply"),
                ("app/bot/cta_handlers.py", "handle_cta", "call_reply"),
                ("app/bot/booking_handlers.py", "handle_date", "booked_reply")]:
            tree = ast.parse(_src(mod))
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == caller)
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", None) == callee]
            assert calls, f"{caller} no longer calls {callee}"
            for c in calls:
                assert "tenant_id" in [getattr(a, "id", None) for a in c.args], \
                    f"{caller} calls {callee} without tenant_id"


class TestKnownRemainingIdentityLeaks:
    """The identity that RC2.5.5c-1 could NOT convert, pinned so it is not
    quietly forgotten. Each is blocked by an explicit scope rule, not an
    oversight. INVERT these when the blocking phase lands.

    Phase 2A landed the payment-path and screens.py conversions; those two
    are inverted below. payment_confirmed_reply has no caller (dead code,
    out of Phase 2A scope) and is still tracked."""

    def test_payment_link_reply_resolves_tenant_identity(self):
        """INVERTED BY PHASE 2A (was ..._still_render_oxford_identity)."""
        tree = ast.parse(_src("app/bot/cta_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "payment_link_reply")
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert not {"INSTITUTE_NAME", "LOCALITY", "PHONE",
                    "RUTRONIX_LABEL"} & used
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_identity" in called

    def test_offer_confirmation_still_renders_oxford_identity(self):
        """Blocked: payment path."""
        tree = ast.parse(_src("app/bot/offer_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "payment_confirmed_reply")
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert {"INSTITUTE_NAME", "PHONE", "LOCALITY", "CITY"} & used

    def test_screens_reads_no_oxford_identity_constant(self):
        """INVERTED BY PHASE 2A (was ..._still_renders_oxford_identity):
        the menus resolve the tenant's identity and persona instead."""
        tree = ast.parse(_src("app/bot/screens.py"))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not {"INSTITUTE_NAME", "PHONE", "RUTRONIX_FULL"} & used
