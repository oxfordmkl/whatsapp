"""Phase RC2.5.5c-3: the deterministic runtime reads the TENANT catalogue.

Before this phase the bot served Oxford's hardcoded catalogue to everyone:
ALL_COURSES, COURSE_FEES, GOAL_COURSES, KEYWORD_TO_COURSE and FULL_FEE_TABLE.
A second tenant's customer browsed Oxford's courses at Oxford's prices, and
`AALIZA_PROMPT` recited the same ten courses to every tenant's AI.

The three polarities in play, which are easy to confuse:

  * catalogue  -> fail-SAFE  (no catalogue still shows A catalogue)
  * payment    -> fail-CLOSED (no link means NO link)
  * one course -> not-found  (an unknown code must NEVER become another course)

Import isolation follows test_platform_security_14c.py.
"""
import ast
import json
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc255c3_runtime.db")
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
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.services import payment_link_service as pls                    # noqa: E402
from app.services import prompt_composer                                # noqa: E402
from app.bot import screens, cta_handlers as cta                        # noqa: E402
from app.bot import offer_handlers as oh                                # noqa: E402

OX = "t-ox"
B = "t-b"
EMPTY = "t-empty"

# (code, title, dur_m, reg, net, total, cats, keywords)
OXFORD = [
    ("PGDCA", "PGDCA – Computer Applications", 12, 4500, 15040, 19540, ["job"], ["pgdca", "pgd"]),
    ("AIDM", "AIDM – Digital Marketing", 6, 5900, 25000, 30900, ["business"], ["aidm", "digital marketing"]),
    ("PDCFA", "PDCFA – Computerised Financial Accounting", 6, 2600, 8800, 11400, ["accounting"], ["pdcfa", "tally prime"]),
    ("PYTHON", "Python Programming", 3, 1180, 4000, 5180, ["job"], ["python", "coding"]),
    ("JAVA", "Java Programming", 3, 850, 2800, 3650, ["job"], ["java"]),
    ("DJANGO", "Python Django – Web Development", 3, 3540, 12000, 15540, ["job"], ["django"]),
    ("DGSTP", "DGSTP – Goods & Services Tax Practice", 6, 4890, 19200, 24090, ["accounting"], ["gst", "taxation"]),
    ("DCA", "DCA Fast Track – Computer Applications", 6, 1950, 6400, 8350, ["job", "basic"], ["dca fast track"]),
    ("DCA-REGULAR", "DCA Regular – Computer Applications", 12, 2400, 8000, 10400, ["job", "basic"], ["dca regular"]),
    ("CTTC", "CTTC – Computer Teacher Training", 12, 3550, 12000, 15550, ["basic"], ["cttc", "teacher"]),
    ("CORPORATE-ACCOUNTING", "Corporate Business Accounting & Taxation", 12, 10270, 52000, 62270, ["accounting"], ["corporate"]),
    ("CWPDE", "CWPDE – Word Processing & Data Entry", 6, 1450, 4800, 6250, ["basic"], ["cwpde", "data entry"]),
    ("DOA", "DOA – Office Automation", 6, 1700, 5600, 7300, ["basic"], ["doa"]),
    ("PDDTP", "PDDTP – Desktop Publishing", 6, 1950, 6400, 8350, ["basic"], ["pddtp", "dtp"]),
    ("PDWD", "PDWD – Web Designing using PHP", 6, 2600, 8800, 11400, ["job"], ["pdwd", "php", "web design"]),
    ("WORDPRESS", "WordPress – Website Development", 3, 1300, 4400, 5700, ["business"], ["wordpress"]),
]
PAID = {"PGDCA": "https://rzp.io/rzp/KAQ2C7t", "DCA": "https://rzp.io/rzp/mJPPtM9x",
        "CWPDE": "https://rzp.io/rzp/xkWdKtd", "AIDM": "https://rzp.io/rzp/vF76sj7Y"}

_APP = create_app()
_APP.config["TESTING"] = True


