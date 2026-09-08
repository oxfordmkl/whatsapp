"""Phase RC2.5.4c-x-2: the admin write path writes ONE customer price to BOTH keys.

THE DEFECT
----------
Two fields hold one course's customer-facing total:

    commercial.base_price        written by the RC2.5.4b admin UI
    commercial.normal_total_fee  NO WRITER ANYWHERE in the application

The sixteen production values of normal_total_fee came from RC2.5.5c-2, a
data-only commit that shipped no runnable writer. The catalogue read path
prefers normal_total_fee (RC2.5.5c-3) and falls back to base_price only when
it is absent (RC2.5.4c-x). So an admin edit moved base_price while the
customer kept being quoted the stale normal_total_fee -- production PGDCA
carried 19540 and 16000 simultaneously for roughly seventeen hours.

Dual-write makes the two incapable of diverging on any row an admin touches.

THE CRITICAL GUARD
------------------
The blank branch is ASYMMETRIC and that is deliberate. A blank price pops
base_price, as it always has, and must NOT pop normal_total_fee: doing so
would silently delete the customer-facing price from the sixteen courses
RC2.5.5c-2 authored -- a blank form field erasing what a business charges.
test_blank_price_preserves_normal_total_fee is the assertion that holds that
line, and mutant M5 exists solely to keep it honest.

OUT OF SCOPE: no schema change, no migration, no backfill. FST01 keeps its
missing normal_total_fee until someone edits it -- the RC2.5.4c-x read
fallback still serves it, and that fallback is deliberately retained. The
separate keyword defect (FOLLOW-UP -- COURSE KEYWORD RESOLUTION / FST01) is
untouched.
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

_DB = os.path.join(tempfile.gettempdir(), "rc254cx2_dualwrite.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx2-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx2-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx2-broadcast-key")
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
    """A valid baseline submission; override individual fields per test."""
    base = {"title": "New Course", "kind": "course", "body": "Body text.",
            "sort_order": "5", "duration": "6 Months", "currency": "INR",
            "base_price": "1000", "code": "NEW1", "payment_url": "",
            "registration_fee": "", "tuition_fee": "", "concession": "",
            "net_tuition_fee": "", "exam_fee": ""}
    base.update(overrides)
    return base


def _commercial(row_id):
    with _APP.app_context():
        row = TenantKnowledge.query.get(row_id)
        return json.loads(row.attributes)["commercial"]


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

        # the ordinary RC2.5.5c-2 shape: both keys, agreeing
        aligned = _mk_k(OX, "PGDCA – Computer Applications", {
            "duration": "12 Months",
            "official_name": "Post Graduate Diploma in Computer Applications",
            "categories": ["job"], "keywords": ["pgdca"],
            "eligibility": "Any degree",
            "historical_alias": "Computer Teacher Training",
            "commercial": {"code": "PGDCA", "currency": "INR",
                           "normal_total_fee": 19540, "base_price": 19540,
                           "emi_available": True,
                           "payment_url": ACTIVE_URL,
                           "legacy_payment_url": LEGACY_URL,
                           "offers": [{"label": "Diwali", "final_price": 17999}]},
            "regulatory": {"source": "Kerala State Rutronix fee card",
                           "as_of": "2026",
                           "components": [
                               {"type": "registration_fee",
                                "label": "Registration Fee", "amount": 4500},
                               {"type": "net_tuition_fee",
                                "label": "Net Tuition Fee to ATC",
                                "amount": 15040}]}}, order=1)

        # the FST01 shape: base_price only, created through the admin UI
        fst = _mk_k(OX, "full stack web development", {
            "duration": "12 Months", "categories": ["job"],
            "commercial": {"code": "FST01", "currency": "INR",
                           "base_price": 37000, "offers": []}}, order=2)

        # the historical divergence: normal newer than base
        divergent = _mk_k(OX, "Divergent Course", {
            "duration": "6 Months",
            "commercial": {"code": "DIV1", "currency": "INR",
                           "normal_total_fee": 19540, "base_price": 16000,
                           "offers": []}}, order=3)

        # normal_total_fee present, base_price absent entirely
        no_base = _mk_k(OX, "No Base Course", {
            "duration": "6 Months",
            "commercial": {"code": "NOBASE", "currency": "INR",
                           "normal_total_fee": 19540, "offers": []}}, order=4)

        rival = _mk_k(OTHER, "Rival Course", {
            "duration": "3 Months",
            "commercial": {"code": "RIV1", "currency": "INR",
                           "normal_total_fee": 5000, "base_price": 5000,
                           "offers": []}}, order=1)

        data = {"ox_admin": ox_admin.id, "rival_admin": rival_admin.id,
                "aligned": aligned.id, "fst": fst.id,
                "divergent": divergent.id, "no_base": no_base.id,
                "rival": rival.id}
    yield data
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ 1, 7 — CREATE ════════════════════════════════════════════════════════

class TestCreate:

    def test_create_writes_the_price_to_both_keys(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Brand New", code="BN1", base_price="12345"))
        assert r.status_code in (200, 302)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Brand New").one()
            c = json.loads(row.attributes)["commercial"]
        assert c["base_price"] == 12345
        assert c["normal_total_fee"] == 12345

    def test_create_with_no_price_writes_neither_key(self, seeded):
        """Existing blank-price semantics preserved: a course created with no
        price has no price, and dual-write must not invent one."""
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Free Course", code="FREE1", base_price=""))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Free Course").one()
            c = json.loads(row.attributes)["commercial"]
        assert "base_price" not in c
        assert "normal_total_fee" not in c

    def test_created_course_reads_back_one_price(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Readback", code="RB1", base_price="7777"))
        with _APP.app_context():
            rec = cat.get_course(OX, "RB1")
        assert rec is not None and rec.normal_total_fee == 7777


# ═══ 2, 3, 5, 6 — UPDATE across every stored shape ════════════════════════

class TestUpdateShapes:

    def test_aligned_row_updates_both(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA – Computer Applications", code="PGDCA",
                       base_price="20000"))
        c = _commercial(seeded["aligned"])
        assert c["base_price"] == 20000
        assert c["normal_total_fee"] == 20000

    def test_fst01_shape_gains_normal_total_fee(self, seeded):
        """The production FST01 shape: base_price only. An edit upgrades it
        to the canonical shape -- nothing is backfilled, it converges when
        someone edits it."""
        before = _commercial(seeded["fst"])
        assert "normal_total_fee" not in before and before["base_price"] == 37000
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['fst']}/edit",
            data=_form(title="full stack web development", code="FST01",
                       base_price="38000"))
        c = _commercial(seeded["fst"])
        assert c["base_price"] == 38000
        assert c["normal_total_fee"] == 38000

    def test_divergent_row_is_reconciled_to_the_submitted_price(self, seeded):
        """normal=19540, base=16000 -- the shape production actually carried.
        The admin's typed number wins; neither stored value is preferred."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['divergent']}/edit",
            data=_form(title="Divergent Course", code="DIV1",
                       base_price="20000"))
        c = _commercial(seeded["divergent"])
        assert c["normal_total_fee"] == 20000
        assert c["base_price"] == 20000
        assert 19540 not in (c["normal_total_fee"], c["base_price"])
        assert 16000 not in (c["normal_total_fee"], c["base_price"])

    def test_row_without_base_price_gains_one(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['no_base']}/edit",
            data=_form(title="No Base Course", code="NOBASE",
                       base_price="20000"))
        c = _commercial(seeded["no_base"])
        assert c["normal_total_fee"] == 20000
        assert c["base_price"] == 20000

    def test_the_value_is_the_submitted_one_not_a_stored_one(self, seeded):
        """Guards M2/M3: neither stored field may be the write source."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['divergent']}/edit",
            data=_form(title="Divergent Course", code="DIV1",
                       base_price="31337"))
        c = _commercial(seeded["divergent"])
        assert c["normal_total_fee"] == 31337 and c["base_price"] == 31337


# ═══ 4 — THE BLANK-PRICE SAFETY GUARD ═════════════════════════════════════

class TestBlankPriceGuard:

    def test_blank_price_preserves_normal_total_fee(self, seeded):
        """THE critical guard. A blank price pops base_price, as it always
        has, and must NOT pop normal_total_fee -- otherwise a blank form
        field silently deletes what the business charges."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA – Computer Applications", code="PGDCA",
                       base_price=""))
        c = _commercial(seeded["aligned"])
        assert "base_price" not in c, "existing pop-on-blank behaviour changed"
        assert c["normal_total_fee"] == 19540, \
            "blank price DELETED the customer-facing price"

    def test_the_customer_is_still_quoted_after_a_blank_submission(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA – Computer Applications", code="PGDCA",
                       base_price=""))
        with _APP.app_context():
            rec = cat.get_course(OX, "PGDCA")
        assert rec.normal_total_fee == 19540

    def test_blank_price_on_a_base_only_row_leaves_no_price(self, seeded):
        """FST01 has no normal_total_fee to protect, so a blank submission
        legitimately leaves it with no price at all."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['fst']}/edit",
            data=_form(title="full stack web development", code="FST01",
                       base_price=""))
        c = _commercial(seeded["fst"])
        assert "base_price" not in c and "normal_total_fee" not in c

    def test_merge_unit_contract_for_the_blank_branch(self, seeded):
        """Asserted directly on _merge_attributes, not only through HTTP."""
        existing = {"commercial": {"normal_total_fee": 19540,
                                   "base_price": 19540, "offers": []}}
        cleaned, errors = kas.validate_payload(_form(base_price=""))
        assert not errors
        merged = kas._merge_attributes(existing, cleaned)
        assert "base_price" not in merged["commercial"]
        assert merged["commercial"]["normal_total_fee"] == 19540


# ═══ 8 — components are never summed ══════════════════════════════════════

class TestNoDerivation:

    def test_price_is_not_derived_from_components(self, seeded):
        """4500 + 15040 == 19540, but the DECLARED price must win."""
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Inconsistent", code="INC1", base_price="1",
                       registration_fee="4500", net_tuition_fee="15040"))
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Inconsistent").one().attributes)
        assert a["commercial"]["base_price"] == 1
        assert a["commercial"]["normal_total_fee"] == 1
        assert 19540 not in (a["commercial"]["base_price"],
                             a["commercial"]["normal_total_fee"])

    def test_components_still_stored_independently(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Components", code="CMP1", base_price="19540",
                       registration_fee="4500", tuition_fee="18800",
                       concession="3760"))
        with _APP.app_context():
            a = json.loads(TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Components").one().attributes)
        by = {c["type"]: c["amount"] for c in a["regulatory"]["components"]}
        assert by == {"registration_fee": 4500, "max_tuition_fee": 18800,
                      "concession": 3760}

    def test_the_merge_source_reads_no_component_and_no_stored_price(self):
        """AST guard: the dual-write assignment may only take its value from
        cleaned["base_price"]."""
        src = open(KAS_PY, encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_merge_attributes")
        writes = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)):
                t = node.targets[0]
                key = getattr(t.slice, "value", None)
                if key == "normal_total_fee":
                    writes.append(ast.dump(node.value))
        assert len(writes) == 1, f"expected one normal_total_fee write, got {writes}"
        assert "'base_price'" in writes[0] and "cleaned" in writes[0], writes[0]
        assert "components" not in writes[0]


# ═══ 9, 10, 11, 12 — preservation ═════════════════════════════════════════

class TestPreservation:

    def test_unrelated_attributes_survive_an_edit(self, seeded):
        """AMENDED BY RC2.5.4c-x-5a. `keywords` and `categories` are no longer
        UNRELATED to the form -- that phase made them editable fields, so a
        submission that omits them now clears them by design (the pop-on-blank
        contract, pinned in test_course_keywords_categories_rc254cx5). They are
        therefore submitted here like any other form field, which keeps this
        test's actual subject intact: attributes with NO form field at all --
        historical_alias, official_name, eligibility, regulatory.source/as_of
        -- must survive an edit rather than being silently destroyed."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="Renamed", code="PGDCA", base_price="20000",
                       keywords="pgdca", categories=["job"]))
        a = _attrs(seeded["aligned"])
        assert a["historical_alias"] == "Computer Teacher Training"
        assert a["official_name"].startswith("Post Graduate")
        assert a["categories"] == ["job"]
        assert a["keywords"] == ["pgdca"]
        assert a["eligibility"] == "Any degree"
        assert a["regulatory"]["source"] == "Kerala State Rutronix fee card"
        assert a["regulatory"]["as_of"] == "2026"

    def test_offers_and_emi_survive_an_edit(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000"))
        c = _commercial(seeded["aligned"])
        assert c["offers"][0]["label"] == "Diwali"
        assert c["offers"][0]["final_price"] == 17999
        assert c["emi_available"] is True

    def test_legacy_payment_url_remains_non_writable(self, seeded):
        form = _form(title="PGDCA", code="PGDCA", base_price="20000")
        form["legacy_payment_url"] = "https://evil.example.com/x"
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit", data=form)
        a = _attrs(seeded["aligned"])
        assert a["commercial"]["legacy_payment_url"] == LEGACY_URL
        assert "evil.example.com" not in json.dumps(a)

    def test_payment_url_behaviour_is_unchanged(self, seeded):
        cl = client(seeded["ox_admin"])
        cl.post(f"/tenant/courses/{seeded['aligned']}/edit",
                data=_form(title="PGDCA", code="PGDCA", base_price="20000",
                           payment_url=ACTIVE_URL))
        assert _commercial(seeded["aligned"])["payment_url"] == ACTIVE_URL
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "PGDCA") == ACTIVE_URL
        # blanking still clears the ACTIVE link, exactly as before
        cl.post(f"/tenant/courses/{seeded['aligned']}/edit",
                data=_form(title="PGDCA", code="PGDCA", base_price="20000",
                           payment_url=""))
        assert _commercial(seeded["aligned"])["payment_url"] is None
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "PGDCA") is None

    def test_a_dual_written_price_does_not_create_a_payment_url(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['fst']}/edit",
            data=_form(title="full stack web development", code="FST01",
                       base_price="38000"))
        assert _commercial(seeded["fst"])["normal_total_fee"] == 38000
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "FST01") is None

    def test_update_does_not_replace_the_whole_attributes_object(self, seeded):
        """AMENDED BY RC2.5.4c-x-5a for the same reason as
        test_unrelated_attributes_survive_an_edit: keywords and categories are
        now form-managed, so they are submitted rather than omitted. The
        subject -- that an update MERGES into the stored attributes instead of
        replacing them wholesale -- is unchanged and still asserted over every
        top-level key."""
        before = set(_attrs(seeded["aligned"]).keys())
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000",
                       keywords="pgdca", categories=["job"]))
        after = set(_attrs(seeded["aligned"]).keys())
        assert before <= after, f"top-level keys lost: {before - after}"


