"""Phase RC2.5.4c-x-6a: tenant admins can manage official_name and eligibility.

THE GAP
-------
Both are stored at the TOP LEVEL of TenantKnowledge.attributes and were
writable by nobody:

    official_name  16/16 authored courses have it; ZERO readers in app/
    eligibility    14/16 have it; only prompt GUARDRAILS mention it

Neither has a deterministic customer path. `official_name` has no reader
anywhere in app/ at all, and eligibility's three source mentions are
instructions telling the model NOT to invent one:

    prompt_composer.py:77  "Never invent prices, offers, guarantees,
                            eligibility or legal claims"
    prompts.py:38          "Do NOT claim PSC eligibility ... unless certain"
    constants.py:41        "always quote exactly -- never invent eligibility"

So both reach a customer solely through knowledge_service._flatten_attrs,
which renders every non-excluded scalar into the AI's knowledge block. That
flattener is NOT touched by this phase; these tests assert it still carries
both fields.

WHY ELIGIBILITY IS A SELECT, NOT FREE TEXT
-------------------------------------------
It is an entry-requirement claim quoted to customers by a generative model.
Production holds exactly three values (11 x "10th and Above", 2 x "+2 &
Above", 1 x "Any Degree"). Free text would let an admin type a regulatory
claim in prose straight into the AI's context; a closed vocabulary keeps the
stored values identical to the ones already authored.

NOT IN THIS PHASE: emi_available. It is nested under `commercial`, boolean,
and rendered as a FINANCIAL claim in four customer-facing places with no EMI
mechanism anywhere in the platform -- a different risk profile, deliberately
split out (RC2.5.4c-x-6 audit, Phase B). Tests here prove it is untouched.

NO BACKFILL. DCA and DGSTP have no eligibility in production and keep it that
way; this phase creates the capability, it does not populate records.
"""
import ast
import json
import os
import sys
import tempfile

import pytest
from werkzeug.security import generate_password_hash

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6a_name_elig.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6a-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge, User                    # noqa: E402
from app.services import knowledge_admin_service as kas                 # noqa: E402
from app.services import knowledge_service as ks                        # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.services import payment_link_service as pls                    # noqa: E402