def _row(tenant, code, title, dur_m, reg, net, total, cats, kws, active=True, sort=0):
    attrs = {
        "duration": f"{dur_m} Months", "categories": list(cats), "keywords": list(kws),
        "commercial": {"code": code, "currency": "INR", "normal_total_fee": total,
                       "base_price": total, "emi_available": dur_m in (6, 12),
                       "offers": [],
                       "payment_url": PAID.get(code) if tenant == OX else None},
        "regulatory": {"components": [
            {"type": "registration_fee", "label": "Registration Fee", "amount": reg},
            {"type": "net_tuition_fee", "label": "Net Tuition Fee to ATC", "amount": net}]},
    }
    return TenantKnowledge(tenant_id=tenant, kind="course", title=title,
                           body=f"About {title}.", attributes=json.dumps(attrs),
                           is_active=active, sort_order=sort)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (B, "Beta"), (EMPTY, "Empty")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        for i, spec in enumerate(OXFORD, start=1):
            db.session.add(_row(OX, *spec, sort=i))
        db.session.add(_row(B, "BETA1", "Beta Yoga Foundation", 6, 100, 900, 1000,
                            ["basic"], ["yoga"], sort=1))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# 1-4, 8, 17 ── catalogue basics and identity ────────────────────────────────

class TestCatalogueBasics:

    def test_sixteen_courses_for_oxford(self, seeded):
        with _APP.app_context():
            assert len(cat.list_courses(OX)) == 16

    def test_all_sixteen_codes(self, seeded):
        with _APP.app_context():
            assert {c.code for c in cat.list_courses(OX)} == {s[0] for s in OXFORD}

    def test_ordered_by_sort_order(self, seeded):
        with _APP.app_context():
            orders = [c.sort_order for c in cat.list_courses(OX)]
        assert orders == sorted(orders)

    def test_identity_is_code_not_position_or_title(self, seeded):
        """Renaming a course must not change what it IS."""
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX, title=OXFORD[0][1]).first()
            row.title = "Totally Renamed"
            row.sort_order = 99
            db.session.commit()
            c = cat.get_course(OX, "PGDCA")
        assert c is not None and c.title == "Totally Renamed"

    def test_get_course_is_case_insensitive_on_code(self, seeded):
        with _APP.app_context():
            assert cat.get_course(OX, "pgdca").code == "PGDCA"


# 1, 2, 25 ── tenant isolation ───────────────────────────────────────────────

class TestTenantIsolation:

    def test_tenant_b_sees_only_its_own_catalogue(self, seeded):
        with _APP.app_context():
            codes = {c.code for c in cat.list_courses(B)}
        assert codes == {"BETA1"}

    def test_no_oxford_course_reaches_tenant_b(self, seeded):
        with _APP.app_context():
            titles = " ".join(c.title for c in cat.list_courses(B))
        for bad in ("PGDCA", "DGSTP", "Corporate", "WordPress"):
            assert bad not in titles

    def test_oxford_course_not_gettable_by_tenant_b(self, seeded):
        with _APP.app_context():
            assert cat.get_course(B, "PGDCA") is None

    def test_oxford_catalogue_absent_from_tenant_b_prompt(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(B)
        assert "Beta Yoga" in out
        for bad in ("PGDCA", "DGSTP", "CORPORATE-ACCOUNTING", "62270"):
            assert bad not in out


# 3, 10, 11 ── fallback semantics ────────────────────────────────────────────

class TestFallbackIsFailSafe:

    def test_empty_catalogue_falls_back_to_platform_default(self, seeded):
        with _APP.app_context():
            courses = cat.list_courses(EMPTY)
        assert courses and all(c.is_default for c in courses)

    def test_falsy_tenant_falls_back_not_crashes(self, seeded):
        with _APP.app_context():
            assert cat.list_courses(None)

    def test_falsy_tenant_is_refused_before_any_query(self, seeded, monkeypatch):
        """The guard must REFUSE, not merely happen to find nothing.

        Without it the result is the same today -- TenantKnowledge.tenant_id is
        NOT NULL, so `tenant_id == None` matches no rows and the empty-result
        path falls back anyway. That is an accident of the schema, not a
        decision by this module, and it would stop holding the moment a
        nullable column or an outer join appeared. So the contract is that the
        database is never reached at all.
        """
        touched = []

        class Tripwire:
            def filter(self, *a, **k):
                touched.append(True)
                raise AssertionError("queried the DB for a falsy tenant")

        with _APP.app_context():
            monkeypatch.setattr(TenantKnowledge, "query", Tripwire())
            for bad in (None, "", 0, False):
                assert cat.list_courses(bad)      # platform default, no query
        assert touched == [], "the falsy-tenant guard was removed"

    def test_lookup_failure_falls_back(self, seeded, monkeypatch):
        class Boom:
            def filter(self, *a, **k):
                raise RuntimeError("db down")
        with _APP.app_context():
            monkeypatch.setattr(TenantKnowledge, "query", Boom())
            courses = cat.list_courses(OX)
        assert courses and all(c.is_default for c in courses)

    def test_default_fallback_is_platform_not_a_tenant_query(self, seeded):
        """The fallback must not be 'whatever Oxford has' -- it is the
        platform constant catalogue, flagged as such."""
        with _APP.app_context():
            assert all(c.is_default for c in cat.list_courses(EMPTY))
            assert not any(c.is_default for c in cat.list_courses(OX))


# 9 ── a specific missing course must NOT fall back ──────────────────────────

class TestMissingCourseIsNotSubstituted:

    def test_unknown_code_returns_none(self, seeded):
        with _APP.app_context():
            assert cat.get_course(OX, "NOSUCH") is None

    def test_inactive_course_is_not_returned(self, seeded):
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX, title=OXFORD[4][1]).first()
            row.is_active = False
            db.session.commit()
            assert cat.get_course(OX, "JAVA") is None
            assert "JAVA" not in {c.code for c in cat.list_courses(OX)}

    def test_course_details_screen_returns_none_not_another_course(self, seeded):
        with _APP.app_context():
            assert screens.course_details("NOSUCH", OX) is None


