"""Phase 2A — tenant identity safety: no cross-tenant brand leak.

THE DEFECT
----------
Customer-facing WhatsApp text treated The Oxford Computers' identity as the
platform's. Observed live: a non-Oxford tenant's customer was greeted "ഞാൻ
Oxford Nova — *The Oxford Computers*-ലെ AI Admission Counsellor". The main
menus, goodbye, AI fallback, follow-ups, payment and enrolment replies and the
staff quick replies all hardcoded Oxford's name, persona, phone, website or
address, and the identity resolver fell back to Oxford's facts for any field a
tenant had not configured.

THE CONTRACT PINNED HERE
------------------------
  * Every customer-facing identity comes from the CURRENT tenant.
  * A field a tenant has not configured resolves to NEUTRAL (omitted), never
    to another tenant's value.
  * The default persona is "AI Assistant"; a tenant's own persona wins.
  * The Oxford Computers keeps its identity -- from the profile it authored.
  * Day 1 / 3 / 7 follow-up scheduling is unchanged.
  * No Graph / network call is made by any of these paths.
"""
import ast
import json
import os
import sys
import tempfile
from datetime import timedelta

import pytest
from werkzeug.security import generate_password_hash

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "phase2a_identity_leak.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "p2a-admin-key")
os.environ.setdefault("SECRET_KEY", "p2a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "p2a-broadcast-key")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "p2a-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (ConversationState, FollowUpJob, Tenant,         # noqa: E402
                        TenantSettings, User)
from app.bot import business_profile as bp                              # noqa: E402
from app.bot import cta_handlers as cta                                 # noqa: E402
from app.bot import offer_handlers as oh                                # noqa: E402
from app.bot import router                                              # noqa: E402
from app.bot import screens                                             # noqa: E402
from app.services import ai_service                                     # noqa: E402
from app.services import followup_service                               # noqa: E402
from app.services import prompt_composer                                # noqa: E402
from app.services import tenant_identity_service as tis                 # noqa: E402

OX = "t-ox"       # The Oxford Computers: its own profile + persona "Krishna"
G = "t-gamma"     # a different business with its own configured profile
D = "t-delta"     # a tenant that has configured nothing

OX_PROFILE = {
    "legal_name": bp.INSTITUTE_NAME,
    "address": {"line": bp.ADDRESS, "locality": bp.LOCALITY,
                "city": bp.CITY, "region": "Kerala"},
    "location_url": bp.MAPS_URL,
    "contact": {"phone": bp.PHONE, "whatsapp": bp.WHATSAPP,
                "email": bp.EMAIL, "website": bp.WEBSITE},
    "hours": {"general": bp.OFFICE_HOURS, "extended": bp.COUNSELLOR_HOURS},
}
G_PROFILE = {
    "tagline": "Design Council Accredited",
    "address": {"line": "7 Gamma Lane", "locality": "Kowdiar",
                "city": "Kochi", "region": "Kerala"},
    "location_url": "https://maps.example/gamma",
    "contact": {"phone": "9111122222", "website": "gamma.example"},
    "hours": {"general": "9-5", "extended": "8-8"},
}

# Every string that belongs to The Oxford Computers alone -- including the
# two hard-coded leaks Phase 2A removes ("Oxford Nova", "Varam") and Oxford's
# Rutronix recognition claim.
OXFORD_ONLY = ("The Oxford Computers", "Oxford Computers", "Oxford Nova",
               "9447329972", "theoxfordedu.com", "info@theoxfordedu.com",
               "Malayinkeezhu", "Krishna Building", "maps.app.goo.gl",
               "Rutronix", "Varam", "Krishna")

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _no_oxford(text, who):
    for literal in OXFORD_ONLY:
        assert literal not in text, f"{literal!r} leaked to {who}"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in these paths may reach Meta's Graph API or any other host."""
    import requests

    def _blocked(*a, **k):
        raise AssertionError(f"unexpected network call: {a[:2]}")
    monkeypatch.setattr(requests.Session, "request", _blocked)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="The Oxford Computers", slug="ox",
                              status="ACTIVE", billing_exempt=True,
                              ai_persona_name="Krishna"))
        db.session.add(Tenant(id=G, name="Gamma Design School", slug="gamma",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=D, name="Delta Academy", slug="delta",
                              status="ACTIVE", billing_exempt=True))
        for tid, prof in ((OX, OX_PROFILE), (G, G_PROFILE)):
            db.session.add(TenantSettings(tenant_id=tid, settings=json.dumps(
                {"_v": 1, "business_profile": prof})))
        ids = {}
        for tid in (OX, D):
            u = User(username=f"admin_{tid}", email=f"admin@{tid}.test",
                     password_hash=generate_password_hash("pw"), role="ADMIN",
                     tenant_id=tid, is_active=True,
                     require_password_change=False)
            db.session.add(u)
            db.session.add(ConversationState(phone="919000000001",
                                             name="Asha", tenant_id=tid))
            db.session.commit()
            ids[tid] = u.id
    yield ids
    with _APP.app_context():
        db.session.remove()