OX = "t-ox"
OTHER = "t-rival"
KAS_PY = os.path.join(_ROOT, "app", "services", "knowledge_admin_service.py")
LEGACY_URL = "https://rzp.io/rzp/KAQ2C7t"
ACTIVE_URL = "https://rzp.io/rzp/ACTIVE99"
PGDCA_OFFICIAL = "Post Graduate Diploma in Computer Applications (PGDCA)"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _mk_user(tenant, username, role="ADMIN"):
    u = User(username=username, email=f"{username}.{tenant}@x.test",
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _mk_k(tenant, title, attrs, order=0, active=True):
    row = TenantKnowledge(
        tenant_id=tenant, kind="course", title=title, body="Body.",
        attributes=json.dumps(attrs), is_active=active, sort_order=order)
    db.session.add(row)
    db.session.commit()
    return row


def _form(**overrides):
    base = {"title": "New Course", "kind": "course", "body": "Body text.",
            "sort_order": "5", "duration": "6 Months", "currency": "INR",
            "base_price": "1000", "code": "NEW1", "payment_url": "",
            "keywords": "", "official_name": "", "eligibility": "",
            "registration_fee": "", "tuition_fee": "", "concession": "",
            "net_tuition_fee": "", "exam_fee": ""}
    base.update(overrides)
    return base


def _attrs(row_id):
    with _APP.app_context():
        return json.loads(TenantKnowledge.query.get(row_id).attributes)


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding -- flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Rival")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        ox_admin = _mk_user(OX, "admin_ox")
        rival_admin = _mk_user(OTHER, "admin_rival")

        # a fully-authored course, the RC2.5.5c-2 shape
        rich = _mk_k(OX, "PGDCA – Computer Applications", {
            "duration": "12 Months",
            "official_name": PGDCA_OFFICIAL,
            "eligibility": "Any Degree",
            "categories": ["job", "business"],
            "keywords": ["pgdca", "pgd"],
            "historical_alias": "Computer Teacher Training",
            "commercial": {"code": "PGDCA", "currency": "INR",
                           "normal_total_fee": 19540, "base_price": 19540,
                           "emi_available": True, "payment_url": ACTIVE_URL,
                           "legacy_payment_url": LEGACY_URL,
                           "offers": [{"label": "Diwali", "final_price": 17999}]},
            "regulatory": {"source": "Kerala State Rutronix fee card",
                           "as_of": "2026",
                           "components": [{"type": "registration_fee",
                                           "label": "Registration Fee",
                                           "amount": 4500}]}}, order=1)

        # the DCA/DGSTP production shape: NO eligibility key
        no_elig = _mk_k(OX, "DCA Fast Track – Computer Applications", {
            "duration": "6 Months",
            "official_name": "Diploma in Computer Application (DCA - Fast Track)",
            "categories": ["job", "basic"], "keywords": ["dca fast track"],
            "commercial": {"code": "DCA", "currency": "INR",
                           "normal_total_fee": 8350, "base_price": 8350,
                           "emi_available": True, "offers": []}}, order=2)

        # an FST01-shaped inactive record: neither field, must stay untouched
        inactive = _mk_k(OX, "full stack web development", {
            "duration": "12 Months",
            "commercial": {"code": "FST01", "currency": "INR",
                           "normal_total_fee": 50000, "base_price": 50000,
                           "offers": [], "payment_url": None}},
            order=3, active=False)

        rival = _mk_k(OTHER, "Rival Course", {
            "duration": "3 Months",
            "official_name": "Rival Official", "eligibility": "+2 & Above",
            "commercial": {"code": "RIV1", "currency": "INR",
                           "normal_total_fee": 900, "base_price": 900,
                           "offers": []}}, order=1)

        data = {"ox_admin": ox_admin.id, "rival_admin": rival_admin.id,
                "rich": rich.id, "no_elig": no_elig.id,
                "inactive": inactive.id, "rival": rival.id}
    yield data
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ 1-5 — official_name ══════════════════════════════════════════════════

class TestOfficialName:

    def test_create_stores_official_name(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="New", code="N1",
                       official_name="Diploma in Something (DIS)"))
        assert r.status_code in (200, 302)
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="New").one().attributes)
        assert a["official_name"] == "Diploma in Something (DIS)"

    def test_edit_updates_official_name(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="Renamed Official Name",
                       eligibility="Any Degree"))
        assert _attrs(seeded["rich"])["official_name"] == "Renamed Official Name"

    def test_blank_official_name_removes_the_key(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="", eligibility="Any Degree"))
        a = _attrs(seeded["rich"])
        assert "official_name" not in a
        assert a["eligibility"] == "Any Degree", "the two must be independent"

    @pytest.mark.parametrize("raw,expected", [
        ("  Padded Name  ", "Padded Name"),
        ("Double  spaced   name", "Double spaced name"),
        ("Line\nbreak name", "Line break name"),
        ("\tTabbed\tname", "Tabbed name"),
    ])
    def test_whitespace_is_collapsed(self, seeded, raw, expected):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name=raw))
        assert _attrs(seeded["rich"])["official_name"] == expected

    def test_over_long_official_name_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="x" * (kas.MAX_OFFICIAL_NAME_LEN + 1)))
        assert r.status_code == 400
        assert _attrs(seeded["rich"])["official_name"] == PGDCA_OFFICIAL, \
            "a rejected submission must not have written anything"

    def test_the_longest_production_value_is_accepted(self, seeded):
        """Longest in production is 75 chars; the ceiling must not reject it."""
        name = "Certificate in Word Processing and Data Entry Operator (CWPDE)"
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name=name))
        assert _attrs(seeded["rich"])["official_name"] == name

    def test_official_name_is_not_course_identity(self, seeded):
        """Changing it must not move get_course, resolve_legacy_name or the
        catalogue title -- identity is commercial.code."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA – Computer Applications", code="PGDCA",
                       base_price="19540", official_name="Totally Different"))
        with _APP.app_context():
            rec = cat.get_course(OX, "PGDCA")
            assert rec is not None and rec.code == "PGDCA"
            assert rec.title == "PGDCA – Computer Applications"
            assert cat.get_course(OX, "TOTALLY-DIFFERENT") is None
            assert cat.resolve_legacy_name(OX, "Totally Different") is None
            assert cat.match_keyword(OX, "totally different").kind == "none"


# ═══ 6-10 — eligibility ═══════════════════════════════════════════════════

class TestEligibility:

    def test_create_stores_eligibility(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="New", code="N1", eligibility="10th and Above"))
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="New").one().attributes)
        assert a["eligibility"] == "10th and Above"

    def test_edit_updates_eligibility(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       eligibility="+2 & Above"))
        assert _attrs(seeded["rich"])["eligibility"] == "+2 & Above"

    def test_blank_eligibility_removes_the_key(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       eligibility="", official_name=PGDCA_OFFICIAL))
        a = _attrs(seeded["rich"])
        assert "eligibility" not in a
        assert a["official_name"] == PGDCA_OFFICIAL, "the two must be independent"

    @pytest.mark.parametrize("value", ["10th and Above", "+2 & Above", "Any Degree"])
    def test_every_allowed_value_is_accepted(self, seeded, value):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       eligibility=value))
        assert _attrs(seeded["rich"])["eligibility"] == value

    @pytest.mark.parametrize("bad", [
        "Graduate", "10th", "anything at all", "PSC eligible",
        "<script>alert(1)</script>", "Any Degree or equivalent",
    ])
    def test_invalid_eligibility_rejected(self, seeded, bad):
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       eligibility=bad))
        assert r.status_code == 400
        assert _attrs(seeded["rich"])["eligibility"] == "Any Degree", \
            "a rejected value must not have been written"

    def test_case_and_padding_normalise_to_canonical_storage(self, seeded):
        """Stored in the vocabulary's own casing, so the fourteen authored
        values keep their exact byte shape."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       eligibility="  any degree  "))
        assert _attrs(seeded["rich"])["eligibility"] == "Any Degree"

    def test_the_vocabulary_is_exactly_the_three_production_values(self):
        assert kas.ELIGIBILITY_VALUES == (
            "10th and Above", "+2 & Above", "Any Degree")

    def test_a_row_without_eligibility_keeps_it_absent(self, seeded):
        """The DCA/DGSTP shape. Editing it WITHOUT choosing an eligibility
        must not invent one -- no backfill by side effect."""
        assert "eligibility" not in _attrs(seeded["no_elig"])
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['no_elig']}/edit",
            data=_form(title="DCA Fast Track – Computer Applications",
                       code="DCA", base_price="8350",
                       official_name="Diploma in Computer Application (DCA - Fast Track)",
                       keywords="dca fast track", categories=["job", "basic"]))
        a = _attrs(seeded["no_elig"])
        assert "eligibility" not in a, "an edit invented an eligibility claim"
        assert a["official_name"].startswith("Diploma in Computer")