# 12-16 ── discovery and ambiguity ───────────────────────────────────────────

class TestDiscoveryRules:

    def test_dca_is_ambiguous_and_never_auto_selected(self, seeded):
        with _APP.app_context():
            m = cat.match_keyword(OX, "dca")
        assert m.kind == "ambiguous"
        assert {c.code for c in m.candidates} == {"DCA", "DCA-REGULAR"}
        assert m.course is None

    @pytest.mark.parametrize("term", ["sap", "erp"])
    def test_sap_and_erp_never_reach_pdcfa(self, seeded, term):
        with _APP.app_context():
            m = cat.match_keyword(OX, term)
        assert m.kind == "none"

    def test_payroll_never_reaches_dgstp(self, seeded):
        with _APP.app_context():
            m = cat.match_keyword(OX, "payroll")
        assert m.kind == "none"

    def test_wordpress_selects_wordpress_not_pdwd(self, seeded):
        with _APP.app_context():
            m = cat.match_keyword(OX, "wordpress")
        assert m.kind == "exact" and m.course.code == "WORDPRESS"

    def test_python_and_django_select_their_own_course(self, seeded):
        with _APP.app_context():
            assert cat.match_keyword(OX, "python").course.code == "PYTHON"
            assert cat.match_keyword(OX, "django").course.code == "DJANGO"

    def test_explicit_dca_variants_are_not_ambiguous(self, seeded):
        with _APP.app_context():
            assert cat.match_keyword(OX, "dca fast track").course.code == "DCA"
            assert cat.match_keyword(OX, "dca regular").course.code == "DCA-REGULAR"

    def test_categories_use_the_existing_four(self, seeded):
        with _APP.app_context():
            for c in cat.list_courses(OX):
                assert set(c.categories) <= set(cat.CATEGORIES)


# 18 ── legacy conversation compatibility ────────────────────────────────────

class TestLegacyNameCompatibility:
    """The 48 live conversations hold pre-c-3 display names."""

    @pytest.mark.parametrize("legacy,code", [
        ("PGDCA", "PGDCA"), ("pgdca", "PGDCA"),
        ("AIDM Digital Marketing", "AIDM"),
        ("SAP Financial Accounting", "PDCFA"),
        ("Python Programming", "PYTHON"),
        ("DCA Fast Track", "DCA"),
        ("Computer Teacher Training", "CTTC"),
        ("Corporate Business Accounting", "CORPORATE-ACCOUNTING"),
        ("Word Processing & Data Entry", "CWPDE"),
        ("Professional Web Designing", "PDWD"),
    ])
    def test_legacy_name_resolves_to_current_course(self, seeded, legacy, code):
        with _APP.app_context():
            r = cat.resolve_legacy_name(OX, legacy)
        assert r is not None and r.code == code

    def test_legacy_gst_payroll_resolves_to_dgstp(self, seeded):
        with _APP.app_context():
            r = cat.resolve_legacy_name(OX, "GST & Payroll")
        assert r is not None and r.code == "DGSTP"

    def test_legacy_gst_payroll_shows_no_payroll_claim(self, seeded):
        """Compatibility must not resurrect the unsupported claim."""
        with _APP.app_context():
            text, _ = cta.fees_reply("GST & Payroll", OX)
        assert "Payroll" not in text and "18,999" not in text
        assert "DGSTP" in text and "24,090" in text

    def test_unknown_legacy_name_returns_none_not_a_guess(self, seeded):
        with _APP.app_context():
            assert cat.resolve_legacy_name(OX, "Some Retired Course") is None

    def test_current_titles_also_resolve(self, seeded):
        with _APP.app_context():
            assert cat.resolve_legacy_name(OX, "DGSTP – Goods & Services Tax Practice").code == "DGSTP"