def _client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ Resolver and persona ═══════════════════════════════════════════════════

class TestResolverIsNeutral:

    def test_unconfigured_tenant_gets_its_name_and_nothing_else(self, seeded):
        with _APP.app_context():
            i = tis.resolve_business_identity(D)
        assert i.name == "Delta Academy"
        assert (i.contact.phone, i.contact.email, i.contact.website,
                i.address.line, i.location_url, i.hours.general) == ("",) * 6

    def test_missing_tenant_and_no_tenant_are_nameless_not_oxford(self, seeded):
        with _APP.app_context():
            for tid in (None, "", "t-missing"):
                i = tis.resolve_business_identity(tid)
                assert i.name == "" and i.contact.phone == ""

    def test_resolver_no_longer_imports_oxfords_constants(self):
        tree = ast.parse(open(os.path.join(
            _ROOT, "app", "services", "tenant_identity_service.py"),
            encoding="utf-8").read())
        mods = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
        assert "app.bot.business_profile" not in mods

    def test_oxford_resolves_its_authored_profile(self, seeded):
        with _APP.app_context():
            i = tis.resolve_business_identity(OX)
        assert i.name == "The Oxford Computers" and i.is_configured
        assert i.contact.phone == bp.PHONE and i.contact.website == bp.WEBSITE

    def test_default_persona_is_ai_assistant_and_single_sourced(self):
        assert tis.DEFAULT_PERSONA_NAME == "AI Assistant"
        assert ai_service._DEFAULT_PERSONA_NAME == tis.DEFAULT_PERSONA_NAME
        assert prompt_composer.DEFAULT_PERSONA_NAME == tis.DEFAULT_PERSONA_NAME
        # the menus' import-failure fallback (last-resort path) agrees too
        assert screens._NO_PERSONA == tis.DEFAULT_PERSONA_NAME
        assert screens._NO_IDENTITY.name == ""

    def test_oxford_persona_is_krishna_and_others_default(self, seeded):
        with _APP.app_context():
            assert tis.resolve_persona_name(OX) == "Krishna"
            assert tis.resolve_persona_name(D) == "AI Assistant"
            assert tis.resolve_persona_name(None) == "AI Assistant"
            assert tis.resolve_persona_name("t-missing") == "AI Assistant"


# ═══ WhatsApp menus ═════════════════════════════════════════════════════════

class TestMenus:

    # Gamma has configured a tagline, so it follows the name in brackets
    # (reconciled with the pre-existing local main_menu edit); Delta has not.
    @pytest.mark.parametrize("tid,who", [
        (D, "*Delta Academy*"),
        (G, "*Gamma Design School*(Design Council Accredited)"),
    ])
    def test_non_oxford_main_menu(self, seeded, tid, who):
        with _APP.app_context():
            s = screens.main_menu("Asha", tid)
        assert f"ഞാൻ AI Assistant — {who}-ലെ" in s.body
        _no_oxford(s.body, tid)
        _no_oxford(s.fallback_body, tid)

    def test_main_menu_tagline_follows_name_when_configured(self, seeded):
        with _APP.app_context():
            s = screens.main_menu("Asha", G)
        assert "*Gamma Design School*(Design Council Accredited)-ലെ" in s.body
        _no_oxford(s.body, G)

    def test_main_menu_without_tagline_is_name_only(self, seeded):
        with _APP.app_context():
            s = screens.main_menu("Asha", D)
        assert "ഞാൻ AI Assistant — *Delta Academy*-ലെ AI Admission Counsellor" in s.body
        assert "*Delta Academy*(" not in s.body
        _no_oxford(s.body, D)

    @pytest.mark.parametrize("tid", [None, "", "t-missing"])
    def test_main_menu_unconfigured_is_neutral(self, seeded, tid):
        with _APP.app_context():
            s = screens.main_menu("Asha", tid)
        assert "ഞാൻ AI Assistant — AI Admission Counsellor" in s.body
        assert "()" not in s.body and "**" not in s.body
        _no_oxford(s.body, tid)
        _no_oxford(s.fallback_body, tid)

    def test_non_oxford_legacy_menu_uses_own_tagline(self, seeded):
        with _APP.app_context():
            g, _ = screens.legacy_main_menu_reply("Asha", G)
            d, _ = screens.legacy_main_menu_reply("Asha", D)
        assert "*Gamma Design School*-ലേക്ക് സ്വാഗതം!" in g
        assert "Design Council Accredited • AI-Enabled Courses" in g
        assert "*Delta Academy*-ലേക്ക് സ്വാഗതം!" in d
        _no_oxford(g, G)
        _no_oxford(d, D)

    def test_oxford_main_menu_is_its_own_identity(self, seeded):
        with _APP.app_context():
            s = screens.main_menu("Asha", OX)
            legacy, _ = screens.legacy_main_menu_reply("Asha", OX)
        assert "ഞാൻ Krishna — *The Oxford Computers*-ലെ" in s.body
        assert "Oxford Nova" not in s.body
        assert "*The Oxford Computers*-ലേക്ക് സ്വാഗതം!" in legacy


