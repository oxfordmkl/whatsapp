"""Phase 2B — Xasnic platform neutrality and tenant-owned education claims.

THE DEFECTS
-----------
Phase 2A stopped the primary tenant's NAME and CONTACT details reaching other
tenants. Its CLAIMS still did, and the platform itself still called itself
"Oxford CRM":

  * the certificate / placement / batch-timing replies hardcoded The Oxford
    Computers' Rutronix accreditation, PSC / NORKA eligibility, a "Kerala &
    Gulf" placement record and its batch times -- for every tenant;
  * TRUST_LINES asserted a Rutronix certificate and placement assistance at
    random inside course-detail and fee replies;
  * the shared education AI prompt told every tenant's assistant it was a
    Rutronix-authorised government training centre with PSC and NORKA
    eligibility, speaking as a Malayali counsellor in Manglish;
  * the Marketing Hub presets, usable by any tenant admin, were written as
    The Oxford Computers with Oxford's address, website and claims;
  * login, public, sidebar, email and /health identified the platform as
    Oxford; /health published platform-wide lead and follow-up counts;
  * the profile save reported success when nothing had changed.

THE CONTRACT PINNED HERE
------------------------
  * Institution claims come from the CURRENT tenant's own knowledge rows,
    found by a generic topic tag on the existing Discovery keywords; with no
    such row the reply is neutral. Never another tenant's content.
  * Shared prompt, TRUST_LINES and deterministic replies carry no
    accreditation, regulator, country, placement, timing or language claim.
  * The platform is "Xasnic" (config.PLATFORM_NAME); tenant identity is
    unchanged.
  * /health keeps HTTP 200 and only operational fields.
  * No Graph / network call is made by any of these paths.
"""
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "phase2b_platform_neutrality.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "p2b-admin-key")
os.environ.setdefault("SECRET_KEY", "p2b-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "p2b-broadcast-key")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "p2b-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (ConversationState, Tenant, TenantKnowledge,     # noqa: E402
                        TenantSettings, User)
from app.bot import constants as K                                      # noqa: E402
from app.bot import router                                              # noqa: E402
from app.bot import prompts                                             # noqa: E402
from app.services import prompt_composer                                # noqa: E402

OX = "t-ox"       # The Oxford Computers: its own profile, persona and claims
G = "t-gamma"     # a different business with its own tagged knowledge
D = "t-delta"     # a tenant that has configured nothing

OX_PROFILE = {
    "legal_name": "The Oxford Computers",
    "tagline": "Kerala State Rutronix ATC",
    "address": {"line": "Krishna Building", "locality": "Malayinkeezhu",
                "city": "Thiruvananthapuram", "region": "Kerala"},
    "contact": {"phone": "9447329972", "website": "theoxfordedu.com"},
}
G_PROFILE = {
    "tagline": "Design Council Accredited",
    "address": {"line": "7 Gamma Lane", "city": "Lisbon", "country": "Portugal"},
    "contact": {"phone": "351210000000", "website": "gamma.example"},
}

# Oxford's own claims, as its production rows hold them (rows 32-34), now
# tagged through the existing Discovery keywords field.
OX_ROWS = (
    ("policy", "Accreditation", "Kerala State Rutronix Authorised Training Centre",
     ["certificate", "recognition"]),
    ("policy", "PSC eligibility",
     "Eligible 6-month & 12-month govt-approved courses are PSC eligible",
     ["certificate"]),
    ("faq", "NORKA Attestation",
     "NORKA Attestation available for eligible certificates", ["certificate"]),
    # Untagged, exactly like production row 35: it must NOT be picked up as a
    # timing answer merely because it exists.
    ("faq", "Learning modes",
     "Offline Classes | Online Live Classes | Fast Track available", None),
)
G_ROWS = (
    ("policy", "Certification", "Diplomas awarded by the Gamma Design Council.",
     ["certificate"]),
    ("faq", "Career services", "Portfolio reviews with partner studios.",
     ["placement"]),
    ("faq", "Studio hours", "Weekday studio sessions from 10:00 to 16:00.",
     ["timing", "schedule"]),
)

# Anything that belongs to the primary tenant, its regulator, its country or
# its language -- none of it may reach another tenant or the shared layers.
OXFORD_CLAIMS = ("Rutronix", "PSC", "NORKA", "Kerala", "Gulf", "Government",
                 "government", "Govt", "track record", "The Oxford Computers",
                 "Oxford Computers", "Malayinkeezhu", "theoxfordedu",
                 "9447329972", "Oxford Nova")
FIXED_TIMES = re.compile(r"\b(9|11|12|2|5|7)\s?(AM|PM)\b|Weekend batches|"
                         r"Offline Classes|Online Live", re.IGNORECASE)
SHARED_PROMPT_BANNED = ("Rutronix", "PSC", "NORKA", "Government", "government",
                        "Govt", "Kerala", "Gulf", "Malayali", "Manglish",
                        "Malayalam", "AI-enabled", "The Oxford Computers",
                        "Malayinkeezhu", "theoxfordedu", "9447329972")

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _no_claims(text, who):
    for literal in OXFORD_CLAIMS:
        assert literal not in text, f"{literal!r} reached {who}"
    assert not FIXED_TIMES.search(text), f"fixed Oxford timing reached {who}"


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
        for tid, rows in ((OX, OX_ROWS), (G, G_ROWS)):
            for i, (kind, title, body, kw) in enumerate(rows):
                attrs = {"keywords": kw} if kw else {}
                db.session.add(TenantKnowledge(
                    tenant_id=tid, kind=kind, title=title, body=body,
                    attributes=json.dumps(attrs), sort_order=20 + i))
        ids = {}
        for tid in (OX, G, D):
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


def _reply(msg, tid, monkeypatch):
    """Run the real router for one message; capture background events."""
    started = []

    class _Thread:
        def __init__(self, target=None, kwargs=None, daemon=None, **_):
            self.kwargs = kwargs or {}

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(router, "threading", SimpleNamespace(Thread=_Thread))
    with _APP.test_request_context("/"):
        text, preset = router.smart_reply(msg, "Asha", "919000000001", False,
                                          tenant_id=tid)
    return text, preset, started


# ═══ Certificate / placement / timing replies ═══════════════════════════════

TOPIC_MESSAGES = {
    "certificate": ("certificate", "certificate undo?"),
    "placement": ("placement", "job assistance"),
    "timing": ("timing", "batch", "class time"),
}


class TestTopicReplies:

    @pytest.mark.parametrize("tid", [G, D])
    @pytest.mark.parametrize("msg", [m for ms in TOPIC_MESSAGES.values() for m in ms])
    def test_non_oxford_tenants_get_no_oxford_claim(self, seeded, monkeypatch, tid, msg):
        text, _, _ = _reply(msg, tid, monkeypatch)
        _no_claims(text, f"{tid} ({msg!r})")

    def test_oxford_certificate_reply_is_its_own_knowledge(self, seeded, monkeypatch):
        text, preset, _ = _reply("certificate", OX, monkeypatch)
        assert "Kerala State Rutronix Authorised Training Centre" in text
        assert "PSC eligible" in text
        assert "NORKA Attestation available" in text
        assert preset == "COURSE"

    def test_other_tenant_gets_its_own_certificate_content(self, seeded, monkeypatch):
        text, _, _ = _reply("certificate", G, monkeypatch)
        assert "Diplomas awarded by the Gamma Design Council." in text
        _no_claims(text, G)

    def test_other_tenant_gets_its_own_placement_and_timing(self, seeded, monkeypatch):
        placement, _, _ = _reply("placement", G, monkeypatch)
        timing, _, _ = _reply("timing", G, monkeypatch)
        assert "Portfolio reviews with partner studios." in placement
        assert "Weekday studio sessions from 10:00 to 16:00." in timing

    @pytest.mark.parametrize("topic", sorted(TOPIC_MESSAGES))
    def test_absent_content_gives_the_neutral_fallback(self, seeded, monkeypatch, topic):
        text, _, _ = _reply(TOPIC_MESSAGES[topic][0], D, monkeypatch)
        from app.bot import tenant_topics
        assert tenant_topics.FALLBACKS[topic] in text
        _no_claims(text, D)

    def test_oxford_without_placement_or_timing_rows_is_neutral(self, seeded, monkeypatch):
        """Operator content for these two topics does not exist yet: Oxford
        gets the same neutral text as anyone, never its old hardcoded one."""
        from app.bot import tenant_topics
        placement, _, _ = _reply("placement", OX, monkeypatch)
        timing, _, _ = _reply("timing", OX, monkeypatch)
        assert tenant_topics.FALLBACKS["placement"] in placement
        assert tenant_topics.FALLBACKS["timing"] in timing
        assert "Kerala & Gulf" not in placement
        assert not FIXED_TIMES.search(timing), "untagged Learning-modes row leaked"

    def test_certificate_reply_stays_deterministic_and_mentions_certificates(
            self, seeded, monkeypatch):
        text, _, _ = _reply("certificate undo?", D, monkeypatch)
        assert "certificate" in text.lower()

    def test_placement_event_is_still_logged(self, seeded, monkeypatch):
        _, _, started = _reply("placement", D, monkeypatch)
        assert [e.get("event_type") for e in started] == ["PLACEMENT_ASKED"]
        assert started[0]["tenant_id"] == D

    def test_router_no_longer_hardcodes_the_claims(self):
        src = open(os.path.join(_ROOT, "app", "bot", "router.py"),
                   encoding="utf-8").read()
        strings = " ".join(n.value for n in ast.walk(ast.parse(src))
                           if isinstance(n, ast.Constant) and isinstance(n.value, str))
        for gone in ("Government Recognised", "government-backed",
                     "Kerala & Gulf", "track record", "9 AM – 11 AM",
                     "12 PM – 2 PM", "5 PM – 7 PM", "Weekend batches"):
            assert gone not in strings, f"router still hardcodes {gone!r}"
        for name in ("RUTRONIX_FULL", "PSC_NOTE", "NORKA_NOTE", "LEARNING_MODES"):
            assert name not in src, f"router still uses {name}"


class TestTopicLookup:

    def test_lookup_is_tenant_scoped(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            ox = ks.fetch_topic_rows(OX, ("certificate",))
            g = ks.fetch_topic_rows(G, ("certificate",))
            d = ks.fetch_topic_rows(D, ("certificate",))
        assert ox and all(r.tenant_id == OX for r in ox)
        assert g and all(r.tenant_id == G for r in g)
        assert d == ()

    def test_lookup_matches_tags_not_titles(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            rows = ks.fetch_topic_rows(OX, ("timing", "batch", "schedule"))
        assert rows == (), "an untagged row must not answer a topic"

    def test_lookup_fails_closed_without_a_tenant(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            assert ks.fetch_topic_rows(None, ("certificate",)) == ()
            assert ks.fetch_topic_rows("", ("certificate",)) == ()
            assert ks.fetch_topic_rows(OX, ()) == ()

    def test_lookup_ignores_course_rows(self, seeded):
        """Course rows carry discovery keywords for the catalogue; a course
        tagged 'certificate' is a course, not a policy answer."""
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(TenantKnowledge(
                tenant_id=D, kind="course", title="Certificate Course",
                body="x", attributes=json.dumps({"keywords": ["certificate"]})))
            db.session.commit()
            assert ks.fetch_topic_rows(D, ("certificate",)) == ()

    def test_lookup_discards_everything_on_an_isolation_violation(self, seeded, monkeypatch):
        from app.services import knowledge_service as ks
        foreign = SimpleNamespace(tenant_id=G, kind="faq", title="x", body="y",
                                  attributes=json.dumps({"keywords": ["certificate"]}))

        class _Q:
            def limit(self, n):
                return self

            def all(self):
                return [foreign]

        monkeypatch.setattr(ks, "_base_query", lambda tid, kinds=None: _Q())
        with _APP.app_context():
            assert ks.fetch_topic_rows(OX, ("certificate",)) == ()


# ═══ TRUST_LINES ════════════════════════════════════════════════════════════

class TestTrustLines:

    def test_pool_is_non_empty(self):
        assert K.TRUST_LINES and all(isinstance(x, str) and x.strip()
                                     for x in K.TRUST_LINES)

    @pytest.mark.parametrize("claim", ("rutronix", "placement", "govern", "govt",
                                       "approved", "accredit", "recogni",
                                       "certif", "kerala", "india", "job"))
    def test_no_line_carries_an_institutional_claim(self, claim):
        for line in K.TRUST_LINES:
            assert claim not in line.lower(), f"TRUST_LINES claims {claim!r}: {line!r}"


# ═══ Shared AI prompt ═══════════════════════════════════════════════════════

class TestSharedPrompt:

    def test_template_carries_no_institutional_or_language_claim(self):
        for banned in SHARED_PROMPT_BANNED:
            assert banned not in prompts.EDUCATION_PROMPT_TEMPLATE, \
                f"shared template still says {banned!r}"

    @pytest.mark.parametrize("tid", [G, D, None])
    def test_non_oxford_prompt_has_no_oxford_claim(self, seeded, tid):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(tid, query="certificate psc")
        for banned in ("Rutronix", "PSC", "NORKA", "Government", "Govt",
                       "Kerala", "Gulf", "Malayali", "Manglish", "Recognition:"):
            assert banned not in out, f"{banned!r} reached {tid}'s prompt"

    def test_oxford_prompt_still_receives_its_own_knowledge(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(
                OX, persona_name="Krishna", query="psc eligibility")
        assert "PSC eligible" in out
        assert "Tagline: Kerala State Rutronix ATC" in out
        assert "Krishna" in out

    def test_prompt_tells_the_model_to_quote_claims_only_from_supplied_data(self):
        t = prompts.EDUCATION_PROMPT_TEMPLATE
        assert "BUSINESS PROFILE or BUSINESS KNOWLEDGE" in t
        assert "never invent deadlines, seat limits" in t.lower()


# ═══ Marketing Hub ══════════════════════════════════════════════════════════

HUB_OXFORD = ("The Oxford Computers", "Oxford Computers", "Oxford CRM",
              "Malayinkeezhu", "Trivandrum", "Thiruvananthapuram", "theoxfordedu",
              "9447329972", "Kerala Govt", "Government Certificate",
              "100% Placement", "Rutronix", "Attingal")


class TestMarketingHub:

    @pytest.mark.parametrize("tid,name", [(G, "Gamma Design School"),
                                          (D, "Delta Academy")])
    def test_non_oxford_hub_shows_its_own_name_and_no_oxford(self, seeded, tid, name):
        body = _client(seeded[tid]).get("/crm/marketing").get_data(as_text=True)
        assert f"const MKT_BUSINESS = {json.dumps(name)}" in body
        for literal in HUB_OXFORD:
            assert literal not in body, f"{literal!r} on {tid}'s Marketing Hub"

    def test_oxford_hub_shows_the_oxford_computers(self, seeded):
        body = _client(seeded[OX]).get("/crm/marketing").get_data(as_text=True)
        assert 'const MKT_BUSINESS = "The Oxford Computers"' in body

    def test_presets_interpolate_the_business_name(self, seeded):
        body = _client(seeded[G]).get("/crm/marketing").get_data(as_text=True)
        block = body.split("const MKT_TEMPLATES = {")[1].split("};")[0]
        assert block.count("${MKT_BUSINESS}") >= 5

    def test_campaign_start_passes_the_actors_tenant(self, seeded, monkeypatch):
        from app.services import campaign_service
        calls = []
        monkeypatch.setattr(campaign_service, "start_campaign",
                            lambda phones, msg, name, tenant_id=None:
                            calls.append(tenant_id))
        r = _client(seeded[G]).post("/crm/marketing/start_job", json={
            "phones": ["919000000009"], "message": "hi", "campaign_name": "t"})
        assert r.status_code == 200 and calls == [G]


# ═══ Platform identity: Xasnic ══════════════════════════════════════════════

class TestPlatformIdentity:

    def test_platform_name_is_centrally_configured(self):
        from app import config
        assert config.PLATFORM_NAME == "Xasnic"
        assert _APP.config["PLATFORM_NAME"] == "Xasnic"

    @pytest.mark.parametrize("path", ["/", "/register", "/pending",
                                      "/forgot-password", "/resend-verification",
                                      "/crm/login", "/crm/super/login"])
    def test_public_and_auth_pages_say_xasnic(self, seeded, path):
        body = _APP.test_client().get(path).get_data(as_text=True)
        assert "Xasnic" in body, path
        for literal in ("Oxford CRM", "Oxford Nova", "Oxford Computers"):
            assert literal not in body, f"{literal!r} on {path}"

    def test_tenant_sidebar_says_xasnic_and_keeps_the_tenant_name(self, seeded):
        body = _client(seeded[OX]).get("/tenant/profile").get_data(as_text=True)
        assert "Xasnic" in body
        assert "Oxford CRM" not in body
        assert "The Oxford Computers" in body          # tenant identity kept

    def test_crm_sidebar_says_xasnic(self, seeded):
        body = _client(seeded[G]).get("/crm/marketing").get_data(as_text=True)
        assert "Xasnic" in body and "Oxford CRM" not in body

    def test_no_template_names_the_platform_oxford(self):
        """Every template except the deferred legacy /panel."""
        hits = []
        for base, _, files in os.walk(os.path.join(_ROOT, "templates")):
            for name in files:
                rel = os.path.relpath(os.path.join(base, name), _ROOT).replace(os.sep, "/")
                if rel == "templates/panel.html" or not name.endswith(".html"):
                    continue
                text = open(os.path.join(base, name), encoding="utf-8").read()
                for literal in ("Oxford CRM", "Oxford Computers", "Oxford Nova"):
                    if literal in text:
                        hits.append((rel, literal))
        assert hits == [], hits

    def test_emails_say_xasnic(self, seeded, monkeypatch):
        from app.services.email_service import email_service
        sent = []
        monkeypatch.setattr(email_service, "email_client", SimpleNamespace(
            send_email=lambda **kw: sent.append(kw) or True))
        with _APP.test_request_context("/"):
            email_service.send_verification_email("a@x.test", "Asha")
            email_service.send_password_reset_email("a@x.test", 1, "h" * 40)
        assert [s["subject"] for s in sent] == [
            "Verify your Xasnic account", "Reset your Xasnic password"]
        for s in sent:
            assert "Xasnic" in s["html_content"]
            assert "Oxford" not in s["html_content"]

    def test_code_default_sender_name_is_xasnic(self):
        src = open(os.path.join(_ROOT, "app", "config.py"), encoding="utf-8").read()
        assert 'os.environ.get("BREVO_SENDER_NAME", "Xasnic")' in src


# ═══ /health ════════════════════════════════════════════════════════════════

class TestHealth:

    def test_health_is_200_with_only_operational_fields(self, seeded):
        r = _APP.test_client().get("/health")
        assert r.status_code == 200
        body = r.get_json()
        assert set(body) == {"status", "database", "scheduler", "whatsapp_token",
                             "app", "gemini_active"}
        assert body["app"] == "Xasnic" and body["status"] == "running"

    def test_health_exposes_no_aggregate_or_inventory(self, seeded):
        body = _APP.test_client().get("/health").get_data(as_text=True)
        for gone in ("leads_in_memory", "pending_followups", "features",
                     "sdk", "sheets_configured", "Oxford", "Manglish"):
            assert gone not in body, gone


# ═══ Profile save: no false success ═════════════════════════════════════════

def _profile_form(**over):
    form = {"name": "Gamma Design School", "bp_tagline": "Design Council Accredited",
            "bp_address_line": "7 Gamma Lane", "bp_address_city": "Lisbon",
            "bp_address_country": "Portugal", "bp_contact_phone": "351210000000",
            "bp_contact_website": "gamma.example"}
    form.update(over)
    return form


class TestProfileSave:

    def _settings_stamp(self, tid):
        with _APP.app_context():
            row = TenantSettings.query.filter_by(tenant_id=tid).first()
            return row.updated_at, row.settings

    def test_unchanged_submission_writes_nothing(self, seeded):
        before = self._settings_stamp(G)
        r = _client(seeded[G]).post("/tenant/profile", data=_profile_form(),
                                    follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "No changes to save." in body
        assert "Company profile updated successfully." not in body
        assert self._settings_stamp(G) == before

    def test_changed_submission_still_persists(self, seeded):
        r = _client(seeded[G]).post(
            "/tenant/profile", data=_profile_form(bp_tagline="Now Accredited"),
            follow_redirects=True)
        assert "Company profile updated successfully." in r.get_data(as_text=True)
        with _APP.app_context():
            row = TenantSettings.query.filter_by(tenant_id=G).first()
            assert json.loads(row.settings)["business_profile"]["tagline"] == "Now Accredited"

    def test_renaming_the_business_is_a_change(self, seeded):
        r = _client(seeded[G]).post(
            "/tenant/profile", data=_profile_form(name="Gamma School of Design"),
            follow_redirects=True)
        assert "Company profile updated successfully." in r.get_data(as_text=True)
        with _APP.app_context():
            assert db.session.get(Tenant, G).name == "Gamma School of Design"


# ═══ Unchanged by this phase ════════════════════════════════════════════════

class TestKeptUnchanged:

    def test_payment_confirmed_reply_is_present_and_byte_identical(self):
        """Kept for the future verified-payment phase (decision D7)."""
        src = open(os.path.join(_ROOT, "app", "bot", "offer_handlers.py"),
                   encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "payment_confirmed_reply")
        seg = ast.get_source_segment(src, fn)
        assert hashlib.sha256(seg.encode("utf-8")).hexdigest() == (
            "c63ea245936befd8440344a88f3b964ceda32ecd167f45e524cd69047f8ec092")


# ═══ Future-neutrality guard ════════════════════════════════════════════════

def _string_constants(rel):
    tree = ast.parse(open(os.path.join(_ROOT, rel), encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    # module/function/class docstrings are documentation, not content
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(n, clean=False)
            if doc in out:
                out.remove(doc)
    return out


class TestNeutralityGuard:

    @pytest.mark.parametrize("rel", ["app/bot/constants.py", "app/bot/router.py",
                                     "app/bot/tenant_topics.py"])
    def test_shared_bot_content_carries_no_regional_or_oxford_claim(self, rel):
        text = "\n".join(_string_constants(rel))
        for banned in ("Rutronix", "PSC", "NORKA", "Kerala", "Gulf",
                       "The Oxford Computers", "Malayinkeezhu", "theoxfordedu",
                       "9447329972", "Government Recognised", "government-backed",
                       "Govt Certified", "Oxford Nova"):
            assert banned not in text, f"{rel} carries {banned!r}"
        assert not re.search(r"\b9 AM\s*–\s*11 AM|\b12 PM\s*–\s*2 PM|\b5 PM\s*–\s*7 PM",
                             text), f"{rel} carries fixed batch times"

    def test_regional_claim_constants_are_gone(self):
        for name in ("RUTRONIX_LABEL", "RUTRONIX_FULL", "PSC_NOTE",
                     "NORKA_NOTE", "LEARNING_MODES", "AI_NOTE"):
            assert not hasattr(K, name), f"constants.{name} is back"