# 6, 7 ── pricing and EMI ────────────────────────────────────────────────────

class TestPricingAndEmi:

    @pytest.mark.parametrize("code,total", [(s[0], s[5]) for s in OXFORD])
    def test_normal_total_comes_from_catalogue(self, seeded, code, total):
        with _APP.app_context():
            assert cat.get_course(OX, code).normal_total_fee == total

    @pytest.mark.parametrize("code", ["PYTHON", "JAVA", "DJANGO", "WORDPRESS"])
    def test_three_month_courses_show_no_emi(self, seeded, code):
        with _APP.app_context():
            assert cat.get_course(OX, code).emi_available is False
            text, _ = cta.fees_reply(cat.get_course(OX, code).title, OX)
        assert "EMI not available" in text

    def test_python_no_longer_advertises_emi(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("PYTHON", OX)
        assert "EMI Available" not in screen.body

    def test_longer_courses_show_emi(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("PGDCA", OX)
        assert "EMI Available" in screen.body

    def test_new_prices_are_used_not_legacy_ones(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("PGDCA", OX)
        assert "19,540" in text and "15,999" not in text

    def test_registration_and_tuition_shown_separately(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("PGDCA", OX)
        assert "4,500" in text and "15,040" in text


# 27 ── FULL_FEE_TABLE retired ───────────────────────────────────────────────

class TestFullFeeTableRetired:

    def test_fees_reply_does_not_read_full_fee_table(self):
        tree = ast.parse(_src("app/bot/cta_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "fees_reply")
        assert "FULL_FEE_TABLE" not in {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}

    def test_whole_catalogue_listing_uses_tenant_prices(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("", OX)
        assert "24,090" in text and "62,270" in text
        assert "18,999" not in text and "40,000" not in text

    def test_router_no_longer_imports_catalogue_constants(self):
        tree = ast.parse(_src("app/bot/router.py"))
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.module == "app.bot.constants"
                    for a in n.names}
        for gone in ("ALL_COURSES", "COURSE_FEES", "KEYWORD_TO_COURSE",
                     "GOAL_COURSES", "FULL_FEE_TABLE"):
            assert gone not in imported


# 19-21 ── payment boundary ──────────────────────────────────────────────────

class TestPaymentBoundary:

    def test_catalogue_record_carries_no_payment_url(self, seeded):
        with _APP.app_context():
            c = cat.get_course(OX, "PGDCA")
        assert not hasattr(c, "payment_url")
        assert "rzp.io" not in json.dumps(c.__dict__, default=str)

    def test_catalogue_service_never_reads_a_payment_key(self):
        tree = ast.parse(_src("app/services/catalogue_service.py"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "get":
                for a in n.args:
                    if isinstance(a, ast.Constant) and a.value in (
                            "payment_url", "legacy_payment_url"):
                        raise AssertionError("catalogue_service reads a payment key")

    def test_payment_still_resolves_by_code(self, seeded):
        with _APP.app_context():
            for code, url in PAID.items():
                assert pls.resolve_payment_url(OX, code) == url

    def test_renaming_does_not_break_payment(self, seeded):
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX, title=OXFORD[0][1]).first()
            row.title = "Renamed Again"
            db.session.commit()
            assert pls.resolve_payment_url(OX, "PGDCA") == PAID["PGDCA"]

    def test_unpaid_courses_have_no_link(self, seeded):
        with _APP.app_context():
            for code in ("DGSTP", "PDCFA", "CORPORATE-ACCOUNTING", "JAVA"):
                assert pls.resolve_payment_url(OX, code) is None

    def test_tenant_b_gets_no_oxford_payment_url(self, seeded):
        with _APP.app_context():
            for code in PAID:
                assert pls.resolve_payment_url(B, code) is None


# 22-24, 28 ── AI prompt ─────────────────────────────────────────────────────

class TestPromptCatalogue:

    def test_all_sixteen_identities_reach_the_prompt(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        for code, *_ in OXFORD:
            assert code in out, f"{code} missing from the AI catalogue index"

    def test_index_has_one_entry_per_active_course(self, seeded):
        with _APP.app_context():
            assert len(cat.catalogue_index(OX)) == 16

    def test_index_is_bounded_and_carries_no_bodies(self, seeded):
        with _APP.app_context():
            idx = cat.catalogue_index(OX)
        for line in idx:
            assert "About " not in line, "a full body leaked into the index"

    @pytest.mark.parametrize("obsolete", [
        "GST & Payroll", "Payroll Processing", "Payroll",
        "18,999", "15,999", "SAP Financial Accounting", "40,000",
    ])
    def test_no_obsolete_catalogue_data_in_prompt(self, seeded, obsolete):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        assert obsolete not in out

    def test_dormant_constants_cannot_leak_into_the_prompt(self, seeded):
        """constants.py still HOLDS the old catalogue for the fallback path.
        Its presence in source must not put it in front of Gemini."""
        from app.bot.constants import COURSE_FEES
        assert COURSE_FEES, "precondition: the old table still exists"
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        assert "15,999" not in out and "18,999" not in out

    def test_prompt_source_has_no_hardcoded_catalogue(self):
        src = _src("app/bot/prompts.py")
        for bad in ("GST & Payroll", "SAP Financial Accounting",
                    "15,999", "18,999", "40,000"):
            assert bad not in src

    def test_no_payment_url_in_prompt(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX)
        assert "rzp.io" not in out and "payment_url" not in out

    def test_index_absent_for_a_tenant_with_no_catalogue(self, seeded):
        """A default-catalogue tenant still gets an index -- fail-safe -- but
        it must be the platform default, not another tenant's."""
        with _APP.app_context():
            idx = cat.catalogue_index(EMPTY)
        assert idx and not any("DGSTP" in line for line in idx)


# 26 ── business identity isolation ──────────────────────────────────────────

class TestBusinessIdentityInCourseDetail:

    def test_course_detail_uses_resolved_identity(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("PGDCA", OX)
        assert "theoxfordedu.com" not in _src("app/bot/screens.py").split(
            "def course_details")[1][:1200], "website hardcoded in course_details"
        assert screen.body

    def test_course_detail_uses_THIS_tenants_configured_identity(self, seeded):
        """The decisive identity test: with tenant B configured, resolving
        from None (or from Oxford globals) yields visibly different output.

        Without a configured profile every tenant resolves to the same
        platform defaults, so a mutation swapping tenant_id for None would be
        invisible. Configuring B is what makes the assertion bite.
        """
        import json as _json
        from app.models import TenantSettings
        with _APP.app_context():
            db.session.add(TenantSettings(tenant_id=B, settings=_json.dumps({
                "business_profile": {
                    "contact": {"phone": "0800111222", "website": "beta.example.com"},
                }})))
            db.session.commit()
            screen = screens.course_details("BETA1", B)
        assert "0800111222" in screen.body
        assert "beta.example.com" in screen.body
        from app.bot import business_profile as bp
        assert bp.PHONE not in screen.body
        assert bp.WEBSITE not in screen.body

    def test_course_detail_for_tenant_b_has_no_oxford_course(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("BETA1", B)
        assert "Beta Yoga" in screen.body
        assert "PGDCA" not in screen.body


# 5 ── screens / listing integration ─────────────────────────────────────────

class TestScreensUseTheCatalogue:

    def test_course_list_rows_are_keyed_by_code(self, seeded):
        with _APP.app_context():
            screen = screens.course_list("accounting", OX)
        ids = [r["id"] if isinstance(r, dict) else r.id
               for s in screen.sections for r in s.rows]
        assert any(i.endswith("PDCFA") or i.endswith("DGSTP")
                   or i.endswith("CORPORATE-ACCOUNTING") for i in ids)

    def test_course_list_is_tenant_scoped(self, seeded):
        with _APP.app_context():
            assert screens.course_list("accounting", B) is None

    def test_course_detail_shows_catalogue_price(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("CORPORATE-ACCOUNTING", OX)
        assert "62,270" in screen.body


# D1/D2 -- the contained fix: every payment CTA reads the tenant catalogue ---
#
# Found by the c-3 final pre-commit acceptance audit, missed by the tests
# above because none of them asserted what a payment CTA *displays*.
#
#   D1  the migrated router stores the CATALOGUE title in st["course"], and
#       that title matches none of COURSE_PAYMENT_LINKS' legacy keys -- so the
#       gate the payment CTAs used never opened and NO link was issued at all.
#   D2  where a legacy name did still match, the CTA quoted the constant's
#       obsolete price while fees_reply quoted the catalogue's: two different
#       prices for one course inside one conversation.
#
# code -> (current catalogue price, current catalogue title, the obsolete
#          constant price, the pre-c-3 name still sitting in persisted state)
PAYABLE = {
    "PGDCA": ("\u20b919,540", "PGDCA \u2013 Computer Applications",
              "\u20b915,999", "PGDCA"),
    "DCA": ("\u20b98,350", "DCA Fast Track \u2013 Computer Applications",
            "\u20b96,400", "DCA Fast Track"),
    "CWPDE": ("\u20b96,250", "CWPDE \u2013 Word Processing & Data Entry",
              "\u20b94,800", "Word Processing & Data Entry"),
    "AIDM": ("\u20b930,900", "AIDM \u2013 Digital Marketing",
             "\u20b919,999", "AIDM Digital Marketing"),
}


def _st(course=""):
    return {"course": course, "stage": "start", "offer_course": ""}


# 1-5 -- the four payable courses, through both name-keyed payment CTAs ------

class TestPaymentCtaReadsTheCatalogue:

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_current_title_still_issues_the_payment_link(self, seeded, code):
        """D1: this is the case that issued nothing at all."""
        _price, title, _old, _legacy = PAYABLE[code]
        st = _st(title)
        with _APP.app_context():
            text, _preset = cta.enroll_reply("Alice", title, st, OX)
        assert PAID[code] in text
        assert st["stage"] == "payment_pending"
        assert st["offer_course"] == code

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_enroll_quotes_the_catalogue_price_not_the_constant(self, seeded, code):
        price, title, old, _legacy = PAYABLE[code]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", title, _st(title), OX)
        assert price in text
        assert old not in text

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_pay_intent_quotes_the_catalogue_price_not_the_constant(self, seeded, code):
        price, title, old, _legacy = PAYABLE[code]
        with _APP.app_context():
            text, _ = oh.handle_pay_intent(_st(title), OX)
        assert PAID[code] in text
        assert price in text
        assert old not in text

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_cta_shows_the_catalogue_title_and_duration(self, seeded, code):
        _price, title, _old, _legacy = PAYABLE[code]
        with _APP.app_context():
            duration = cat.get_course(OX, code).duration
            text, _ = cta.enroll_reply("Alice", title, _st(title), OX)
        assert title in text
        assert duration in text

    def test_the_cta_and_the_fee_reply_agree_on_the_price(self, seeded):
        """The symptom D2 produced in one conversation: FEES said 19,540 and
        the enrol CTA that followed said 15,999."""
        _price, title, _old, _legacy = PAYABLE["PGDCA"]
        with _APP.app_context():
            fees, _ = cta.fees_reply(title, OX)
            enrol, _ = cta.enroll_reply("Alice", title, _st(title), OX)
        assert "19,540" in fees and "19,540" in enrol
        assert "15,999" not in fees and "15,999" not in enrol

    def test_no_course_branch_lists_catalogue_prices(self, seeded):
        """The same two obsolete literals lived in the no-course branch."""
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "", _st(), OX)
            first = cat.list_courses(OX)[0]
            money = cat.format_money(first.normal_total_fee)
        assert preset == "GOAL"
        assert "\u20b915,999" not in text and "\u20b96,400" not in text
        assert first.title in text
        assert money in text


# 6, 8 -- conversations persisted before c-3 still hold the OLD name ---------

class TestLegacyStoredNamesStillResolve:

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_legacy_name_reaches_the_same_reply_as_the_new_title(self, seeded, code):
        _price, title, _old, legacy = PAYABLE[code]
        with _APP.app_context():
            new_text, _ = cta.enroll_reply("Alice", title, _st(title), OX)
            old_text, _ = cta.enroll_reply("Alice", legacy, _st(legacy), OX)
        assert old_text == new_text

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_legacy_name_quotes_the_current_price(self, seeded, code):
        price, _title, old, legacy = PAYABLE[code]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", legacy, _st(legacy), OX)
        assert price in text
        assert old not in text

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_legacy_conversation_enters_payment_pending_on_the_code(self, seeded, code):
        _price, _title, _old, legacy = PAYABLE[code]
        st = _st(legacy)
        with _APP.app_context():
            oh.handle_pay_intent(st, OX)
        assert st["stage"] == "payment_pending"
        assert st["offer_course"] == code      # the stable CODE, never a name

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_a_payment_pending_conversation_can_be_re_served(self, seeded, code):
        """A conversation already waiting on a transaction id, holding a
        pre-c-3 name, must still be able to get its link re-sent."""
        _price, _title, _old, legacy = PAYABLE[code]
        st = {"course": legacy, "stage": "payment_pending", "offer_course": code}
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", legacy, st, OX)
        assert PAID[code] in text
        assert st["stage"] == "payment_pending"

    def test_an_unknown_stored_name_issues_no_link(self, seeded):
        """Fail-closed is unchanged: no resolution means no payment link and
        no payment_pending, not a guessed course."""
        st = _st("Course We Never Sold")
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", st["course"], st, OX)
        assert not any(u in text for u in PAID.values())
        assert st["stage"] != "payment_pending"


# 7 -- offer semantics -------------------------------------------------------

class TestOfferFlowSemantics:

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_handle_offer_quotes_the_catalogue_price(self, seeded, code):
        price, title, old, _legacy = PAYABLE[code]
        st = _st()
        with _APP.app_context():
            text, _ = oh.handle_offer(code, st, OX)
        assert price in text and title in text
        assert old not in text
        assert st["offer_course"] == code and st["stage"] == "payment_pending"

    @pytest.mark.parametrize("digit,code", [("1", "CWPDE"), ("2", "DCA"),
                                            ("3", "AIDM"), ("4", "PGDCA")])
    def test_numeric_offer_reply_matches_the_code_path(self, seeded, digit, code):
        with _APP.app_context():
            by_number = oh.handle_offer_number(digit, _st(), OX)
            by_code = oh.handle_offer(code, _st(), OX)
        assert by_number == by_code

    def test_offer_menu_shows_every_current_price_and_no_obsolete_one(self, seeded):
        with _APP.app_context():
            text, preset = oh.offer_menu_reply(OX)
        for code, (price, title, old, _legacy) in PAYABLE.items():
            assert price in text, code
            assert title in text, code
            assert old not in text, code
        assert preset == "OFFER"

    def test_offer_menu_keeps_the_constants_menu_positions(self, seeded):
        """OFFER_MENU is still the offer SET and still fixes 1-2-3-4."""
        with _APP.app_context():
            text, _ = oh.offer_menu_reply(OX)
        seen = [text.index(PAYABLE[c][1])
                for c in ("CWPDE", "DCA", "AIDM", "PGDCA")]
        assert seen == sorted(seen)

    def test_no_offer_price_is_invented_where_the_catalogue_defines_none(self, seeded):
        """Offer semantics preserved rather than assumed: RC2.5.5c-2 authored
        `offers: []` for every course, so there is no temporary offer amount
        to display. The menu therefore shows the NORMAL catalogue price -- and
        must not resurrect OFFER_MENU's obsolete column and call it an offer.
        """
        with _APP.app_context():
            assert all(cat.get_course(OX, c).offers == () for c in PAYABLE)
            text, _ = oh.offer_menu_reply(OX)
        for code, (price, _t, old, _l) in PAYABLE.items():
            assert price in text and old not in text

    def test_offer_menu_does_not_dead_end_when_nothing_is_on_offer(self, seeded):
        """A tenant none of whose courses appear in OFFER_MENU used to be
        answered with Oxford's four. It must now be told there is no offer,
        not asked for a course number under an empty list."""
        with _APP.app_context():
            text, preset = oh.offer_menu_reply(B)
        assert preset == "OFFER"
        assert "course number reply cheyyoo" not in text
        assert "COURSES" in text


# 9 -- tenant isolation across the whole fix ---------------------------------

class TestTenantIsolationAcrossTheFix:

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_tenant_b_gets_no_oxford_link_and_no_oxford_price(self, seeded, code):
        price, title, old, _legacy = PAYABLE[code]
        st = _st(title)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", title, st, B)
        assert PAID[code] not in text
        assert price not in text and old not in text
        assert st["stage"] != "payment_pending"

    def test_tenant_b_offer_menu_carries_no_oxford_content(self, seeded):
        with _APP.app_context():
            text, _ = oh.offer_menu_reply(B)
        for code, (price, title, old, _legacy) in PAYABLE.items():
            assert price not in text and title not in text and old not in text
            assert PAID[code] not in text

    @pytest.mark.parametrize("code", sorted(PAYABLE))
    def test_tenant_b_cannot_select_an_oxford_offer(self, seeded, code):
        st = _st()
        with _APP.app_context():
            assert oh.handle_offer(code, st, B) is None
        assert st["stage"] != "payment_pending"

    def test_tenant_b_pay_intent_falls_through_to_its_own_menu(self, seeded):
        st = _st("Beta Yoga Foundation")
        with _APP.app_context():
            text, _ = oh.handle_pay_intent(st, B)
        assert not any(u in text for u in PAID.values())
        assert st["stage"] == "offer_menu"


# 10 -- static guards on the customer-facing payment CTAs --------------------

_CTA_PATHS = [("app/bot/cta_handlers.py", "enroll_reply"),
              ("app/bot/offer_handlers.py", "handle_pay_intent"),
              ("app/bot/offer_handlers.py", "handle_offer"),
              ("app/bot/offer_handlers.py", "offer_menu_reply")]


def _fn(rel, name):
    tree = ast.parse(_src(rel))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


class TestSourceGuardsOnThePaymentCtas:

    @pytest.mark.parametrize("rel,name", _CTA_PATHS)
    @pytest.mark.parametrize("const", ["COURSE_PAYMENT_LINKS", "COURSE_FEES",
                                       "FULL_FEE_TABLE", "ALL_COURSES",
                                       "GOAL_COURSES"])
    def test_no_old_price_constant_is_read(self, rel, name, const):
        fn = _fn(rel, name)
        assert const not in {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}

    @pytest.mark.parametrize("rel,name", _CTA_PATHS)
    def test_no_inline_rupee_price_literal(self, rel, name):
        """Not just the constants: the same obsolete numbers were also typed
        into the source as literals."""
        fn = _fn(rel, name)
        bad = [n.value for n in ast.walk(fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and re.search(r"\u20b9\s?\d", n.value)]
        assert bad == [], bad

    @pytest.mark.parametrize("rel", ["app/bot/cta_handlers.py",
                                     "app/bot/offer_handlers.py"])
    def test_module_no_longer_imports_the_payment_link_catalogue(self, rel):
        """Structural, not per-function: the constant is not in scope at all."""
        tree = ast.parse(_src(rel))
        imported = {a.asname or a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "COURSE_PAYMENT_LINKS" not in imported

    @pytest.mark.parametrize("name", ["offer_menu_reply", "handle_offer"])
    def test_offer_paths_read_only_the_code_column(self, name):
        """OFFER_MENU stays the offer SET and its positions. Reading any
        further column is reading the obsolete title/price/duration again."""
        fn = _fn("app/bot/offer_handlers.py", name)
        idx = [n.slice.value for n in ast.walk(fn)
               if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
               and isinstance(n.value, ast.Name) and n.value.id == "entry"]
        assert idx and set(idx) == {0}, idx

    @pytest.mark.parametrize("rel,name", [("app/bot/cta_handlers.py", "enroll_reply"),
                                          ("app/bot/offer_handlers.py",
                                           "handle_pay_intent")])
    def test_stored_course_state_is_resolved_through_the_catalogue(self, rel, name):
        fn = _fn(rel, name)
        called = {n.func.attr for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "resolve_legacy_name" in called

    @pytest.mark.parametrize("rel,name", _CTA_PATHS[:3])
    def test_payment_url_still_comes_from_the_resolver_keyed_by_code(self, rel, name):
        fn = _fn(rel, name)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "resolve_payment_url"]
        assert len(calls) == 1
        arg = calls[0].args[1]
        # tenant_id, then a `.code` attribute or the offer's code variable --
        # never a course name.
        assert (isinstance(arg, ast.Attribute) and arg.attr == "code") or \
               (isinstance(arg, ast.Name) and arg.id.endswith("code"))

    def test_payment_link_reply_signature_is_unchanged(self):
        fn = _fn("app/bot/cta_handlers.py", "payment_link_reply")
        assert [a.arg for a in fn.args.args] == ["code", "full_name", "price",
                                                 "dur", "link"]
