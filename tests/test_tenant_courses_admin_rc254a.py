"""Phase RC2.5.4a — read-only tenant Courses & Knowledge admin surface.

THE GAP
-------
tenant_knowledge had no admin surface at all: a tenant could not see what
their own AI assistant knows. The obvious implementation -- reusing
knowledge_service.fetch_knowledge() -- is a trap:

  * it is hard-capped at MAX_ITEMS=8 (a REAL ceiling since RC2.5.3b), so
    Oxford's 18 rows would silently render as 8, the exact class of silent
    truncation RC2.5.3b existed to fix;
  * it filters is_active=True, so an inactive row is structurally
    unreachable -- and an admin must see one in order to manage it.

WHAT THIS PHASE DOES
---------------------
knowledge_admin_service: an independent, read-only, tenant-scoped listing
path with its OWN pagination bound (DEFAULT_PER_PAGE/MAX_PER_PAGE), which
returns inactive rows and never imports MAX_ITEMS. Plus two read-only routes
(/tenant/courses, /tenant/courses/<id>) on the existing tenant blueprint, so
they inherit login_required + tenant_admin_required + the billing guard
unchanged.

ISOLATION: routes resolve tenant_id from _get_current_tenant() (the
authenticated session), never from query/form/path. get_knowledge() looks up
by (id AND tenant_id) together, so another tenant's row id 404s rather than
confirming the row exists elsewhere.

OUT OF SCOPE, NOT TOUCHED: knowledge_service, prompt_composer, ai_service,
models, migrations, COURSE_PAYMENT_LINKS, bot/*, CRM templates. No writes of
any kind -- no create/edit/delete/pricing/offer/payment mutation.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc254a_courses_admin.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254a-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc254a-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc254a-fake-gemini-key")
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
KS_PY = os.path.join(ROOT, "app", "services", "knowledge_service.py")

OX = "t-ox"
OTHER = "t-rival"

# RC2.5.4c-x-1a: the one construct that phase is authorised to change in
# app/services/knowledge_service.py. Everything else in the file stays pinned
# byte-identical to HEAD -- see
# test_knowledge_service_changed_only_where_rc254cx1a_authorised.
_KS_AUTHORISED_CONSTRUCTS = {"_NON_RENDERABLE_KEYS"}


def _assert_knowledge_service_only_authorised_change(root):
    """knowledge_service.py may differ from HEAD ONLY in the constructs named
    in _KS_AUTHORISED_CONSTRUCTS. Stronger than the porcelain zero-diff pin it
    replaces: it also catches a construct being added or removed, and it does
    not care whether the change is committed yet.
    """
    import subprocess

    rel = "app/services/knowledge_service.py"
    # NOT text=True: on Windows that decodes git's stdout with the locale
    # codepage and mangles the file's non-ASCII characters, so the guard would
    # fire on an encoding artifact rather than a real change. Line endings are
    # normalised for the same reason on a CRLF checkout.
    head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root,
                          capture_output=True)
    assert head.returncode == 0, f"cannot read HEAD:{rel}"
    old_src = head.stdout.decode("utf-8").replace("\r\n", "\n")
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as fh:
        new_src = fh.read().replace("\r\n", "\n")

    def segments(src):
        """Top-level constructs by name, plus everything else as one blob so a
        moved or deleted module-level statement is still caught."""
        named, other = {}, []
        for node in ast.parse(src).body:
            seg = ast.get_source_segment(src, node) or ""
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                named[node.name] = seg
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)):
                named[node.targets[0].id] = seg
            else:
                other.append(seg)
        return named, "\n".join(other)

    old_named, old_other = segments(old_src)
    new_named, new_other = segments(new_src)

    assert old_other == new_other, f"module-level code in {rel} changed"
    assert set(old_named) == set(new_named), (
        f"top-level constructs added or removed in {rel}: "
        f"{set(old_named) ^ set(new_named)}")
    for name, old_seg in old_named.items():
        if name in _KS_AUTHORISED_CONSTRUCTS:
            continue
        assert new_named[name] == old_seg, (
            f"{rel}::{name} changed, but only "
            f"{sorted(_KS_AUTHORISED_CONSTRUCTS)} is authorised")


LF = chr(10)

# RC2.5.4c-x-6b1: the ONLY constructs that phase may change in each file.
# constants.py -- two marketing pools that carried an unconditional EMI claim.
# ai_service.py -- smart_fallback, whose fee branch carried the same claim.
_X6B1_AUTHORISED = {
    "app/bot/constants.py": {"TRUST_LINES", "FEES_VALUE_LINES"},
    "app/services/ai_service.py": {"smart_fallback"},
}


def _assert_only_x6b1_constructs_changed(root, rel):
    """`rel` may differ from HEAD ONLY in its authorised constructs.

    Stronger than the working-tree pin it replaces: it also catches a
    construct being added or removed, and it proves every other construct --
    every payment/price constant, every other AI helper -- is byte-identical.
    Once b1 is committed the diff is empty and every comparison is trivially
    equal.
    """
    import subprocess

    authorised = _X6B1_AUTHORISED[rel]
    # NOT text=True: on Windows that decodes git's stdout with the locale
    # codepage and mangles Malayalam and emoji, firing the guard on an
    # encoding artifact. splitlines() normalises line endings.
    head = subprocess.run(["git", "show", "HEAD:" + rel], cwd=root,
                          capture_output=True)
    assert head.returncode == 0, "cannot read HEAD:" + rel
    old_src = LF.join(head.stdout.decode("utf-8").splitlines())
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as fh:
        new_src = LF.join(fh.read().splitlines())

    def segments(src):
        named, other = {}, []
        for node in ast.parse(src).body:
            seg = ast.get_source_segment(src, node) or ""
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                named[node.name] = seg
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)):
                named[node.targets[0].id] = seg
            else:
                other.append(seg)
        return named, LF.join(other)

    old_named, old_other = segments(old_src)
    new_named, new_other = segments(new_src)

    assert old_other == new_other, "module-level code in " + rel + " changed"
    assert set(old_named) == set(new_named), (
        "top-level constructs added or removed in " + rel + ": "
        + str(set(old_named) ^ set(new_named)))
    for name, old_seg in old_named.items():
        if name in authorised:
            continue
        assert new_named[name] == old_seg, (
            rel + "::" + name + " changed, but only "
            + str(sorted(authorised)) + " is authorised")

    # The permitted change must be a REMOVAL of an EMI-affirming line.
    for name in sorted(authorised):
        # Comment lines are skipped: this phase's own rationale comment
        # necessarily says "EMI", and a comment is not a customer claim.
        new_l = [x for x in new_named[name].splitlines()
                 if "emi" in x.lower()
                 and not x.strip().startswith("#")
                 and "not available" not in x.lower()]
        assert not new_l, rel + "::" + name + " still affirms EMI: " + str(new_l)


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
        is_active=active, sort_order=order,
    )
    db.session.add(row)
    db.session.commit()
    return row


# Mirrors the real Oxford production shape closely enough to be meaningful:
# 18 rows total (13 course + 5 standalone), which is exactly the count that
# exposed the MAX_ITEMS=8 truncation gap in production.
_COURSE_TITLES = [
    "Post Graduate Diploma in Computer Applications (PGDCA)",
    "Diploma in Computer Teachers Training Course (CTTC)",
    "Diploma in Computer Application (DCA - Regular)",
    "Diploma in Computer Application (DCA - Fast track)",
    "Certificate in Word Processing and Data Entry Operator (CWPDE)",
    "Diploma in Office Automation (DOA)",
    "Professional Diploma in Desktop Publishing (PDDTP)",
    "Professional Diploma in Web Designing (PDWD)",
    "Diploma in AI Driven Digital Marketing (AIDM)",
    "Certificate in Python",
    "Certificate in Java Programming",
    "Certificate in Website Development Using Python (Django)",
    "Certificate in Wordpress Master",
]
_STANDALONE = [
    ("policy", "Accreditation"),
    ("policy", "PSC eligibility"),
    ("faq", "NORKA Attestation"),
    ("faq", "Learning modes"),
    ("faq", "AI-enabled courses"),
]


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

        order = 0
        for title in _COURSE_TITLES:
            order += 1
            _mk_k(OX, title, "course",
                  body="Best for career growth.",
                  attrs={"duration": "12 Months",
                         "commercial": {"currency": "INR",
                                        "base_price": 1000 + order,
                                        "payment_url": None,
                                        "offers": []}},
                  order=order)
        for kind, title in _STANDALONE:
            order += 1
            _mk_k(OX, title, kind, body=f"{title} details.", order=order)

        # One INACTIVE row -- must be visible to admin, invisible to the AI.
        inactive = _mk_k(OX, "Retired Legacy Course", "course",
                         body="No longer offered.", active=False, order=99)

        # A row with the full pricing shape incl. a legacy payment URL.
        rich = _mk_k(OX, "PGDCA Rich Detail", "course",
                     body="Full shape.",
                     attrs={"duration": "12 Months",
                            "commercial": {
                                "currency": "INR", "base_price": 19540,
                                "payment_url": None,
                                "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t",
                                "offers": [{"label": "Diwali", "final_price": 17000,
                                            "valid_from": "2026-10-15",
                                            "valid_until": "2026-11-05"}]},
                            "regulatory": {
                                "source": "Kerala State Rutronix fee card",
                                "as_of": "2026",
                                "components": [
                                    {"type": "registration_fee",
                                     "label": "Registration Fee", "amount": 4500},
                                    {"type": "net_tuition_fee",
                                     "label": "Net Tuition Fee to ATC",
                                     "amount": 15040}]}},
                     order=100)

        rival_row = _mk_k(OTHER, "Rival Secret Course", "course",
                          body="Rival body.",
                          attrs={"commercial": {"base_price": 999999}},
                          order=1)

        ids = {"ox_admin": ox_admin.id, "ox_staff": ox_staff.id,
               "rival_admin": rival_admin.id, "inactive": inactive.id,
               "rich": rich.id, "rival_row": rival_row.id}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ Tenant isolation — the core safety property ═══════════════════════════

class TestTenantIsolation:

    def test_tenant_a_lists_only_tenant_a_rows(self, seeded):
        with _APP.app_context():
            result = kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)
        assert result["total"] > 0
        assert all(r.tenant_id == OX for r in result["rows"])
        assert not any("Rival" in r.title for r in result["rows"])

    def test_tenant_b_cannot_get_tenant_a_row_by_id(self, seeded):
        """Looked up by (id AND tenant_id) together -- another tenant's id is
        simply not found, never a leak and never a confirmation."""
        with _APP.app_context():
            assert kas.get_knowledge(OTHER, seeded["rich"]) is None
            assert kas.get_knowledge(OX, seeded["rival_row"]) is None

    def test_route_rejects_cross_tenant_row_id_with_404(self, seeded):
        r = client(seeded["rival_admin"]).get(
            f"/tenant/courses/{seeded['rich']}", follow_redirects=False)
        assert r.status_code == 404

    def test_rival_admin_list_page_shows_no_oxford_data(self, seeded):
        r = client(seeded["rival_admin"]).get("/tenant/courses",
                                              follow_redirects=False)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "PGDCA" not in body
        assert "Rival Secret Course" in body

    def test_falsy_tenant_id_lists_nothing(self, seeded):
        with _APP.app_context():
            for bad in (None, "", 0, False):
                assert kas.list_knowledge(bad)["rows"] == ()
                assert kas.get_knowledge(bad, seeded["rich"]) is None


# ═══ Inactive rows: visible to admin, invisible to the AI ═════════════════

class TestInactiveRows:

    def test_inactive_row_visible_in_admin_listing(self, seeded):
        with _APP.app_context():
            result = kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)
            titles = [r.title for r in result["rows"]]
        assert "Retired Legacy Course" in titles

    def test_inactive_row_retrievable_in_admin_detail(self, seeded):
        with _APP.app_context():
            row = kas.get_knowledge(OX, seeded["inactive"])
        assert row is not None
        assert row.is_active is False

    def test_inactive_row_still_excluded_from_ai_retrieval(self, seeded):
        """The whole point of a SEPARATE admin path: the prompt path must
        still refuse to surface an inactive row."""
        with _APP.app_context():
            ai_rows = ks.fetch_knowledge(OX, limit=ks.MAX_ITEMS)
            ai_titles = [r.title for r in ai_rows]
        assert "Retired Legacy Course" not in ai_titles

    def test_inactive_row_renders_on_the_detail_page(self, seeded):
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['inactive']}", follow_redirects=False)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Retired Legacy Course" in body
        assert "Inactive" in body


# ═══ Pagination and the independent admin bound ═══════════════════════════

class TestPagination:

    def test_full_catalog_listable_without_silent_truncation(self, seeded):
        """THE regression this phase exists to prevent: 20 Oxford rows must
        all be reachable, not silently cut to MAX_ITEMS=8."""
        with _APP.app_context():
            total = kas.list_knowledge(OX)["total"]
            seen = set()
            page = 1
            while True:
                result = kas.list_knowledge(OX, page=page, per_page=5)
                if not result["rows"]:
                    break
                seen.update(r.id for r in result["rows"])
                if not result["has_next"]:
                    break
                page += 1
        assert total == 20  # 13 courses + 5 standalone + 1 inactive + 1 rich
        assert len(seen) == total

    def test_admin_bound_is_independent_of_prompt_max_items(self, seeded):
        """A single admin page returns more than the prompt ceiling -- proof
        the two bounds are genuinely separate."""
        with _APP.app_context():
            result = kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)
        assert len(result["rows"]) > ks.MAX_ITEMS

    def test_admin_per_page_ceiling_is_enforced(self, seeded):
        with _APP.app_context():
            result = kas.list_knowledge(OX, per_page=99999)
        assert result["per_page"] <= kas.MAX_PER_PAGE

    def test_page_and_per_page_are_sanitised(self, seeded):
        with _APP.app_context():
            assert kas.list_knowledge(OX, page=0)["page"] >= 1
            assert kas.list_knowledge(OX, page=-5)["page"] >= 1
            assert kas.list_knowledge(OX, per_page=0)["per_page"] >= 1
            assert kas.list_knowledge(OX, page="abc")["page"] >= 1

    def test_pagination_metadata_is_consistent(self, seeded):
        with _APP.app_context():
            first = kas.list_knowledge(OX, page=1, per_page=5)
        assert first["has_prev"] is False
        assert first["has_next"] is True
        assert first["pages"] == (first["total"] + 4) // 5

    def test_kind_filter_scopes_within_tenant(self, seeded):
        with _APP.app_context():
            faqs = kas.list_knowledge(OX, kind="faq", per_page=kas.MAX_PER_PAGE)
        assert faqs["total"] == 3
        assert all(r.kind == "faq" and r.tenant_id == OX for r in faqs["rows"])


# ═══ Prompt path must be completely unaffected ════════════════════════════

class TestPromptPathUnchanged:

    def test_max_items_value_unchanged(self):
        assert ks.MAX_ITEMS == 8

    def test_admin_service_does_not_import_or_reuse_max_items(self):
        """The admin bound must not be derived from the prompt bound.

        AST-based, not substring search: the module's docstring legitimately
        explains WHY it avoids MAX_ITEMS, so a raw `"MAX_ITEMS" not in src`
        check false-fires on its own documentation. What actually matters is
        that no CODE references the name and that knowledge_service is not
        imported at all."""
        src = open(KAS_PY, encoding="utf-8").read()
        tree = ast.parse(src)

        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names} | {
                   n.module for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("knowledge_service" in m for m in imported)

        # No executable reference to the name anywhere.
        referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        referenced |= {n.attr for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute)}
        assert "MAX_ITEMS" not in referenced

    def test_knowledge_service_changed_only_where_rc254cx1a_authorised(self):
        """NARROWED BY RC2.5.4c-x-1a (was test_knowledge_service_file_
        untouched_this_phase, a plain zero-diff pin).

        RC2.5.4c-x-1a is separately authorised to make ONE change to this
        file: `base_price` joins _NON_RENDERABLE_KEYS, so the AI stops
        receiving two prices for one course. (commercial.base_price and
        commercial.normal_total_fee both hold the customer price; the
        deterministic paths apply a precedence rule via CourseRecord, the
        flattener did not, and PGDCA carried 16000 and 19540 simultaneously
        for ~17h.)

        The guard is narrowed, not dropped, and to something STRONGER than
        the porcelain check it replaces: the exclusion set must equal the
        authorised value EXACTLY, and every other top-level construct in the
        file must be byte-identical to HEAD. A change to MAX_ITEMS,
        _flatten_attrs, _render_row, _is_renderable_scalar, fetch_knowledge
        or any constant still fails -- which is the prompt-path guarantee
        this test exists to protect.

        Once 1a is committed the diff is empty and every comparison is
        trivially equal, exactly as before.
        """
        assert ks._NON_RENDERABLE_KEYS == frozenset(
            {"legacy_payment_url", "payment_url", "base_price"})
        _assert_knowledge_service_only_authorised_change(ROOT)

    def test_fetch_knowledge_still_bounded_and_active_only(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, limit=99999)
        assert len(rows) <= ks.MAX_ITEMS
        assert all(r.is_active for r in rows)


# ═══ Authorization ════════════════════════════════════════════════════════

class TestAuthorization:

    def test_staff_forbidden_on_list(self, seeded):
        r = client(seeded["ox_staff"]).get("/tenant/courses",
                                           follow_redirects=False)
        assert r.status_code == 403

    def test_staff_forbidden_on_detail(self, seeded):
        r = client(seeded["ox_staff"]).get(
            f"/tenant/courses/{seeded['rich']}", follow_redirects=False)
        assert r.status_code == 403

    def test_unauthenticated_redirected_not_served(self, seeded):
        r = _APP.test_client().get("/tenant/courses", follow_redirects=False)
        assert r.status_code in (301, 302)
        assert "/tenant/courses" not in r.headers.get("Location", "")

    def test_admin_allowed(self, seeded):
        r = client(seeded["ox_admin"]).get("/tenant/courses",
                                           follow_redirects=False)
        assert r.status_code == 200

    def test_routes_carry_the_standard_decorators(self):
        """Source-level: both new routes must sit behind login_required AND
        tenant_admin_required, like every other tenant route."""
        src = open(os.path.join(ROOT, "app", "routes", "tenant.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        for fn_name in ("tenant_courses", "tenant_course_detail"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            decorators = {ast.unparse(d) for d in fn.decorator_list}
            assert "login_required" in decorators, fn_name
            assert "tenant_admin_required" in decorators, fn_name

    def test_billing_guard_still_applies_to_new_routes(self, seeded):
        """The blueprint-level before_request must gate the new routes too --
        a SUSPENDED, non-exempt tenant is redirected to billing."""
        with _APP.app_context():
            t = Tenant.query.get(OX)
            t.status = "SUSPENDED"
            t.billing_exempt = False
            db.session.commit()
        try:
            r = client(seeded["ox_admin"]).get("/tenant/courses",
                                               follow_redirects=False)
            assert r.status_code in (301, 302)
            assert "billing" in r.headers.get("Location", "").lower()
        finally:
            with _APP.app_context():
                t = Tenant.query.get(OX)
                t.status = "ACTIVE"
                t.billing_exempt = True
                db.session.commit()


# ═══ Detail rendering ═════════════════════════════════════════════════════

class TestDetailRendering:

    def test_invalid_row_id_returns_404(self, seeded):
        r = client(seeded["ox_admin"]).get("/tenant/courses/99999999",
                                           follow_redirects=False)
        assert r.status_code == 404

    def test_non_integer_row_id_does_not_500(self, seeded):
        r = client(seeded["ox_admin"]).get("/tenant/courses/not-an-int",
                                           follow_redirects=False)
        assert r.status_code == 404  # <int:> converter rejects it

    def test_get_knowledge_handles_non_integer_safely(self, seeded):
        with _APP.app_context():
            assert kas.get_knowledge(OX, "abc") is None
            assert kas.get_knowledge(OX, None) is None

    def test_detail_shows_pricing_and_regulatory(self, seeded):
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}", follow_redirects=False)
        body = r.get_data(as_text=True)
        assert "19540" in body
        assert "Registration Fee" in body
        assert "4500" in body

    def test_detail_shows_offers(self, seeded):
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}", follow_redirects=False)
        body = r.get_data(as_text=True)
        assert "Diwali" in body
        assert "17000" in body

    def test_legacy_payment_url_shown_but_clearly_marked_internal(self, seeded):
        """Permitted on the ADMIN surface (the owner may see their own data),
        but must be unmistakably labelled as hidden from AI/customers."""
        r = client(seeded["ox_admin"]).get(
            f"/tenant/courses/{seeded['rich']}", follow_redirects=False)
        body = r.get_data(as_text=True)
        assert "rzp.io/rzp/KAQ2C7t" in body
        assert "Hidden from your AI assistant" in body
        assert "internal reference only" in body.lower()

    def test_legacy_payment_url_still_absent_from_the_composed_prompt(self, seeded):
        """The admin screen showing it must NOT have weakened the prompt-side
        exclusion -- verified end-to-end, not assumed."""
        from app.services import prompt_composer
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        assert "rzp.io/rzp/KAQ2C7t" not in out
        assert "legacy_payment_url" not in out

    def test_malformed_attributes_do_not_break_the_page(self, seeded):
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX).first()
            row.attributes = "{not valid json"
            db.session.commit()
            rid = row.id
        r = client(seeded["ox_admin"]).get(f"/tenant/courses/{rid}",
                                           follow_redirects=False)
        assert r.status_code == 200

    def test_list_page_renders_tenant_name_not_hardcoded_branding(self, seeded):
        """The UI must be tenant-generic: a rival admin sees their own name."""
        r = client(seeded["rival_admin"]).get("/tenant/courses",
                                              follow_redirects=False)
        assert "Rival" in r.get_data(as_text=True)


# ═══ Read-only guarantee ══════════════════════════════════════════════════

class TestReadOnly:

    def test_admin_service_has_no_hard_delete_function(self):
        """TRIPWIRE INVERTED BY RC2.5.4b (was: no write functions at all).

        RC2.5.4a was read-only, so this pinned the ABSENCE of every mutation.
        RC2.5.4b explicitly authorises create / update / activation-toggle,
        so that blanket pin no longer describes the intended design.

        Inverted rather than deleted: the property still worth guarding is
        that DELETION never becomes possible here. Deactivation is the only
        removal, and it is reversible. A hard-delete function appearing in
        this module would still fail this test.
        """
        tree = ast.parse(open(KAS_PY, encoding="utf-8").read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        for forbidden in ("delete", "destroy", "purge", "drop"):
            assert not any(forbidden in n.lower() for n in names), names
        # The authorised RC2.5.4b mutations must be present and named
        # explicitly -- no mutation may hide behind a vague name.
        assert {"create_knowledge", "update_knowledge", "set_active"} <= names

    def test_admin_service_never_hard_deletes(self):
        """TRIPWIRE INVERTED BY RC2.5.4b (was: no db.session.add/commit at
        all). Writes are now authorised; physical deletion still is not."""
        src = open(KAS_PY, encoding="utf-8").read()
        for forbidden in ("db.session.delete", ".delete()"):
            assert forbidden not in src

    def test_new_routes_are_get_only(self):
        src = open(os.path.join(ROOT, "app", "routes", "tenant.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        for fn_name in ("tenant_courses", "tenant_course_detail"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            route = next(ast.unparse(d) for d in fn.decorator_list
                         if "tenant_bp.route" in ast.unparse(d))
            assert "'POST'" not in route and '"POST"' not in route, fn_name
            assert "GET" in route, fn_name

    def test_listing_does_not_mutate_data(self, seeded):
        with _APP.app_context():
            before = TenantKnowledge.query.count()
            kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)
            kas.get_knowledge(OX, seeded["rich"])
            db.session.expire_all()
            assert TenantKnowledge.query.count() == before


# ═══ Fail-open ════════════════════════════════════════════════════════════

class TestFailOpen:

    def test_db_error_during_listing_returns_empty_page(self, seeded, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("simulated DB outage")
        monkeypatch.setattr(kas, "_admin_base_query", _boom)
        with _APP.app_context():
            result = kas.list_knowledge(OX)
        assert result["rows"] == ()
        assert result["total"] == 0

    def test_parse_attributes_never_raises(self, seeded):
        class _Bad:
            id = 1
            attributes = "{broken"
        assert kas.parse_attributes(_Bad()) == {}


# ═══ Scope ════════════════════════════════════════════════════════════════

class TestScope:

    def test_forbidden_files_untouched(self):
        # NARROWED BY RC2.5.4c-x-1a: app/services/knowledge_service.py moves
        # out of this blanket zero-diff list and into the dedicated,
        # stricter test_knowledge_service_changed_only_where_rc254cx1a_
        # authorised above, which permits ONLY the authorised exclusion-set
        # change and still rejects every other edit to that file. Every other
        # path below keeps its full zero-diff protection unchanged.
        import subprocess
        # NARROWED BY RC2.5.4c-x-6b1: app/services/ai_service.py and
        # app/bot/constants.py move out of this blanket zero-diff list into
        # the dedicated, stricter construct-level tests below, which permit
        # ONLY the authorised EMI-claim removals and still reject every other
        # edit to those files. Every other path keeps full zero-diff cover.
        for path in ("app/models.py", "migrations/",
                     "app/services/prompt_composer.py",
                     "app/bot/router.py",
                     "app/services/whatsapp_service.py",
                     "app/routes/webhook.py"):
            out = subprocess.run(["git", "status", "--porcelain", "--", path],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            assert out.strip() == "", f"{path} unexpectedly changed"

    def test_ai_service_changed_only_where_x6b1_authorised(self):
        _assert_only_x6b1_constructs_changed(ROOT, "app/services/ai_service.py")

    def test_constants_changed_only_where_x6b1_authorised(self):
        _assert_only_x6b1_constructs_changed(ROOT, "app/bot/constants.py")

    def test_payment_links_untouched(self):
        from app.bot.constants import COURSE_PAYMENT_LINKS
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"

    def test_templates_use_the_existing_dark_design_system(self):
        """RC2.5.4a keeps the existing visual language -- no light-palette
        redesign, no new CSS framework, reusing the t-* components."""
        for name in ("courses.html", "course_detail.html"):
            src = open(os.path.join(ROOT, "templates", "tenant", name),
                       encoding="utf-8").read()
            assert "t-card" in src
            assert "tenant/sidebar.html" in src
            assert "var(--" in src  # uses the existing CSS custom properties

    def test_no_chart_library_introduced(self):
        for name in ("courses.html", "course_detail.html"):
            src = open(os.path.join(ROOT, "templates", "tenant", name),
                       encoding="utf-8").read().lower()
            for lib in ("chart.js", "apexcharts", "d3.js", "plotly"):
                assert lib not in src
