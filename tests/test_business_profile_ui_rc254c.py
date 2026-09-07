"""Phase RC2.5.4c — tenant self-service Business Profile UI.

THE GAP
-------
The identity architecture already existed and was production-validated:
resolve_business_identity() reads the `business_profile` section of
TenantSettings and falls back PER FIELD to the platform defaults, and
tenant_settings_service.set_section() was written in RC2.5.2 as the write
path. That write path had FOUR tests and ZERO application callers -- a
tenant had no way to author its own identity, so every tenant but Oxford
resolved entirely to Oxford's defaults.

This phase supplies only the UI and that write. It changes no resolution
semantics, no fallback behaviour, and no deterministic output.

THE TRAP THIS FILE GUARDS
--------------------------
The obvious implementation feeds the form from resolve_business_identity().
That would show a tenant Oxford's address, phone and hours as if they were
its own saved values -- and the moment it pressed Save, they would BECOME its
own saved values. A fallback would have been silently promoted to authored
content, which is the exact F2 confusion the c-5 provenance work exists to
prevent, re-introduced through the admin surface.

So the form renders the RAW STORED SECTION and nothing else:
    stored value -> shown
    no stored value -> blank ("leave blank to use the platform default")

test_missing_values_render_blank_not_platform_defaults is the assertion that
holds that line.

EMPTY-FIELD SEMANTICS
----------------------
Blank fields are OMITTED from the section rather than stored as "". An empty
string is a value the per-field resolver would honour, and the contract's
"not set" is "absent". A wholly blank submission therefore yields {}, which
resolve_business_identity() reads as is_configured=False -- preserving the
existing TWO-state contract instead of inventing a third.

OUT OF SCOPE, NOT TOUCHED: tenant_identity_service, prompt_composer,
catalogue_service, payment_link_service, app/bot/*, models, migrations.
CSRF is a real, pre-existing, platform-wide gap (Flask-WTF is not installed
and CSRFProtect is never initialised); this page matches the existing posture
of /tenant/profile, /tenant/staff and /tenant/whatsapp/save rather than
inventing a partial parallel one. It is scheduled for RC2.5.5d and is
asserted-as-observed below, not claimed as solved.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc254c_business_profile.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254c-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254c-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc254c-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc254c-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantSettings, User                     # noqa: E402
from app.bot.business_profile import BUSINESS_PROFILE                   # noqa: E402
from app.services import tenant_settings_service as settings_svc        # noqa: E402
from app.services import tenant_identity_service as ident               # noqa: E402

A = "t-alpha"      # authors a profile
B = "t-beta"       # must never be touched by A
KEY = ident.SETTINGS_KEY

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = A

# A complete Business Profile submission, one value per exposed field.
FORM = {
    "name": "Alpha Institute",
    "industry": "Education",
    "billing_email": "billing@alpha.test",
    "bp_legal_name": "Alpha Institute Pvt Ltd",
    "bp_description": "Vocational training for working adults.",
    "bp_tagline": "Learn. Apply. Advance.",
    "bp_address_line": "12 Alpha Street",
    "bp_address_locality": "Indiranagar",
    "bp_address_city": "Bengaluru",
    "bp_address_region": "Karnataka",
    "bp_address_country": "India",
    "bp_address_postal_code": "560038",
    "bp_location_url": "https://maps.example/alpha",
    "bp_contact_phone": "9000011111",
    "bp_contact_whatsapp": "9000022222",
    "bp_contact_email": "hello@alpha.test",
    "bp_contact_website": "alpha.test",
    "bp_hours_general": "10 AM - 6 PM (Mon-Fri)",
    "bp_hours_extended": "9 AM - 8 PM (Mon-Sat)",
    "bp_brand_voice": "Warm, concise, professional.",
}
BLANK = {k: ("" if k.startswith("bp_") else v) for k, v in FORM.items()}


def _mk_user(tenant, username, role):
    u = User(username=username, email=f"{username}.{tenant}@x.test",
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding -- flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((A, "Alpha"), (B, "Beta")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        ids = {
            "a_admin": _mk_user(A, "admin_a", "ADMIN").id,
            "a_staff": _mk_user(A, "staff_a", "STAFF").id,
            "b_admin": _mk_user(B, "admin_b", "ADMIN").id,
        }
        # Tenant B starts with an authored profile AND a sibling section, so
        # both "A cannot touch B" and "siblings survive" are observable.
        settings_svc.set_section(B, KEY, {"legal_name": "Beta Ltd",
                                          "contact": {"phone": "8888800000"}})
        row = TenantSettings.query.filter_by(tenant_id=B).first()
        blob = json.loads(row.settings)
        blob["branding"] = {"primary_color": "#00ff00"}
        row.settings = json.dumps(blob)
        db.session.commit()
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _section(tenant_id):
    with _APP.app_context():
        return settings_svc.get_section(tenant_id, KEY)


def _blob(tenant_id):
    with _APP.app_context():
        row = TenantSettings.query.filter_by(tenant_id=tenant_id).first()
        return json.loads(row.settings) if row else {}


# ═══ 1-4 Access control ════════════════════════════════════════════════════

class TestAccess:

    def test_admin_can_get_the_profile_page(self, seeded):
        r = client(seeded["a_admin"]).get("/tenant/profile")
        assert r.status_code == 200
        assert b"Business Profile" in r.data

    def test_admin_can_post_the_business_profile(self, seeded):
        r = client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert r.status_code in (200, 302)
        assert _section(A).get("legal_name") == "Alpha Institute Pvt Ltd"

    def test_staff_is_forbidden(self, seeded):
        c = client(seeded["a_staff"])
        assert c.get("/tenant/profile").status_code == 403
        assert c.post("/tenant/profile", data=FORM).status_code == 403
        assert _section(A) == {}, "a forbidden POST must not persist"

    def test_anonymous_is_redirected_and_writes_nothing(self, seeded):
        c = _APP.test_client()
        assert c.get("/tenant/profile", follow_redirects=False).status_code in (301, 302)
        c.post("/tenant/profile", data=FORM, follow_redirects=False)
        assert _section(A) == {}


# ═══ 5-6 Rendering ═════════════════════════════════════════════════════════

class TestRendering:

    def test_stored_values_render(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        body = client(seeded["a_admin"]).get("/tenant/profile").get_data(as_text=True)
        for value in ("Alpha Institute Pvt Ltd", "12 Alpha Street", "Bengaluru",
                      "9000011111", "hello@alpha.test",
                      "10 AM - 6 PM (Mon-Fri)", "Warm, concise, professional."):
            assert value in body, f"stored value missing from the form: {value!r}"

    def test_missing_values_render_blank_not_platform_defaults(self, seeded):
        """THE central guarantee of this phase.

        With nothing authored, the form must be EMPTY. If it were fed from
        resolve_business_identity() it would show Oxford's address, phone,
        website and hours as this tenant's own saved data -- and saving would
        make them so.
        """
        body = client(seeded["a_admin"]).get("/tenant/profile").get_data(as_text=True)
        for leaked in (BUSINESS_PROFILE["address"], BUSINESS_PROFILE["phone"],
                       BUSINESS_PROFILE["email"], BUSINESS_PROFILE["website"],
                       BUSINESS_PROFILE["maps_url"], BUSINESS_PROFILE["locality"],
                       BUSINESS_PROFILE["city"]):
            assert leaked not in body, f"platform default prefilled: {leaked!r}"
        with _APP.app_context():
            resolved = ident.resolve_business_identity(A)
        assert resolved.contact.phone == BUSINESS_PROFILE["phone"], \
            "precondition: the RESOLVER does still fall back"
        assert resolved.is_configured is False

    def test_the_form_tells_the_admin_blank_means_default(self, seeded):
        body = client(seeded["a_admin"]).get("/tenant/profile").get_data(as_text=True)
        assert "blank" in body.lower() and "default" in body.lower()


# ═══ 7-12 Persistence ══════════════════════════════════════════════════════

class TestPersistence:

    def test_scalars_persist(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        s = _section(A)
        assert s["legal_name"] == "Alpha Institute Pvt Ltd"
        assert s["tagline"] == "Learn. Apply. Advance."
        assert s["description"] == "Vocational training for working adults."
        assert s["location_url"] == "https://maps.example/alpha"

    def test_nested_address_persists(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        addr = _section(A)["address"]
        assert addr == {"line": "12 Alpha Street", "locality": "Indiranagar",
                        "city": "Bengaluru", "region": "Karnataka",
                        "country": "India", "postal_code": "560038"}

    def test_nested_contact_persists(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert _section(A)["contact"] == {
            "phone": "9000011111", "whatsapp": "9000022222",
            "email": "hello@alpha.test", "website": "alpha.test"}

    def test_hours_persist(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert _section(A)["hours"] == {"general": "10 AM - 6 PM (Mon-Fri)",
                                        "extended": "9 AM - 8 PM (Mon-Sat)"}

    def test_brand_voice_persists(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert _section(A)["brand_voice"] == "Warm, concise, professional."

    def test_sibling_settings_sections_survive(self, seeded):
        """set_section()'s read-modify-write, exercised through the UI: a
        Business Profile save must not clear branding/locale/features."""
        with _APP.app_context():
            settings_svc.set_section(A, "branding", {"primary_color": "#ff0000"})
            db.session.commit()
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        blob = _blob(A)
        assert blob["branding"] == {"primary_color": "#ff0000"}
        assert blob[KEY]["legal_name"] == "Alpha Institute Pvt Ltd"
        assert "_v" in blob, "set_section must still stamp the schema version"

    def test_values_are_trimmed(self, seeded):
        padded = dict(FORM, bp_legal_name="  Alpha Institute Pvt Ltd  ",
                      bp_contact_phone="  9000011111  ")
        client(seeded["a_admin"]).post("/tenant/profile", data=padded)
        s = _section(A)
        assert s["legal_name"] == "Alpha Institute Pvt Ltd"
        assert s["contact"]["phone"] == "9000011111"

    def test_editing_replaces_rather_than_accumulates(self, seeded):
        c = client(seeded["a_admin"])
        c.post("/tenant/profile", data=FORM)
        c.post("/tenant/profile", data=dict(FORM, bp_tagline="A new tagline"))
        assert _section(A)["tagline"] == "A new tagline"


# ═══ 13-15 Semantics ═══════════════════════════════════════════════════════

class TestSemantics:

    def test_authoring_flips_is_configured_to_true(self, seeded):
        with _APP.app_context():
            assert ident.resolve_business_identity(A).is_configured is False
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        with _APP.app_context():
            resolved = ident.resolve_business_identity(A)
        assert resolved.is_configured is True
        assert resolved.contact.phone == "9000011111"
        assert resolved.address.city == "Bengaluru"

    def test_an_all_blank_submission_stays_unconfigured(self, seeded):
        """The two-state contract, preserved. Blank fields are OMITTED, not
        stored as "", so a wholly blank save yields {} -- which the resolver
        reads as unconfigured. Storing "" instead would produce a third,
        misleading state: 'configured' with empty values that the per-field
        fallback would then honour."""
        client(seeded["a_admin"]).post("/tenant/profile", data=BLANK)
        assert _section(A) == {}
        with _APP.app_context():
            resolved = ident.resolve_business_identity(A)
        assert resolved.is_configured is False
        assert resolved.contact.phone == BUSINESS_PROFILE["phone"]

    def test_clearing_one_field_falls_back_for_that_field_only(self, seeded):
        c = client(seeded["a_admin"])
        c.post("/tenant/profile", data=FORM)
        c.post("/tenant/profile", data=dict(FORM, bp_contact_phone=""))
        s = _section(A)
        assert "phone" not in s["contact"]
        assert s["contact"]["email"] == "hello@alpha.test"
        with _APP.app_context():
            resolved = ident.resolve_business_identity(A)
        assert resolved.contact.phone == BUSINESS_PROFILE["phone"]
        assert resolved.contact.email == "hello@alpha.test"

    def test_malformed_existing_settings_do_not_crash_the_page(self, seeded):
        with _APP.app_context():
            row = TenantSettings.query.filter_by(tenant_id=A).first()
            if row is None:
                row = TenantSettings(tenant_id=A, settings="{}")
                db.session.add(row)
            row.settings = "{not valid json"
            db.session.commit()
        r = client(seeded["a_admin"]).get("/tenant/profile")
        assert r.status_code == 200
        r2 = client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert r2.status_code in (200, 302)
        assert _section(A)["legal_name"] == "Alpha Institute Pvt Ltd"

    def test_a_stray_form_field_cannot_inject_a_key(self, seeded):
        client(seeded["a_admin"]).post(
            "/tenant/profile", data=dict(FORM, bp_evil="x", evil="y",
                                         bp_address_evil="z"))
        s = _section(A)
        assert "evil" not in s and "bp_evil" not in s
        assert "evil" not in s["address"]


# ═══ 16-18 Isolation ═══════════════════════════════════════════════════════

class TestTenantIsolation:

    def test_tenant_a_cannot_update_tenant_b_via_form_field(self, seeded):
        """The handler derives the tenant from the session only. A tenant_id
        in the payload must be inert."""
        before = _section(B)
        client(seeded["a_admin"]).post(
            "/tenant/profile",
            data=dict(FORM, tenant_id=B, tenant=B, id=B))
        assert _section(B) == before, "tenant A wrote into tenant B"
        assert _section(A)["legal_name"] == "Alpha Institute Pvt Ltd"

    def test_tenant_a_cannot_update_tenant_b_via_query_string(self, seeded):
        before = _section(B)
        client(seeded["a_admin"]).post(f"/tenant/profile?tenant_id={B}", data=FORM)
        assert _section(B) == before
        assert _section(A) != {}

    def test_tenant_a_post_changes_only_tenant_a(self, seeded):
        b_blob_before = _blob(B)
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert _blob(B) == b_blob_before
        with _APP.app_context():
            assert Tenant.query.get(B).name == "Beta"

    def test_tenant_b_remains_unchanged_and_still_resolves_its_own(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        assert _section(B)["legal_name"] == "Beta Ltd"
        assert _section(B)["contact"]["phone"] == "8888800000"
        assert _blob(B)["branding"] == {"primary_color": "#00ff00"}
        with _APP.app_context():
            b = ident.resolve_business_identity(B)
        assert b.legal_name == "Beta Ltd" and b.contact.phone == "8888800000"

    def test_tenant_b_admin_sees_only_its_own_values(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        body = client(seeded["b_admin"]).get("/tenant/profile").get_data(as_text=True)
        assert "Beta Ltd" in body
        assert "Alpha Institute Pvt Ltd" not in body
        assert "12 Alpha Street" not in body


# ═══ Existing Tenant-column behaviour must not regress ═════════════════════

class TestExistingColumnsPreserved:

    def test_name_industry_and_billing_email_still_save(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        with _APP.app_context():
            t = Tenant.query.get(A)
        assert t.name == "Alpha Institute"
        assert t.industry == "Education"
        assert t.billing_email == "billing@alpha.test"

    def test_empty_name_is_still_rejected(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=dict(FORM, name=""))
        with _APP.app_context():
            assert Tenant.query.get(A).name == "Alpha"
        assert _section(A) == {}, "a rejected save must not write the profile"

    def test_columns_are_not_written_into_the_json_section(self, seeded):
        client(seeded["a_admin"]).post("/tenant/profile", data=FORM)
        s = _section(A)
        for column_key in ("name", "industry", "billing_email"):
            assert column_key not in s


# ═══ Source guards ═════════════════════════════════════════════════════════

def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestSourceContract:

    def test_the_handler_never_reads_a_tenant_id_from_the_request(self):
        tree = ast.parse(_src("app/routes/tenant.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "tenant_profile")
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "get":
                base = n.func.value
                src = ast.dump(base)
                if "tenant" in str(getattr(n.args[0], "value", "")).lower():
                    assert "form" not in src and "args" not in src, \
                        "tenant identifier read from the request"

    def test_persistence_goes_through_set_section(self):
        """Not a second JSON mechanism: sibling preservation and version
        stamping live in set_section and must not be reimplemented."""
        tree = ast.parse(_src("app/routes/tenant.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "tenant_profile")
        calls = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)}
        assert "set_section" in calls

    def test_the_form_is_not_fed_from_the_resolver(self):
        """resolve_business_identity() must not appear in the profile route:
        its per-field fallback is exactly what must NOT reach the form."""
        tree = ast.parse(_src("app/routes/tenant.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "tenant_profile")
        names = {getattr(n.func, "attr", getattr(n.func, "id", None))
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "resolve_business_identity" not in names

    def test_identity_resolution_is_untouched_by_this_phase(self):
        """RC2.5.4c supplies a write path only. If the resolver changed, the
        fallback semantics c-5 validated in production changed with it."""
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/tenant_identity_service.py",
             "app/services/prompt_composer.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", f"identity/runtime files changed: {out}"

    def test_catalogue_service_changed_only_where_rc254cx_was_authorised(self):
        """NARROWED BY RC2.5.4c-x-a.

        catalogue_service.py used to be pinned zero-diff alongside the two
        resolvers above. RC2.5.4c-x is separately authorised to change it --
        the read-side reconciliation of the RC2.5.4b admin write path
        (commercial.base_price) with the RC2.5.5c-3 read path
        (commercial.normal_total_fee).

        So the guard is narrowed, not dropped, and narrowed to something
        STRONGER than the porcelain check it replaces: every top-level
        construct in the file must be byte-identical to HEAD except
        `_record_from_row`, the one function c-x owns. A change anywhere else
        in the file -- `_default_catalogue`, `catalogue_index_with_provenance`,
        `format_money`, `match_keyword`, LEGACY_NAME_TO_CODE, any constant --
        still fails, which is the c-5 provenance guarantee this test existed
        to protect. `app/services/` as a whole is NOT permitted; the two
        resolvers above remain pinned zero-diff by the assertion above.

        Once c-x is committed the diff is empty and every comparison is
        trivially equal, exactly as before.
        """
        import subprocess
        AUTHORISED = {"_record_from_row"}
        rel = "app/services/catalogue_service.py"
        # NOT text=True: on Windows that decodes git's stdout with the locale
        # codepage (cp1252), which mangles the file's non-ASCII literals --
        # format_money's "₹" -- and the guard then fires on an encoding
        # artifact rather than a real change. Decode utf-8 explicitly, and
        # normalise line endings so a CRLF checkout is not a false positive
        # either (_src reads utf-8 with universal newlines).
        head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=_ROOT,
                              capture_output=True)
        assert head.returncode == 0, (
            f"cannot read HEAD:{rel}: {head.stderr.decode('utf-8', 'replace')}")
        head_src = head.stdout.decode("utf-8").replace("\r\n", "\n")

        def segments(src):
            """Top-level constructs by name, plus the module body outside
            them as one blob, so a moved or deleted constant is caught."""
            tree = ast.parse(src)
            named, other = {}, []
            for node in tree.body:
                seg = ast.get_source_segment(src, node) or ""
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    named[node.name] = seg
                else:
                    other.append(seg)
            return named, "\n".join(other)

        old_named, old_other = segments(head_src)
        new_named, new_other = segments(_src(rel).replace("\r\n", "\n"))

        assert old_other == new_other, "module-level code in catalogue_service.py changed"
        assert set(old_named) == set(new_named), (
            "top-level constructs added or removed: "
            f"{set(old_named) ^ set(new_named)}")
        for name, old_seg in old_named.items():
            if name in AUTHORISED:
                continue
            assert new_named[name] == old_seg, (
                f"{rel}::{name} changed, but only {sorted(AUTHORISED)} "
                "is authorised in this phase")

    def test_csrf_posture_is_unchanged_and_still_absent(self):
        """Asserted as OBSERVED, not as solved. The platform has no CSRF
        mechanism; this page matches the existing posture rather than adding
        a partial one. Remediation is RC2.5.5d. If CSRF is ever introduced,
        this test fails and forces the page to be included deliberately."""
        src = _src("templates/tenant/profile.html")
        assert "csrf_token" not in src
        assert "CSRFProtect" not in _src("app/__init__.py")