# ═══ 13, 14, 15 — read and render contracts unchanged ═════════════════════

class TestReadAndRenderContracts:

    def test_base_price_still_excluded_from_ai_rendering(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000"))
        with _APP.app_context():
            block = ks.render_knowledge_block(OX, query="PGDCA fee")
        assert "base_price" not in block
        assert "commercial.normal_total_fee: 20000" in block
        assert ks._NON_RENDERABLE_KEYS == frozenset(
            {"legacy_payment_url", "payment_url", "base_price"})

    def test_catalogue_precedence_is_unchanged(self, seeded):
        """The RC2.5.4c-x fallback must NOT be removed: it still serves a row
        written before this phase."""
        with _APP.app_context():
            assert cat.get_course(OX, "FST01").normal_total_fee == 37000
            assert cat.get_course(OX, "DIV1").normal_total_fee == 19540

    def test_only_one_canonical_price_is_exposed_after_dual_write(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['divergent']}/edit",
            data=_form(title="Divergent Course", code="DIV1",
                       base_price="20000"))
        with _APP.app_context():
            rec = cat.get_course(OX, "DIV1")
            block = ks.render_knowledge_block(OX, query="Divergent Course")
            idx, _ = cat.catalogue_index_with_provenance(OX)
        c = _commercial(seeded["divergent"])
        assert c["base_price"] == c["normal_total_fee"] == rec.normal_total_fee == 20000
        # Scoped to THIS course's rendered line. The block spans several
        # courses, and a sibling row legitimately carries its own 19540 --
        # asserting on the whole block would be testing the wrong thing.
        div_line = next(ln for ln in block.splitlines()
                        if "commercial.code: DIV1" in ln)
        assert "commercial.normal_total_fee: 20000" in div_line
        assert "base_price" not in div_line
        assert "16000" not in div_line and "19540" not in div_line
        line = next(e for e in idx if e.startswith("DIV1"))
        assert "₹20,000" in line