# ═══ 11, 12 — prefill and AI delivery ═════════════════════════════════════

class TestPrefillAndAI:

    def test_edit_form_prefills_both(self, seeded):
        body = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}/edit").get_data(as_text=True)
        assert 'name="official_name"' in body
        assert PGDCA_OFFICIAL in body
        assert 'name="eligibility"' in body
        assert 'value="Any Degree"' in body and "selected" in body

    def test_a_row_without_eligibility_prefills_not_specified(self, seeded):
        body = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['no_elig']}/edit").get_data(as_text=True)
        assert "Not specified" in body
        assert 'name="eligibility"' in body

    def test_create_form_renders_the_vocabulary(self, seeded):
        import html
        body = client(seeded["ox_admin"]).get(
            "/tenant/courses/new").get_data(as_text=True)
        # Jinja escapes the value, so "+2 & Above" renders as "+2 &amp; Above".
        # Asserting on the escaped form is the correct expectation -- an
        # UNescaped "&" in an attribute would be the bug.
        for v in kas.ELIGIBILITY_VALUES:
            assert f'value="{html.escape(v)}"' in body
        assert "&amp;" in body, "the ampersand must be HTML-escaped"
        assert "Not specified" in body

    def test_both_fields_reach_the_ai_prompt(self, seeded):
        """Via the EXISTING generic flattener -- not modified by this phase."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="AI Visible Name",
                       eligibility="10th and Above"))
        from app.services import prompt_composer as pc
        with _APP.app_context():
            block = ks.render_knowledge_block(OX, query="PGDCA")
            prompt = pc.compose_system_prompt(OX, query="PGDCA")
        assert "official_name: AI Visible Name" in block
        assert "eligibility: 10th and Above" in block
        assert "AI Visible Name" in prompt and "10th and Above" in prompt

    def test_removing_them_removes_them_from_the_prompt(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="", eligibility=""))
        with _APP.app_context():
            block = ks.render_knowledge_block(OX, query="PGDCA")
        assert "official_name" not in block
        assert "eligibility" not in block

    def test_the_flattener_was_not_modified(self):
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/knowledge_service.py",
             "app/services/prompt_composer.py",
             "app/services/catalogue_service.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", f"AI/catalogue path changed: {out}"


# ═══ 13, 14, 15, 20 — preservation ════════════════════════════════════════

class TestPreservation:

    def test_keywords_and_categories_preserved(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="X", eligibility="Any Degree",
                       keywords="pgdca, pgd", categories=["job", "business"]))
        a = _attrs(seeded["rich"])
        assert a["keywords"] == ["pgdca", "pgd"]
        assert a["categories"] == ["job", "business"]

    def test_commercial_price_and_payment_preserved(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000",
                       payment_url=ACTIVE_URL, official_name="X",
                       eligibility="Any Degree"))
        c = _attrs(seeded["rich"])["commercial"]
        assert c["base_price"] == 20000 and c["normal_total_fee"] == 20000
        assert c["payment_url"] == ACTIVE_URL
        assert c["offers"][0]["label"] == "Diwali"
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "PGDCA") == ACTIVE_URL

    def test_emi_available_is_untouched_by_this_phase(self, seeded):
        """emi_available is Phase B. This phase must neither write nor clear
        it, and it must survive an edit exactly as before."""
        before = _attrs(seeded["rich"])["commercial"]["emi_available"]
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="X", eligibility="Any Degree"))
        c = _attrs(seeded["rich"])["commercial"]
        assert c["emi_available"] is before is True
        with _APP.app_context():
            assert cat.get_course(OX, "PGDCA").emi_available is True

    def test_no_emi_form_field_exists(self, seeded):
        """Phase A must not ship an EMI control."""
        body = client(seeded["ox_admin"]).get(
            "/tenant/courses/new").get_data(as_text=True)
        assert 'name="emi_available"' not in body

    def test_blank_price_still_preserves_normal_total_fee(self, seeded):
        """The RC2.5.4c-x-2 guard is unchanged by this phase."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="",
                       official_name="X", eligibility="Any Degree"))
        c = _attrs(seeded["rich"])["commercial"]
        assert "base_price" not in c
        assert c["normal_total_fee"] == 19540

    def test_legacy_payment_url_remains_protected(self, seeded):
        form = _form(title="PGDCA", code="PGDCA", base_price="19540",
                     official_name="X", eligibility="Any Degree")
        form["legacy_payment_url"] = "https://evil.example.com/x"
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit", data=form)
        a = _attrs(seeded["rich"])
        assert a["commercial"]["legacy_payment_url"] == LEGACY_URL
        assert "evil.example.com" not in json.dumps(a)

    def test_unrelated_attributes_survive(self, seeded):
        """`registration_fee` is submitted because it HAS a form field: blank
        fee-component inputs clear regulatory.components, which is the
        pre-existing RC2.5.4b contract and not something this phase changes.
        The subject here is attributes with NO form field at all --
        historical_alias, regulatory.source and regulatory.as_of -- which must
        survive an edit rather than being silently destroyed."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="Renamed", code="PGDCA", base_price="19540",
                       official_name="X", eligibility="Any Degree",
                       registration_fee="4500"))
        a = _attrs(seeded["rich"])
        assert a["historical_alias"] == "Computer Teacher Training"
        assert a["regulatory"]["source"] == "Kerala State Rutronix fee card"
        assert a["regulatory"]["as_of"] == "2026"
        assert a["regulatory"]["components"][0]["amount"] == 4500

    def test_both_are_stored_at_the_top_level(self, seeded):
        merged = kas._merge_attributes(
            {"commercial": {"base_price": 1, "offers": []}},
            kas.validate_payload(_form(official_name="N",
                                       eligibility="Any Degree"))[0])
        assert merged["official_name"] == "N"
        assert merged["eligibility"] == "Any Degree"
        assert "official_name" not in merged["commercial"]
        assert "eligibility" not in merged["commercial"]


# ═══ 17 — the inactive FST01-shaped record ════════════════════════════════

class TestInactiveRecordUntouched:

    def test_an_inactive_row_is_not_altered_by_editing_another(self, seeded):
        before = _attrs(seeded["inactive"])
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="X", eligibility="Any Degree"))
        after = _attrs(seeded["inactive"])
        assert after == before
        assert "official_name" not in after and "eligibility" not in after
        with _APP.app_context():
            row = TenantKnowledge.query.get(seeded["inactive"])
            assert row.is_active is False
            assert cat.get_course(OX, "FST01") is None


# ═══ 18, 19 — tenant isolation ════════════════════════════════════════════

class TestTenantIsolation:

    def test_a_tenant_cannot_edit_another_tenants_course(self, seeded):
        r = client(seeded["rival_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="Hijacked", code="PGDCA", base_price="1",
                       official_name="Hijack", eligibility="10th and Above"))
        assert r.status_code == 404
        a = _attrs(seeded["rich"])
        assert a["official_name"] == PGDCA_OFFICIAL
        assert a["eligibility"] == "Any Degree"

    def test_form_supplied_tenant_id_is_not_authority(self, seeded):
        form = _form(title="Injected", code="INJ1", official_name="Inj",
                     eligibility="Any Degree")
        form["tenant_id"] = OTHER
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=form)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Injected").one()
        assert row.tenant_id == OX

    def test_rival_row_untouched_by_oxford_edits(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       official_name="X", eligibility="Any Degree"))
        a = _attrs(seeded["rival"])
        assert a["official_name"] == "Rival Official"
        assert a["eligibility"] == "+2 & Above"

    def test_neither_field_leaks_across_tenants_in_the_prompt(self, seeded):
        with _APP.app_context():
            ox = ks.render_knowledge_block(OX, query="course")
            rv = ks.render_knowledge_block(OTHER, query="course")
        assert "Rival Official" not in ox
        assert PGDCA_OFFICIAL not in rv


# ═══ source contract ══════════════════════════════════════════════════════

class TestSourceContract:

    def test_merge_pops_both_on_blank_and_never_pops_normal_total_fee(self):
        src = open(KAS_PY, encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_merge_attributes")
        popped = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "pop":
                if node.args and isinstance(node.args[0], ast.Constant):
                    popped.add(node.args[0].value)
        assert {"official_name", "eligibility"} <= popped
        assert "normal_total_fee" not in popped

    def test_emi_available_is_not_written_by_the_admin_service(self):
        """Phase B has not been implemented."""
        src = open(KAS_PY, encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_merge_attributes")
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)):
                key = getattr(node.targets[0].slice, "value", None)
                assert key != "emi_available", "EMI was implemented in Phase A"