# ═══ Goodbye, AI fallback ═══════════════════════════════════════════════════

class TestExitAndFallback:

    def test_non_oxford_exit(self, seeded):
        with _APP.app_context():
            d, _ = router.msg_exit("Asha", D)
            g, _ = router.msg_exit("Asha", G)
        assert "Delta Academy — always here for you." in d
        assert "📞" not in d and "🌐" not in d          # nothing configured
        assert "📞 9111122222 | 🌐 gamma.example" in g
        _no_oxford(d, D)
        _no_oxford(g, G)

    def test_oxford_exit_is_byte_identical(self, seeded):
        with _APP.app_context():
            text, preset = router.msg_exit("Asha", OX)
        assert preset is None
        assert text == ("👋 Nandi Asha! Oru nalla divasam nerunnu! 😊\n\n"
                        "The Oxford Computers — always here for you.\n"
                        "📞 9447329972 | 🌐 theoxfordedu.com\n\n"
                        "Thiriche message cheyyoo — happy to help!")

    @pytest.mark.parametrize("msg", ["", "what is the fee", "any placement job"])
    def test_non_oxford_ai_fallback(self, seeded, msg):
        with _APP.app_context():
            d = ai_service.smart_fallback("Asha", msg, D)
            g = ai_service.smart_fallback("Asha", msg, G)
        _no_oxford(d, D)
        _no_oxford(g, G)
        assert "📞" not in d
        if msg == "":
            assert "Njan AI Assistant — Delta Academy-nte counselor." in d
            assert "📞 9111122222" in g

    def test_oxford_ai_fallback_uses_krishna(self, seeded):
        with _APP.app_context():
            text = ai_service.smart_fallback("Asha", "", OX)
        assert "Njan Krishna — The Oxford Computers-nte counselor." in text
        assert "📞 9447329972" in text


# ═══ Payment and enrolment ══════════════════════════════════════════════════

class TestPaymentAndEnrol:

    def _pay(self, tid):
        return cta.payment_link_reply("PGDCA", "PG Diploma", "₹19,540",
                                      "12 months", "https://pay.example/x", tid)[0]

    def test_non_oxford_payment_message(self, seeded):
        with _APP.app_context():
            g, d = self._pay(G), self._pay(D)
        assert "🎓 Design Council Accredited" in g
        assert "📍 Gamma Design School, Kowdiar" in g
        assert "📞 9111122222" in g
        assert "📍 Delta Academy" in d and "📞" not in d
        _no_oxford(g, G)
        _no_oxford(d, D)

    def test_oxford_payment_message_keeps_its_venue_and_phone(self, seeded):
        with _APP.app_context():
            text = self._pay(OX)
        assert "📍 The Oxford Computers, Malayinkeezhu" in text
        assert "Any doubt undenkil call cheyyoo: 📞 9447329972" in text

    def test_non_oxford_enrol_counselor_branch(self, seeded):
        with _APP.app_context():
            d, _ = cta.enroll_reply("Asha", "Unlisted Course", {}, D)
            g, _ = cta.enroll_reply("Asha", "Unlisted Course", {}, G)
        assert "Ivide thanne reply cheyyum" in d
        assert "📞 *9111122222* — ippol call cheyyoo" in g
        _no_oxford(d, D)
        _no_oxford(g, G)

    def test_oxford_enrol_counselor_branch(self, seeded):
        with _APP.app_context():
            text, _ = cta.enroll_reply("Asha", "Unlisted Course", {}, OX)
        assert "📞 *9447329972* — ippol call cheyyoo" in text

    def test_offer_menu_carries_no_other_tenants_recognition(self, seeded):
        with _APP.app_context():
            d, _ = oh.offer_menu_reply(D)
            g, _ = oh.offer_menu_reply(G)
        assert "Rutronix" not in d and "Rutronix" not in g
        assert "Design Council Accredited" in g