# ═══ 16 — tenant isolation ════════════════════════════════════════════════

class TestTenantIsolation:

    def test_a_tenant_cannot_edit_another_tenants_course(self, seeded):
        r = client(seeded["rival_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="Hijacked", code="PGDCA", base_price="1"))
        assert r.status_code == 404
        c = _commercial(seeded["aligned"])
        assert c["normal_total_fee"] == 19540 and c["base_price"] == 19540

    def test_a_form_supplied_tenant_id_is_not_authority_on_create(self, seeded):
        form = _form(title="Injected", code="INJ1", base_price="999")
        form["tenant_id"] = OTHER
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=form)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Injected").one()
        assert row.tenant_id == OX, "form tenant_id was treated as authority"

    def test_dual_write_does_not_touch_another_tenants_row(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['aligned']}/edit",
            data=_form(title="PGDCA", code="PGDCA", base_price="20000"))
        c = _commercial(seeded["rival"])
        assert c["normal_total_fee"] == 5000 and c["base_price"] == 5000

    def test_create_uses_the_session_tenant(self, seeded):
        client(seeded["rival_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Rival New", code="RN1", base_price="4242"))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Rival New").one()
            c = json.loads(row.attributes)["commercial"]
            assert row.tenant_id == OTHER
            assert cat.get_course(OX, "RN1") is None
        assert c["normal_total_fee"] == 4242 and c["base_price"] == 4242
