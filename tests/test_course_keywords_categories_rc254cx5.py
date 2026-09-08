"""Phase RC2.5.4c-x-5a: tenant admins can manage course keywords and categories.

THE GAP
-------
`attributes.keywords` and `attributes.categories` were READ by the runtime and
written by NOBODY:

    keywords   -> catalogue_service.match_keyword       (free-text discovery)
    categories -> catalogue_service.courses_for_category (goal recommendations)

All sixteen authored Oxford courses carry both, from RC2.5.5c-2's data-only
commit. They survived admin edits only incidentally, via the `dict(existing)`
copy in _merge_attributes -- exactly the accident commercial.normal_total_fee
lived on before RC2.5.4c-x-2. But validate_payload collected neither, so a
course CREATED through the admin form got neither, and was therefore:

  * unreachable by free text -- match_keyword had no candidate to offer, which
    is why "full stack web development" resolved to PDWD on the strength of
    PDWD's 3-character keyword "web";
  * absent from every goal recommendation -- courses_for_category matches on
    `categories`, so the course appeared in no Job/Business/Accounting/Basic
    menu at all.

An admin could not fix either through any supported surface.

BLANK SEMANTICS ARE ASYMMETRIC WITH THE PRICE GUARD, DELIBERATELY
------------------------------------------------------------------
Blank keywords/categories POP the key. Blank price does NOT pop
normal_total_fee (RC2.5.4c-x-2). The difference is intentional: blanking a
price would silently delete a customer-facing money value for a field the
admin never saw, while blanking keywords is a legible act with a visible
effect -- and leaving stale keywords un-clearable would be the worse failure,
the same reasoning `code` already records for itself. Both halves are pinned
here.

OUT OF SCOPE: catalogue_service, match_keyword, KEYWORD_TO_COURSE, the
platform default catalogue, official_name, emi_available, eligibility, audit
logging, and FST01 (deactivated in production; neither reactivated nor edited).
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

_DB = os.path.join(tempfile.gettempdir(), "rc254cx5_kwcat.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx5-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx5-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx5-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge, User                    # noqa: E402
from app.services import knowledge_admin_service as kas                 # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.services import payment_link_service as pls                    # noqa: E402

OX = "t-ox"
OTHER = "t-rival"
KAS_PY = os.path.join(_ROOT, "app", "services", "knowledge_admin_service.py")
LEGACY_URL = "https://rzp.io/rzp/KAQ2C7t"
ACTIVE_URL = "https://rzp.io/rzp/ACTIVE99"

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


def _mk_k(tenant, title, attrs, order=0):
    row = TenantKnowledge(
        tenant_id=tenant, kind="course", title=title, body="Body.",
        attributes=json.dumps(attrs), is_active=True, sort_order=order)
    db.session.add(row)
    db.session.commit()
    return row


def _form(**overrides):
    base = {"title": "New Course", "kind": "course", "body": "Body text.",
            "sort_order": "5", "duration": "6 Months", "currency": "INR",
            "base_price": "1000", "code": "NEW1", "payment_url": "",
            "keywords": "", "registration_fee": "", "tuition_fee": "",
            "concession": "", "net_tuition_fee": "", "exam_fee": ""}
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

        # a fully-authored RC2.5.5c-2-shaped course
        rich = _mk_k(OX, "PGDCA – Computer Applications", {
            "duration": "12 Months",
            "official_name": "Post Graduate Diploma in Computer Applications",
            "categories": ["job", "business"],
            "keywords": ["pgdca", "pgd"],
            "eligibility": "Any degree",
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

        # a bare course, the shape the admin form used to produce
        bare = _mk_k(OX, "Bare Course", {
            "duration": "6 Months",
            "commercial": {"code": "BARE1", "currency": "INR",
                           "normal_total_fee": 5000, "base_price": 5000,
                           "offers": []}}, order=2)

        rival = _mk_k(OTHER, "Rival Course", {
            "duration": "3 Months",
            "categories": ["basic"], "keywords": ["rival"],
            "commercial": {"code": "RIV1", "currency": "INR",
                           "normal_total_fee": 900, "base_price": 900,
                           "offers": []}}, order=1)

        data = {"ox_admin": ox_admin.id, "rival_admin": rival_admin.id,
                "rich": rich.id, "bare": bare.id, "rival": rival.id}
    yield data
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ 1, 10 — CREATE ═══════════════════════════════════════════════════════

class TestCreate:

    def test_create_stores_keywords_and_categories(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Full Stack", code="FS1",
                       keywords="full stack, mern, react",
                       categories=["job", "business"]))
        assert r.status_code in (200, 302)
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Full Stack").one().attributes)
        assert a["keywords"] == ["full stack", "mern", "react"]
        assert a["categories"] == ["job", "business"]

    def test_multiple_categories_survive_getlist(self, seeded):
        """The MultiDict trap: request.form.get() returns only the FIRST
        value, so reading a <select multiple> with .get() would silently
        store one category and drop the rest."""
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Many Cats", code="MC1",
                       categories=["job", "business", "accounting", "basic"]))
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Many Cats").one().attributes)
        assert len(a["categories"]) == 4
        assert set(a["categories"]) == {"job", "business", "accounting", "basic"}

    def test_create_without_either_field_stores_neither(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Neither", code="NEI1"))
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Neither").one().attributes)
        assert "keywords" not in a and "categories" not in a


# ═══ 2, 3, 4 — EDIT and the blank-pop contract ════════════════════════════

class TestEditAndBlank:

    def test_edit_updates_both(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="pgdca, diploma", categories=["accounting"]))
        a = _attrs(seeded["rich"])
        assert a["keywords"] == ["pgdca", "diploma"]
        assert a["categories"] == ["accounting"]

    def test_blank_keywords_removes_the_key(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="", categories=["job"]))
        a = _attrs(seeded["rich"])
        assert "keywords" not in a, "blank keywords must REMOVE the key"
        assert a["categories"] == ["job"], "categories must be unaffected"

    def test_blank_categories_removes_the_key(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="pgdca"))
        a = _attrs(seeded["rich"])
        assert "categories" not in a, "no selection must REMOVE the key"
        assert a["keywords"] == ["pgdca"]

    def test_the_two_blank_branches_are_independent(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="", categories=[]))
        a = _attrs(seeded["rich"])
        assert "keywords" not in a and "categories" not in a

    def test_blank_price_still_preserves_normal_total_fee(self, seeded):
        """The RC2.5.4c-x-2 guard is UNCHANGED by this phase. The asymmetry
        with the pop-on-blank above is deliberate."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="",
                       keywords="pgdca", categories=["job"]))
        c = _attrs(seeded["rich"])["commercial"]
        assert "base_price" not in c
        assert c["normal_total_fee"] == 19540, \
            "the price guard was altered by this phase"

    def test_a_bare_course_gains_both_on_edit(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords="bare, starter", categories=["basic"]))
        a = _attrs(seeded["bare"])
        assert a["keywords"] == ["bare", "starter"]
        assert a["categories"] == ["basic"]