# ═══ Follow-ups ═════════════════════════════════════════════════════════════

class TestFollowUps:

    def _jobs(self, tid):
        with _APP.app_context():
            followup_service.schedule_followups("919000000009", "Asha", tid)
            return [(j.day, j.send_at, j.message, j.tenant_id, j.done)
                    for j in FollowUpJob.query.filter_by(tenant_id=tid)
                    .order_by(FollowUpJob.day).all()]

    def test_day_1_3_7_scheduling_is_unchanged(self, seeded):
        jobs = self._jobs(D)
        assert [j[0] for j in jobs] == [1, 3, 7]
        assert all(j[3] == D and j[4] is False for j in jobs)
        base = jobs[0][1] - timedelta(hours=24)
        assert [j[1] - base for j in jobs] == [timedelta(hours=h)
                                               for h in (24, 72, 168)]

    def test_non_oxford_follow_ups(self, seeded):
        jobs = self._jobs(D)
        assert jobs[0][2].startswith(
            "Hi Asha 😊 AI Assistant here from Delta Academy.\n\n")
        assert jobs[2][2].endswith("All the best from Delta Academy 🎓")
        for job in jobs:
            _no_oxford(job[2], D)

    def test_oxford_follow_ups_use_krishna_and_its_name(self, seeded):
        jobs = self._jobs(OX)
        assert jobs[0][2].startswith(
            "Hi Asha 😊 Krishna here from The Oxford Computers.\n\n")
        assert jobs[2][2].endswith("All the best from The Oxford Computers 🎓")

    def test_tenant_without_a_name_gets_no_from_clause(self, seeded):
        with _APP.app_context():
            persona, from_business = followup_service._followup_identity("t-missing")
        assert (persona, from_business) == ("AI Assistant", "")


# ═══ Staff quick replies (rendered page) ═══════════════════════════════════

class TestQuickReplies:

    def test_non_oxford_quick_replies(self, seeded):
        body = _client(seeded[D]).get("/crm/lead/919000000001").get_data(as_text=True)
        assert "thank you for contacting Delta Academy!" in body
        assert "we will share our location!" in body
        assert "Oxford Computers" not in body.split('id="quick-replies"')[1].split("</div>")[0]
        assert "located at Varam" not in body

    def test_oxford_quick_replies_use_its_own_identity(self, seeded):
        body = _client(seeded[OX]).get("/crm/lead/919000000001").get_data(as_text=True)
        assert "thank you for contacting The Oxford Computers!" in body
        assert "Our office is located at Malayinkeezhu, Thiruvananthapuram." in body
        assert "located at Varam" not in body


# ═══ No code path still hardcodes Oxford ════════════════════════════════════

class TestNoHardcodedOxfordInCustomerPaths:

    @pytest.mark.parametrize("rel,fn", [
        ("app/bot/screens.py", "main_menu"),
        ("app/bot/screens.py", "legacy_main_menu_reply"),
        ("app/bot/router.py", "msg_exit"),
        ("app/services/ai_service.py", "smart_fallback"),
        ("app/bot/cta_handlers.py", "payment_link_reply"),
        ("app/bot/cta_handlers.py", "enroll_reply"),
        ("app/bot/offer_handlers.py", "offer_menu_reply"),
    ])
    def test_function_code_has_no_oxford_literal(self, rel, fn):
        tree = ast.parse(open(os.path.join(_ROOT, rel), encoding="utf-8").read())
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        body = [s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        code = "\n".join(ast.unparse(s) for s in body)
        for name in ("INSTITUTE_NAME", "RUTRONIX_FULL", "RUTRONIX_LABEL",
                     "Oxford Nova", "The Oxford Computers", "9447329972",
                     "theoxfordedu.com"):
            assert name not in code, f"{fn} still hardcodes {name!r}"

    def test_follow_up_templates_are_neutral(self):
        text = "".join(t["message"] for t in followup_service.FOLLOWUP_TEMPLATES)
        _no_oxford(text, "FOLLOWUP_TEMPLATES")
