"""Phase RC2.5.4b — tenant Courses & Knowledge CRUD.

WHAT THIS PHASE ADDS
---------------------
RC2.5.4a gave tenants a READ-ONLY view of their knowledge. RC2.5.4b adds
create / edit / activate-deactivate, and nothing else. There is deliberately
NO hard-delete anywhere: deactivation is the only removal, and it is
reversible -- an inactive row stays visible to its owner and stops reaching
the AI.

THE TWO WRITE-SIDE RULES THAT MATTER MOST
------------------------------------------
1. tenant_id is ALWAYS session-derived. Nothing in request.form, request.args,
   a hidden field, or the URL path can influence which tenant a row belongs
   to. Mutations resolve the row by (id AND tenant_id) together, so another
   tenant's row id yields an ordinary 404 that does not confirm existence.

2. legacy_payment_url is NEVER writable. RC2.5.3a-K keeps it out of the
   PROMPT; this phase keeps it out of the WRITE path, so a crafted form post
   can neither introduce nor alter one. Updates MERGE into stored attributes
   rather than replacing them, so offers/regulatory/historical_alias written
   by earlier phases survive an edit.

PRICING: base_price, registration fee, tuition fee, concession and exam fee
are separate declared facts. Nothing is derived from anything else.

OUT OF SCOPE, NOT TOUCHED: knowledge_service, prompt_composer, ai_service,
models, migrations, COURSE_PAYMENT_LINKS, bot/*, payment infrastructure,
CRM templates. No production data written.
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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc254b_courses_crud.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254b-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc254b-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc254b-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge, User                    # noqa: E402
from app.services import knowledge_admin_service as kas                 # noqa: E402
from app.services import knowledge_service as ks                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KAS_PY = os.path.join(ROOT, "app", "services", "knowledge_admin_service.py")
TENANT_PY = os.path.join(ROOT, "app", "routes", "tenant.py")

OX = "t-ox"
OTHER = "t-rival"
LEGACY_URL = "https://rzp.io/rzp/KAQ2C7t"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _mk_user(tenant, username, role="STAFF"):
    u = User(username=username, email=f"{username}.{tenant}@x.test",
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _mk_k(tenant, title, kind="course", body=None, attrs=None,
          active=True, order=0):
    row = TenantKnowledge(
        tenant_id=tenant, kind=kind, title=title, body=body,
        attributes=json.dumps(attrs if attrs is not None else {}),
        is_active=active, sort_order=order)
    db.session.add(row)
    db.session.commit()
    return row


def _form(**overrides):
    """A valid baseline submission; override individual fields per test."""
    base = {"title": "New Course", "kind": "course", "body": "Body text.",
            "sort_order": "5", "duration": "6 Months", "currency": "INR",
            "base_price": "1000", "payment_url": "",
            "registration_fee": "", "tuition_fee": "", "concession": "",
            "net_tuition_fee": "", "exam_fee": ""}
    base.update(overrides)
    return base


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

        ox_admin = _mk_user(OX, "admin_ox", role="ADMIN")
        ox_staff = _mk_user(OX, "staff_ox", role="STAFF")
        rival_admin = _mk_user(OTHER, "admin_rival", role="ADMIN")

        # A row carrying EVERY shape an earlier phase may have written --
        # used to prove an edit preserves what the form has no field for.
        rich = _mk_k(OX, "PGDCA", "course", "Original body.", {
            "duration": "12 Months",
            "historical_alias": "Computer Teacher Training",
            "commercial": {
                "currency": "INR", "base_price": 19540, "payment_url": None,
                "legacy_payment_url": LEGACY_URL,
                "offers": [{"label": "Diwali", "final_price": 17000}]},
            "regulatory": {
                "source": "Kerala State Rutronix fee card", "as_of": "2026",
                "components": [
                    {"type": "registration_fee", "label": "Registration Fee",
                     "amount": 4500},
                    {"type": "net_tuition_fee",
                     "label": "Net Tuition Fee to ATC", "amount": 15040}]}},
            order=1)

        plain = _mk_k(OX, "Simple FAQ", "faq", "Some answer.", order=2)
        inactive = _mk_k(OX, "Retired", "course", "Old.", active=False, order=3)
        rival_row = _mk_k(OTHER, "Rival Secret", "course", "Rival body.",
                          {"commercial": {"base_price": 999999}}, order=1)

        ids = {"ox_admin": ox_admin.id, "ox_staff": ox_staff.id,
               "rival_admin": rival_admin.id, "rich": rich.id,
               "plain": plain.id, "inactive": inactive.id,
               "rival_row": rival_row.id}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ Create ═══════════════════════════════════════════════════════════════

class TestCreate:

    def test_admin_can_create_valid_course(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(title="Brand New Course"),
            follow_redirects=False)
        assert r.status_code in (301, 302)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="Brand New Course").first()
        assert row is not None
        assert row.is_active is True

    def test_created_row_belongs_to_the_current_tenant(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(title="Ownership Check"))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Ownership Check").first()
        assert row.tenant_id == OX

    def test_tenant_id_in_form_is_ignored(self, seeded):
        """A crafted post naming another tenant must not place the row there."""
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Injected", tenant_id=OTHER))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Injected").first()
        assert row.tenant_id == OX

    def test_created_row_stores_declared_pricing_only(self, seeded):
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=_form(
            title="Pricing Check", base_price="19540",
            registration_fee="4500", net_tuition_fee="15040"))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Pricing Check").first()
            attrs = json.loads(row.attributes)
        assert attrs["commercial"]["base_price"] == 19540
        amounts = {c["type"]: c["amount"]
                   for c in attrs["regulatory"]["components"]}
        assert amounts["registration_fee"] == 4500
        assert amounts["net_tuition_fee"] == 15040
        # 4500 + 15040 == 19540, but base_price must be the DECLARED value,
        # never a computed one -- proven by declaring a deliberately
        # inconsistent price below.

    def test_base_price_is_never_derived_from_components(self, seeded):
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=_form(
            title="Inconsistent", base_price="1",
            registration_fee="4500", net_tuition_fee="15040"))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Inconsistent").first()
            attrs = json.loads(row.attributes)
        assert attrs["commercial"]["base_price"] == 1  # stored as declared

    def test_created_active_row_is_reachable_by_ai_retrieval(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Zebra Quantum Course", body="zebraquantum topic"))
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="zebraquantum")
        assert any(r.title == "Zebra Quantum Course" for r in rows)


# ═══ Validation ═══════════════════════════════════════════════════════════

class TestValidation:

    def test_blank_title_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(title="   "))
        assert r.status_code == 400
        with _APP.app_context():
            assert TenantKnowledge.query.filter_by(tenant_id=OX).count() == 3

    def test_invalid_kind_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(kind="malicious"))
        assert r.status_code == 400

    def test_negative_price_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(base_price="-100"))
        assert r.status_code == 400

    def test_negative_fee_component_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(registration_fee="-1"))
        assert r.status_code == 400

    def test_non_numeric_price_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(base_price="free"))
        assert r.status_code == 400

    def test_negative_sort_order_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(sort_order="-3"))
        assert r.status_code == 400

    def test_non_numeric_sort_order_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(sort_order="first"))
        assert r.status_code == 400

    def test_overlong_title_rejected(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(title="x" * 300))
        assert r.status_code == 400

    @pytest.mark.parametrize("bad_url", [
        "javascript:alert(1)", "data:text/html,<script>", "file:///etc/passwd",
        "ftp://example.com/x", "not a url", "//evil.example.com",
    ])
    def test_unsafe_or_malformed_payment_url_rejected(self, seeded, bad_url):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create", data=_form(payment_url=bad_url))
        assert r.status_code == 400, bad_url

    def test_valid_https_payment_url_accepted(self, seeded):
        r = client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="With Link",
                       payment_url="https://pay.example.com/abc"))
        assert r.status_code in (301, 302)
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="With Link").first()
            attrs = json.loads(row.attributes)
        assert attrs["commercial"]["payment_url"] == "https://pay.example.com/abc"

    def test_validation_is_server_side_not_html_only(self):
        """The service validates independently of any browser."""
        cleaned, errors = kas.validate_payload({"title": "", "kind": "nope"})
        assert errors
        assert any("Title" in e for e in errors)
        assert any("Type" in e for e in errors)

    def test_malformed_stored_attributes_do_not_break_editing(self, seeded):
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX).first()
            row.attributes = "{not valid json"
            db.session.commit()
            rid = row.id
        r = client(seeded["ox_admin"]).post(
            f"/tenant/courses/{rid}/edit", data=_form(title="Recovered"))
        assert r.status_code in (301, 302)
        with _APP.app_context():
            fixed = TenantKnowledge.query.get(rid)
            assert fixed.title == "Recovered"
            json.loads(fixed.attributes)  # must now be valid JSON


# ═══ Edit ═════════════════════════════════════════════════════════════════

class TestEdit:

    def test_edit_updates_basic_fields(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/edit",
            data=_form(title="Edited Title", kind="policy", body="New body.",
                       sort_order="9"))
        with _APP.app_context():
            row = TenantKnowledge.query.get(seeded["plain"])
        assert row.title == "Edited Title"
        assert row.kind == "policy"
        assert row.body == "New body."
        assert row.sort_order == 9

    def test_edit_preserves_unrelated_attributes(self, seeded):
        """offers, historical_alias and regulatory.source have no form field
        -- they must survive an edit rather than being silently destroyed."""
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA Renamed", base_price="20000"))
        with _APP.app_context():
            attrs = json.loads(TenantKnowledge.query.get(seeded["rich"]).attributes)
        assert attrs["historical_alias"] == "Computer Teacher Training"
        assert attrs["commercial"]["offers"][0]["label"] == "Diwali"
        assert attrs["regulatory"]["source"] == "Kerala State Rutronix fee card"
        assert attrs["commercial"]["base_price"] == 20000  # the edited value

    def test_edit_does_not_change_ownership_or_active_state(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit", data=_form(title="Kept"))
        with _APP.app_context():
            row = TenantKnowledge.query.get(seeded["rich"])
        assert row.tenant_id == OX
        assert row.is_active is True

    def test_edit_form_prefills_stored_values(self, seeded):
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}/edit")
        body = r.get_data(as_text=True)
        assert "PGDCA" in body
        assert "19540" in body
        assert "4500" in body      # registration fee prefilled


# ═══ legacy_payment_url is never writable ════════════════════════════════

class TestLegacyPaymentUrlNotWritable:

    def test_cannot_introduce_legacy_url_via_form(self, seeded):
        client(seeded["ox_admin"]).post(
            "/tenant/courses/create",
            data=_form(title="Sneaky",
                       legacy_payment_url="https://evil.example.com/x"))
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(title="Sneaky").first()
            attrs = json.loads(row.attributes)
        assert "legacy_payment_url" not in attrs
        assert "legacy_payment_url" not in attrs.get("commercial", {})

    def test_cannot_overwrite_existing_legacy_url_via_form(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA",
                       legacy_payment_url="https://evil.example.com/x"))
        with _APP.app_context():
            attrs = json.loads(TenantKnowledge.query.get(seeded["rich"]).attributes)
        assert attrs["commercial"]["legacy_payment_url"] == LEGACY_URL

    def test_existing_legacy_url_survives_an_ordinary_edit(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit",
            data=_form(title="PGDCA v2", base_price="21000"))
        with _APP.app_context():
            attrs = json.loads(TenantKnowledge.query.get(seeded["rich"]).attributes)
        assert attrs["commercial"]["legacy_payment_url"] == LEGACY_URL

    def test_legacy_url_still_excluded_from_the_composed_prompt(self, seeded):
        """Editing must not have weakened the RC2.5.3a-K prompt exclusion."""
        from app.services import prompt_composer
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit", data=_form(title="PGDCA"))
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        assert LEGACY_URL not in out
        assert "legacy_payment_url" not in out

    def test_validate_payload_never_emits_legacy_key(self):
        cleaned, errors = kas.validate_payload(
            _form(legacy_payment_url="https://evil.example.com"))
        assert "legacy_payment_url" not in cleaned

    def test_merge_strips_a_legacy_key_even_if_one_reaches_it(self):
        """Defence-in-depth contract, tested directly at the unit level.

        Through the ROUTES, two other mechanisms already prevent this:
        validate_payload() never emits the key, and dict(existing) preserves
        whatever was stored. So _NON_WRITABLE_ATTR_KEYS is a third,
        independent layer -- and an untested layer is a decorative one.
        This asserts its actual documented contract: if a `cleaned` dict ever
        did carry the key (a future refactor passing raw form data through,
        say), _merge_attributes must still refuse to write it.
        """
        existing = {"commercial": {"legacy_payment_url": LEGACY_URL,
                                   "base_price": 19540}}
        hostile = dict(kas.validate_payload(_form())[0])
        hostile["legacy_payment_url"] = "https://evil.example.com/x"

        merged = kas._merge_attributes(existing, hostile)

        # The stored value survives untouched...
        assert merged["commercial"]["legacy_payment_url"] == LEGACY_URL
        # ...and the injected one is nowhere, at any level.
        assert "evil.example.com" not in json.dumps(merged)
        assert merged.get("legacy_payment_url") is None

    def test_merge_does_not_invent_a_legacy_key_when_none_was_stored(self):
        """The strip must not create the key on rows that never had one."""
        hostile = dict(kas.validate_payload(_form())[0])
        hostile["legacy_payment_url"] = "https://evil.example.com/x"
        merged = kas._merge_attributes({"commercial": {"base_price": 10}},
                                       hostile)
        assert "legacy_payment_url" not in merged.get("commercial", {})
        assert "legacy_payment_url" not in merged


# ═══ Activate / deactivate — soft only ═══════════════════════════════════

class TestActivation:

    def test_deactivate_sets_is_active_false(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/toggle", data={"active": "0"})
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["plain"]).is_active is False

    def test_reactivate_sets_is_active_true(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['inactive']}/toggle", data={"active": "1"})
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["inactive"]).is_active is True

    def test_deactivation_never_deletes_the_row(self, seeded):
        with _APP.app_context():
            before = TenantKnowledge.query.count()
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/toggle", data={"active": "0"})
        with _APP.app_context():
            assert TenantKnowledge.query.count() == before
            assert TenantKnowledge.query.get(seeded["plain"]) is not None

    def test_deactivated_row_excluded_from_ai_retrieval(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/toggle", data={"active": "0"})
        with _APP.app_context():
            titles = [r.title for r in ks.fetch_knowledge(OX, limit=ks.MAX_ITEMS)]
        assert "Simple FAQ" not in titles

    def test_deactivated_row_still_visible_in_admin(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/toggle", data={"active": "0"})
        with _APP.app_context():
            titles = [r.title for r in
                      kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)["rows"]]
        assert "Simple FAQ" in titles

    def test_reactivated_row_returns_to_ai_retrieval(self, seeded):
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['inactive']}/toggle", data={"active": "1"})
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="Retired")
        assert any(r.title == "Retired" for r in rows)

    def test_no_hard_delete_route_exists(self):
        """There must be no route that physically removes a knowledge row."""
        tree = ast.parse(open(TENANT_PY, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef):
                for d in n.decorator_list:
                    src = ast.unparse(d)
                    if "tenant_bp.route" in src and "courses" in src:
                        assert "DELETE" not in src.upper() or "'DELETE'" not in src

    def test_service_contains_no_hard_delete(self):
        src = open(KAS_PY, encoding="utf-8").read()
        assert "db.session.delete" not in src
        assert ".delete()" not in src


# ═══ Tenant isolation on the WRITE path ══════════════════════════════════

class TestWriteIsolation:

    def test_tenant_b_cannot_edit_tenant_a_row(self, seeded):
        r = client(seeded["rival_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/edit", data=_form(title="Hijacked"))
        assert r.status_code == 404
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["rich"]).title == "PGDCA"

    def test_tenant_b_cannot_toggle_tenant_a_row(self, seeded):
        r = client(seeded["rival_admin"]).post(
            f"/tenant/courses/{seeded['rich']}/toggle", data={"active": "0"})
        assert r.status_code == 404
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["rich"]).is_active is True

    def test_cross_tenant_edit_does_not_reveal_existence(self, seeded):
        """A real-but-foreign id and a nonexistent id must be indistinguishable."""
        c = client(seeded["rival_admin"])
        real_foreign = c.post(f"/tenant/courses/{seeded['rich']}/edit",
                              data=_form())
        nonexistent = c.post("/tenant/courses/99999999/edit", data=_form())
        assert real_foreign.status_code == nonexistent.status_code == 404

    def test_get_edit_form_for_foreign_row_is_404_not_a_crash(self, seeded):
        """The GET edit form needs its OWN ownership check.

        The POST path is protected twice over (the route's abort(404) and
        update_knowledge() returning "not found"), so a missing guard there
        still fails safe. GET has only the route's check -- without it, a
        foreign row id renders from a None row and 500s, which both crashes
        and distinguishes a real id from a fake one.
        """
        c = client(seeded["rival_admin"])
        real_foreign = c.get(f"/tenant/courses/{seeded['rich']}/edit")
        nonexistent = c.get("/tenant/courses/99999999/edit")
        assert real_foreign.status_code == 404
        assert nonexistent.status_code == 404

    def test_get_edit_form_for_inactive_own_row_still_works(self, seeded):
        """Guarding GET must not accidentally block a legitimate own row."""
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['inactive']}/edit")
        assert r.status_code == 200

    def test_service_level_update_refuses_foreign_row(self, seeded):
        with _APP.app_context():
            row, errors = kas.update_knowledge(OTHER, seeded["rich"], _form())
        assert row is None and errors is None   # "not found", not "invalid"

    def test_service_level_toggle_refuses_foreign_row(self, seeded):
        with _APP.app_context():
            row, errors = kas.set_active(OTHER, seeded["rich"], False)
        assert row is None and errors is None

    def test_falsy_tenant_id_cannot_mutate(self, seeded):
        with _APP.app_context():
            for bad in (None, "", 0, False):
                row, errors = kas.create_knowledge(bad, _form())
                assert row is None and errors
                row, errors = kas.update_knowledge(bad, seeded["rich"], _form())
                assert row is None
                row, errors = kas.set_active(bad, seeded["rich"], False)
                assert row is None

    def test_rival_row_untouched_throughout(self, seeded):
        client(seeded["ox_admin"]).post("/tenant/courses/create", data=_form())
        client(seeded["ox_admin"]).post(
            f"/tenant/courses/{seeded['plain']}/edit", data=_form(title="X"))
        with _APP.app_context():
            rival = TenantKnowledge.query.get(seeded["rival_row"])
        assert rival.title == "Rival Secret"
        assert rival.tenant_id == OTHER


# ═══ Authorization ════════════════════════════════════════════════════════

class TestAuthorization:

    def test_staff_cannot_create(self, seeded):
        r = client(seeded["ox_staff"]).post("/tenant/courses/create",
                                            data=_form(title="Staff Attempt"))
        assert r.status_code == 403
        with _APP.app_context():
            assert TenantKnowledge.query.filter_by(
                title="Staff Attempt").first() is None

    def test_staff_cannot_edit(self, seeded):
        r = client(seeded["ox_staff"]).post(
            f"/tenant/courses/{seeded['plain']}/edit", data=_form(title="Nope"))
        assert r.status_code == 403
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["plain"]).title == "Simple FAQ"

    def test_staff_cannot_toggle(self, seeded):
        r = client(seeded["ox_staff"]).post(
            f"/tenant/courses/{seeded['plain']}/toggle", data={"active": "0"})
        assert r.status_code == 403

    def test_staff_cannot_reach_the_create_form(self, seeded):
        assert client(seeded["ox_staff"]).get(
            "/tenant/courses/new").status_code == 403

    def test_unauthenticated_cannot_mutate(self, seeded):
        c = _APP.test_client()
        for path, data in (("/tenant/courses/create", _form()),
                           (f"/tenant/courses/{seeded['plain']}/edit", _form()),
                           (f"/tenant/courses/{seeded['plain']}/toggle",
                            {"active": "0"})):
            r = c.post(path, data=data, follow_redirects=False)
            assert r.status_code in (301, 302)
            assert "login" in r.headers.get("Location", "").lower()
        with _APP.app_context():
            assert TenantKnowledge.query.get(seeded["plain"]).title == "Simple FAQ"

    def test_all_mutation_routes_carry_the_standard_decorators(self):
        tree = ast.parse(open(TENANT_PY, encoding="utf-8").read())
        for fn_name in ("tenant_course_new", "tenant_course_create",
                        "tenant_course_edit", "tenant_course_toggle"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            decs = {ast.unparse(d) for d in fn.decorator_list}
            assert "login_required" in decs, fn_name
            assert "tenant_admin_required" in decs, fn_name

    def test_billing_guard_still_gates_mutations(self, seeded):
        with _APP.app_context():
            t = Tenant.query.get(OX)
            t.status, t.billing_exempt = "SUSPENDED", False
            db.session.commit()
        try:
            r = client(seeded["ox_admin"]).post(
                "/tenant/courses/create", data=_form(title="During Suspension"),
                follow_redirects=False)
            assert r.status_code in (301, 302)
            assert "billing" in r.headers.get("Location", "").lower()
            with _APP.app_context():
                assert TenantKnowledge.query.filter_by(
                    title="During Suspension").first() is None
        finally:
            with _APP.app_context():
                t = Tenant.query.get(OX)
                t.status, t.billing_exempt = "ACTIVE", True
                db.session.commit()


# ═══ AI path unchanged ════════════════════════════════════════════════════

class TestAIPathUnchanged:

    def test_max_items_unchanged(self):
        assert ks.MAX_ITEMS == 8

    def test_max_chars_unchanged(self):
        assert ks.MAX_CHARS == 2000

    def test_knowledge_service_file_untouched(self):
        import subprocess
        for path in ("app/services/knowledge_service.py",
                     "app/services/prompt_composer.py",
                     "app/services/ai_service.py"):
            out = subprocess.run(["git", "status", "--porcelain", "--", path],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            assert out.strip() == "", f"{path} unexpectedly changed"

    def test_admin_service_still_does_not_import_knowledge_service(self):
        tree = ast.parse(open(KAS_PY, encoding="utf-8").read())
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names} | {
                   n.module for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("knowledge_service" in m for m in imported)


# ═══ Scope ════════════════════════════════════════════════════════════════

class TestScope:

    def test_forbidden_files_untouched(self):
        import subprocess
        for path in ("app/models.py", "migrations/", "app/bot/constants.py",
                     "app/bot/prompts.py", "app/bot/router.py",
                     "app/services/whatsapp_service.py",
                     "app/routes/webhook.py", "app/routes/admin.py"):
            out = subprocess.run(["git", "status", "--porcelain", "--", path],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            assert out.strip() == "", f"{path} unexpectedly changed"

    def test_payment_links_untouched(self):
        from app.bot.constants import COURSE_PAYMENT_LINKS
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == LEGACY_URL
        assert len(COURSE_PAYMENT_LINKS) == 4

    def test_no_payment_provider_integration_added(self):
        src = open(KAS_PY, encoding="utf-8").read().lower()
        for term in ("razorpay", "stripe", "checkout.session", "payment_intent"):
            assert term not in src

    def test_routes_do_not_mutate_the_database_directly(self):
        """All DB mutation belongs in the service, not in route bodies."""
        tree = ast.parse(open(TENANT_PY, encoding="utf-8").read())
        for fn_name in ("tenant_course_create", "tenant_course_edit",
                        "tenant_course_toggle"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            src = ast.unparse(fn)
            assert "db.session.add" not in src, fn_name
            assert "db.session.commit" not in src, fn_name
            assert "db.session.delete" not in src, fn_name

    def test_templates_keep_the_dark_design_system(self):
        src = open(os.path.join(ROOT, "templates", "tenant",
                                "course_form.html"), encoding="utf-8").read()
        assert "t-card" in src and "t-field" in src
        assert "tenant/sidebar.html" in src
        assert "var(--" in src
        for lib in ("chart.js", "apexcharts", "d3.js", "plotly"):
            assert lib not in src.lower()