# ═══ 5 — normalisation ════════════════════════════════════════════════════

class TestNormalisation:

    @pytest.mark.parametrize("raw,expected", [
        ("  PGDCA ,  PGD  ", ["pgdca", "pgd"]),          # trim + lowercase
        ("a,,b,", ["a", "b"]),                            # empties dropped
        ("dup, DUP, dup", ["dup"]),                       # de-duplicated
        ("Web Design, web design", ["web design"]),       # case-insensitive dup
        ("zebra, alpha", ["zebra", "alpha"]),             # order PRESERVED
        ("tally prime", ["tally prime"]),                 # spaces kept
        ("r&d, fast-track", ["r&d", "fast-track"]),       # & and - allowed
    ])
    def test_keyword_normalisation(self, seeded, raw, expected):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords=raw))
        assert _attrs(seeded["bare"])["keywords"] == expected

    def test_duplicate_categories_collapse(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       categories=["job", "job", "JOB", " job "]))
        assert _attrs(seeded["bare"])["categories"] == ["job"]

    def test_stored_shape_matches_the_sixteen_authored_courses(self, seeded):
        """A list of lower-case, trimmed, de-duplicated strings at the TOP
        level -- byte-compatible with what RC2.5.5c-2 authored."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords="One, Two", categories=["job"]))
        a = _attrs(seeded["bare"])
        assert isinstance(a["keywords"], list) and isinstance(a["categories"], list)
        assert all(isinstance(x, str) and x == x.strip().lower()
                   for x in a["keywords"] + a["categories"])


# ═══ 6, 7, 8, 9 — validation ══════════════════════════════════════════════

class TestValidation:

    def test_too_many_keywords_rejected(self, seeded):
        many = ", ".join(f"kw{i}" for i in range(kas.MAX_KEYWORDS + 1))
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords=many))
        assert r.status_code == 400
        assert "keywords" not in _attrs(seeded["bare"]), "rejected but stored"

    def test_over_long_keyword_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords="a" * (kas.MAX_KEYWORD_LEN + 1)))
        assert r.status_code == 400
        assert "keywords" not in _attrs(seeded["bare"])

    @pytest.mark.parametrize("bad", [
        "<script>", "drop;table", "a\"b", "semi;colon", "path/slash", "at@sign",
    ])
    def test_invalid_keyword_characters_rejected(self, seeded, bad):
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords=bad))
        assert r.status_code == 400
        assert "keywords" not in _attrs(seeded["bare"])

    def test_category_outside_the_vocabulary_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       categories=["job", "not-a-category"]))
        assert r.status_code == 400
        assert "categories" not in _attrs(seeded["bare"]), \
            "an invalid category was partially saved"

    def test_the_vocabulary_is_catalogue_services_own(self, seeded):
        cleaned, errors = kas.validate_payload(
            _form(categories=list(cat.CATEGORIES)))
        assert not errors
        assert sorted(cleaned["categories"]) == sorted(cat.CATEGORIES)

    def test_validation_never_raises_on_odd_input(self, seeded):
        for value in (None, "", ",,,", " , , "):
            cleaned, errors = kas.validate_payload(_form(keywords=value))
            assert cleaned["keywords"] is None


# ═══ 11, 12 — the runtime effect ══════════════════════════════════════════

class TestRuntimeEffect:

    def test_a_new_keyword_makes_the_course_discoverable(self, seeded):
        with _APP.app_context():
            before = cat.match_keyword(OX, "i want mern stack")
        assert getattr(before.course, "code", None) != "BARE1"

        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords="mern stack"))
        with _APP.app_context():
            after = cat.match_keyword(OX, "i want mern stack")
        assert after.kind == "exact"
        assert after.course.code == "BARE1"

    def test_a_new_category_makes_the_course_recommendable(self, seeded):
        with _APP.app_context():
            before = [c.code for c in cat.courses_for_category(OX, "accounting")]
        assert "BARE1" not in before

        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       categories=["accounting"]))
        with _APP.app_context():
            after = [c.code for c in cat.courses_for_category(OX, "accounting")]
        assert "BARE1" in after

    def test_clearing_keywords_removes_discoverability(self, seeded):
        with _APP.app_context():
            assert cat.match_keyword(OX, "pgdca please").course.code == "PGDCA"
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords=""))
        with _APP.app_context():
            assert cat.match_keyword(OX, "pgdca please").kind == "none"

    def test_catalogue_service_source_is_untouched(self):
        """This phase changes the WRITE path only."""
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/catalogue_service.py", "app/bot/constants.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", f"runtime resolver changed: {out}"


# ═══ 13 — edit-form prefill ═══════════════════════════════════════════════

class TestPrefill:

    def test_edit_form_prefills_keywords_and_preselects_categories(self, seeded):
        body = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}/edit").get_data(as_text=True)
        assert 'name="keywords"' in body
        assert "pgdca, pgd" in body
        assert 'name="categories"' in body
        assert body.count("selected") >= 2          # job + business + kind

    def test_a_row_without_either_key_prefills_empty(self, seeded):
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['bare']}/edit")
        assert r.status_code == 200
        assert 'name="keywords"' in r.get_data(as_text=True)

    def test_the_create_form_renders_the_vocabulary(self, seeded):
        body = client(seeded["ox_admin"]).get(
            "/tenant/courses/new").get_data(as_text=True)
        for c in cat.CATEGORIES:
            assert f'value="{c}"' in body


# ═══ 14 — preservation ════════════════════════════════════════════════════

class TestPreservation:

    def test_unrelated_attributes_survive(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="Renamed", code="PGDCA", base_price="19540",
                       keywords="pgdca", categories=["job"]))
        a = _attrs(seeded["rich"])
        assert a["official_name"].startswith("Post Graduate")
        assert a["eligibility"] == "Any degree"
        assert a["historical_alias"] == "Computer Teacher Training"
        assert a["regulatory"]["source"] == "Kerala State Rutronix fee card"
        assert a["regulatory"]["as_of"] == "2026"
        assert a["duration"] == "6 Months"

    def test_commercial_attributes_survive(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000",
                       payment_url=ACTIVE_URL,
                       keywords="pgdca", categories=["job"]))
        c = _attrs(seeded["rich"])["commercial"]
        assert c["base_price"] == 20000 and c["normal_total_fee"] == 20000
        assert c["emi_available"] is True
        assert c["offers"][0]["label"] == "Diwali"
        assert c["legacy_payment_url"] == LEGACY_URL
        assert c["payment_url"] == ACTIVE_URL
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "PGDCA") == ACTIVE_URL

    def test_legacy_payment_url_still_non_writable(self, seeded):
        form = _form(title="PGDCA", code="PGDCA", base_price="19540",
                     keywords="pgdca")
        form["legacy_payment_url"] = "https://evil.example.com/x"
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit", data=form)
        a = _attrs(seeded["rich"])
        assert a["commercial"]["legacy_payment_url"] == LEGACY_URL
        assert "evil.example.com" not in json.dumps(a)

    def test_editing_keywords_does_not_disturb_another_course(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="changed", categories=["basic"]))
        b = _attrs(seeded["bare"])
        assert "keywords" not in b and "categories" not in b
        assert b["commercial"]["normal_total_fee"] == 5000

    def test_merge_writes_at_the_top_level_not_under_commercial(self, seeded):
        merged = kas._merge_attributes(
            {"commercial": {"base_price": 1, "offers": []}},
            kas.validate_payload(_form(keywords="a", categories=["job"]))[0])
        assert merged["keywords"] == ["a"]
        assert "keywords" not in merged["commercial"]
        assert "categories" not in merged["commercial"]


# ═══ 15, 16 — tenant isolation ════════════════════════════════════════════

class TestTenantIsolation:

    def test_a_tenant_cannot_edit_another_tenants_course(self, seeded):
        r = client(seeded["rival_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="Hijacked", code="PGDCA", base_price="1",
                       keywords="hijack", categories=["job"]))
        assert r.status_code == 404
        a = _attrs(seeded["rich"])
        assert a["keywords"] == ["pgdca", "pgd"]
        assert a["categories"] == ["job", "business"]

    def test_form_supplied_tenant_id_is_not_authority(self, seeded):
        form = _form(title="Injected", code="INJ1", keywords="inj",
                     categories=["job"])
        form["tenant_id"] = OTHER
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=form)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Injected").one()
        assert row.tenant_id == OX

    def test_keywords_do_not_leak_across_tenants(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['bare']}/edit",
            data=_form(title="Bare Course", code="BARE1", base_price="5000",
                       keywords="oxfordmark"))
        with _APP.app_context():
            assert cat.match_keyword(OTHER, "oxfordmark").kind == "none"
            assert cat.match_keyword(OX, "oxfordmark").course.code == "BARE1"

    def test_categories_do_not_leak_across_tenants(self, seeded):
        with _APP.app_context():
            ox_basic = [c.code for c in cat.courses_for_category(OX, "basic")]
            b_basic = [c.code for c in cat.courses_for_category(OTHER, "basic")]
        assert "RIV1" not in ox_basic
        assert b_basic == ["RIV1"]

    def test_rival_row_untouched_by_oxford_edits(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="19540",
                       keywords="x", categories=["job"]))
        a = _attrs(seeded["rival"])
        assert a["keywords"] == ["rival"] and a["categories"] == ["basic"]


# ═══ 17 — authored data is only changed when explicitly edited ════════════

class TestAuthoredDataStability:

    def test_an_unrelated_edit_does_not_touch_keywords_or_categories(self, seeded):
        """Editing only the title must leave both fields exactly as authored --
        the form resubmits them, so this proves the round trip is lossless."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="New Title Only", code="PGDCA", base_price="19540",
                       keywords="pgdca, pgd", categories=["job", "business"]))
        a = _attrs(seeded["rich"])
        assert a["keywords"] == ["pgdca", "pgd"]
        assert a["categories"] == ["job", "business"]

    def test_prefill_round_trips_losslessly(self, seeded):
        """GET the edit form, resubmit exactly what it rendered, and the
        stored values must be identical -- the property that stops a routine
        edit from silently degrading authored discovery data."""
        cl = client(seeded["ox_admin"])
        body = cl.get(f"/tenant/courses/{seeded['rich']}/edit").get_data(as_text=True)
        assert "pgdca, pgd" in body
        cl.post(f"/tenant/courses/{seeded['rich']}/edit",
                data=_form(title="PGDCA – Computer Applications", code="PGDCA",
                           base_price="19540", duration="12 Months",
                           keywords="pgdca, pgd",
                           categories=["job", "business"]))
        a = _attrs(seeded["rich"])
        assert a["keywords"] == ["pgdca", "pgd"]
        assert a["categories"] == ["job", "business"]


# ═══ source contract ══════════════════════════════════════════════════════

class TestSourceContract:

    def test_categories_are_read_with_getlist(self):
        src = open(KAS_PY, encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "validate_payload")
        seg = ast.get_source_segment(src, fn)
        assert "getlist" in seg, "a <select multiple> must not be read with .get()"

    def test_the_merge_pops_both_keys_on_blank(self):
        src = open(KAS_PY, encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_merge_attributes")
        popped = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "pop":
                if node.args and isinstance(node.args[0], ast.Constant):
                    popped.add(node.args[0].value)
        assert {"keywords", "categories"} <= popped
        assert "normal_total_fee" not in popped, \
            "the RC2.5.4c-x-2 price guard was altered"
