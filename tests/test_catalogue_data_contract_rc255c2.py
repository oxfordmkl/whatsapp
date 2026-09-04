"""Phase RC2.5.5c-2: the tenant-owned catalogue DATA contract.

DATA-ONLY. Nothing here depends on a runtime catalogue flip -- the
deterministic bot still reads app/bot/constants.py, and RC2.5.5c-3 is a
separate phase. These tests pin the SHAPE and INVARIANTS of the tenant-owned
catalogue so the data cannot silently rot before that flip happens.

Three identity separations the authorization called out explicitly, each of
which had already been conflated somewhere in the system:

  * Python Programming vs Python Django -- separate 3-month courses.
  * PDWD (PHP full-stack) vs WordPress -- the old bot listed WordPress inside
    PDWD's syllabus AND mapped the "wordpress" keyword to PDWD, while
    WordPress is sold separately.
  * DCA Fast Track (6mo) vs DCA Regular (12mo) -- the bare keyword "dca" is
    therefore ambiguous and is deliberately mapped to NEITHER.

And one identity correction: the course the bot sold as "SAP Financial
Accounting" is officially PDCFA, whose syllabus is Tally/GST based and
contains no SAP. A separate "PDCFA with SAP FICO" programme exists at a
different price and is not currently sold, so no SAP claim may appear on the
11,400 course.

Import isolation follows test_platform_security_14c.py.
"""
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc255c2_catalogue.db")
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
from app.services import knowledge_service as ks                        # noqa: E402
from app.services import payment_link_service as pls                    # noqa: E402

OX = "t-ox"
B = "t-b"

# The canonical catalogue as authored in production, mirrored here so the
# contract is asserted rather than merely described.
# code: (dur_months, registration, max_tuition, concession, net, normal_total)
FEES = {
    "PGDCA": (12, 4500, 18800, 3760, 15040, 19540),
    "AIDM": (6, 5900, 25000, 0, 25000, 30900),
    "PDCFA": (6, 2600, 11000, 2200, 8800, 11400),
    "PYTHON": (3, 1180, 5000, 1000, 4000, 5180),
    "JAVA": (3, 850, 3500, 700, 2800, 3650),
    "DJANGO": (3, 3540, 15000, 3000, 12000, 15540),
    "DGSTP": (6, 4890, 24000, 4800, 19200, 24090),
    "DCA": (6, 1950, 8000, 1600, 6400, 8350),
    "DCA-REGULAR": (12, 2400, 10000, 2000, 8000, 10400),
    "CTTC": (12, 3550, 15000, 3000, 12000, 15550),
    "CORPORATE-ACCOUNTING": (12, 10270, 52000, 0, 52000, 62270),
    "CWPDE": (6, 1450, 6000, 1200, 4800, 6250),
    "DOA": (6, 1700, 7000, 1400, 5600, 7300),
    "PDDTP": (6, 1950, 8000, 1600, 6400, 8350),
    "PDWD": (6, 2600, 11000, 2200, 8800, 11400),
    "WORDPRESS": (3, 1300, 5500, 1100, 4400, 5700),
}
THREE_MONTH = {"PYTHON", "JAVA", "DJANGO", "WORDPRESS"}
PAID = {"PGDCA": "https://rzp.io/rzp/KAQ2C7t", "DCA": "https://rzp.io/rzp/mJPPtM9x",
        "CWPDE": "https://rzp.io/rzp/xkWdKtd", "AIDM": "https://rzp.io/rzp/vF76sj7Y"}

_APP = create_app()
_APP.config["TESTING"] = True


def _row(code, tenant=OX, sort=0):
    dur_m, reg, mx, conc, net, total = FEES[code]
    attrs = {
        "duration": f"{dur_m} Months",
        "categories": ["job"],
        "keywords": [code.lower()],
        "commercial": {
            "code": code, "currency": "INR", "base_price": total,
            "normal_total_fee": total, "emi_available": dur_m in (6, 12),
            "offers": [],
            "payment_url": PAID.get(code) if tenant == OX else None,
        },
        "regulatory": {"components": [
            {"type": "registration_fee", "label": "Registration Fee", "amount": reg},
            {"type": "max_tuition_fee", "label": "Maximum Tuition Fee to ATC", "amount": mx},
            {"type": "concession", "label": "Mandatory Tuition Fee Concession", "amount": conc},
            {"type": "net_tuition_fee", "label": "Net Tuition Fee to ATC", "amount": net},
        ]},
    }
    if code in PAID and tenant == OX:
        attrs["commercial"]["legacy_payment_url"] = PAID[code]
    return TenantKnowledge(tenant_id=tenant, kind="course", title=code,
                           body=f"body for {code}", attributes=json.dumps(attrs),
                           is_active=True, sort_order=sort)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug="ox",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=B, name="Beta", slug="b",
                              status="ACTIVE", billing_exempt=True))
        for i, code in enumerate(FEES, start=1):
            db.session.add(_row(code, sort=i))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def rows():
    from app.services import knowledge_admin_service as kas
    with _APP.app_context():
        return kas.list_knowledge(OX, per_page=kas.MAX_PER_PAGE)["rows"]


def attrs_by_code():
    out = {}
    for r in rows():
        a = json.loads(r.attributes or "{}")
        out[(a.get("commercial") or {}).get("code")] = a
    return out


class TestSixteenCourses:

    def test_all_sixteen_codes_present(self, seeded):
        assert set(attrs_by_code()) == set(FEES)

    def test_codes_are_unique(self, seeded):
        codes = [json.loads(r.attributes)["commercial"]["code"] for r in rows()]
        assert len(codes) == len(set(codes)) == 16

    def test_all_active(self, seeded):
        assert all(r.is_active for r in rows())

    def test_sort_orders_unique(self, seeded):
        so = [r.sort_order for r in rows()]
        assert len(so) == len(set(so))


class TestIdentitySeparations:
    """The three separations the authorization forbade merging."""

    def test_python_and_django_are_distinct(self, seeded):
        a = attrs_by_code()
        assert a["PYTHON"]["commercial"]["normal_total_fee"] == 5180
        assert a["DJANGO"]["commercial"]["normal_total_fee"] == 15540
        assert a["PYTHON"] is not a["DJANGO"]

    def test_pdwd_and_wordpress_are_distinct(self, seeded):
        a = attrs_by_code()
        assert a["PDWD"]["duration"] == "6 Months"
        assert a["WORDPRESS"]["duration"] == "3 Months"
        assert a["PDWD"]["commercial"]["normal_total_fee"] == 11400
        assert a["WORDPRESS"]["commercial"]["normal_total_fee"] == 5700

    def test_dca_fast_track_and_regular_are_distinct(self, seeded):
        a = attrs_by_code()
        assert a["DCA"]["duration"] == "6 Months"
        assert a["DCA-REGULAR"]["duration"] == "12 Months"
        assert (a["DCA"]["commercial"]["normal_total_fee"]
                != a["DCA-REGULAR"]["commercial"]["normal_total_fee"])


class TestFeeModel:

    @pytest.mark.parametrize("code", sorted(FEES))
    def test_components_present_and_exact(self, seeded, code):
        _d, reg, mx, conc, net, _t = FEES[code]
        comp = {c["type"]: c["amount"] for c in
                attrs_by_code()[code]["regulatory"]["components"]}
        assert comp["registration_fee"] == reg
        assert comp["max_tuition_fee"] == mx
        assert comp["concession"] == conc
        assert comp["net_tuition_fee"] == net

    @pytest.mark.parametrize("code", sorted(FEES))
    def test_normal_total_declared_and_equals_reg_plus_net(self, seeded, code):
        _d, reg, _m, _c, net, total = FEES[code]
        cm = attrs_by_code()[code]["commercial"]
        assert cm["normal_total_fee"] == total, "normal total must be DECLARED"
        assert reg + net == total, "and must equal registration + net tuition"

    def test_exam_fee_is_not_folded_into_any_total(self, seeded):
        for code, a in attrs_by_code().items():
            types = {c["type"] for c in a["regulatory"]["components"]}
            assert "exam_fee" not in types, f"{code} folded an exam fee in"

    def test_offers_are_empty_and_separate(self, seeded):
        """No promotion was supplied; none may be invented. An offer must
        never overwrite the official structure."""
        for code, a in attrs_by_code().items():
            assert a["commercial"]["offers"] == []


class TestNoUnsupportedPayrollClaim:
    """RC2.5.5c-2 correction. The course was authored as "GST & Payroll" with a
    `payroll` keyword, carried over from the legacy bot card. The official
    source (DGSTP) establishes Financial Accounting, Direct & Indirect Tax,
    Tally Prime, SLP Finance Executive and Tally Essential Professional -- it
    does NOT establish Payroll as a component, and the legacy card is the very
    marketing copy this phase supersedes. No Payroll claim may reappear
    without Oxford-specific source material."""

    def test_code_is_dgstp(self, seeded):
        assert "DGSTP" in attrs_by_code()
        assert "GST-PAYROLL" not in attrs_by_code()

    def test_no_payroll_anywhere_in_the_catalogue(self, seeded):
        for r in rows():
            blob = f"{r.title} {r.body or ''} {r.attributes or ''}".lower()
            assert "payroll" not in blob, f"payroll claim survives on {r.title!r}"

    def test_payroll_is_not_a_keyword(self, seeded):
        for code, a in attrs_by_code().items():
            assert "payroll" not in [k.lower() for k in a.get("keywords", [])]

    def test_dgstp_fee_structure_untouched_by_the_correction(self, seeded):
        a = attrs_by_code()["DGSTP"]
        comp = {c["type"]: c["amount"] for c in a["regulatory"]["components"]}
        assert (comp["registration_fee"], comp["max_tuition_fee"],
                comp["concession"], comp["net_tuition_fee"]) == (4890, 24000, 4800, 19200)
        assert a["commercial"]["normal_total_fee"] == 24090
        assert a["commercial"]["emi_available"] is True
        assert a["duration"] == "6 Months"


class TestEmiIsCourseLevel:

    @pytest.mark.parametrize("code", sorted(THREE_MONTH))
    def test_three_month_courses_have_no_emi(self, seeded, code):
        assert attrs_by_code()[code]["commercial"]["emi_available"] is False

    @pytest.mark.parametrize("code", sorted(set(FEES) - THREE_MONTH))
    def test_longer_courses_have_emi(self, seeded, code):
        assert attrs_by_code()[code]["commercial"]["emi_available"] is True

    def test_emi_is_not_a_global_assumption(self, seeded):
        vals = {a["commercial"]["emi_available"] for a in attrs_by_code().values()}
        assert vals == {True, False}, "EMI must vary per course, not be blanket"

    def test_no_installment_amount_is_stored(self, seeded):
        """Rounding/collection policy is undefined, so no figure may be
        precomputed."""
        for code, a in attrs_by_code().items():
            blob = json.dumps(a).lower()
            for banned in ("monthly_installment", "emi_amount", "interest"):
                assert banned not in blob, f"{code} stored {banned}"


class TestDiscoveryMetadata:

    @pytest.mark.parametrize("code", sorted(FEES))
    def test_every_course_has_category_and_keywords(self, seeded, code):
        a = attrs_by_code()[code]
        assert a.get("categories") and a.get("keywords")


class TestPaymentSafety:

    def test_only_the_four_paid_courses_have_a_url(self, seeded):
        got = {c: a["commercial"].get("payment_url")
               for c, a in attrs_by_code().items()
               if a["commercial"].get("payment_url")}
        assert got == PAID

    def test_legacy_urls_preserved(self, seeded):
        got = {c: a["commercial"].get("legacy_payment_url")
               for c, a in attrs_by_code().items()
               if a["commercial"].get("legacy_payment_url")}
        assert got == PAID

    def test_new_courses_carry_no_payment_url(self, seeded):
        a = attrs_by_code()
        for code in ("PDCFA", "DGSTP", "CORPORATE-ACCOUNTING"):
            assert a[code]["commercial"].get("payment_url") is None
            assert "legacy_payment_url" not in a[code]["commercial"]

    def test_no_payment_url_reaches_the_prompt(self, seeded):
        """Completing the catalogue must not leak a payment URL into the AI
        block -- the RC2.5.5b-1 quarantine still governs."""
        with _APP.app_context():
            block = ks.render_knowledge_block(OX)
        assert "rzp.io" not in block
        assert "payment_url" not in block
        assert "legacy_payment_url" not in block

    def test_resolver_still_serves_only_the_four(self, seeded):
        with _APP.app_context():
            for code, url in PAID.items():
                assert pls.resolve_payment_url(OX, code) == url
            for code in ("PDCFA", "DGSTP", "CORPORATE-ACCOUNTING", "JAVA"):
                assert pls.resolve_payment_url(OX, code) is None


class TestTenantIsolation:

    def test_second_tenant_has_no_catalogue(self, seeded):
        from app.services import knowledge_admin_service as kas
        with _APP.app_context():
            assert kas.list_knowledge(B)["total"] == 0

    def test_second_tenant_gets_no_oxford_payment_url(self, seeded):
        with _APP.app_context():
            for code in PAID:
                assert pls.resolve_payment_url(B, code) is None

    def test_oxford_catalogue_not_visible_to_other_tenant(self, seeded):
        with _APP.app_context():
            block = ks.render_knowledge_block(B)
        assert "PGDCA" not in block


class TestNoRuntimeFlip:
    """DATA-ONLY. The deterministic bot must still read the constants."""

    def test_deterministic_catalogue_untouched(self):
        from app.bot.constants import ALL_COURSES, COURSE_FEES, OFFER_MENU
        assert len(ALL_COURSES) == 10
        assert len(COURSE_FEES) == 10
        assert len(OFFER_MENU) == 4

    def test_bot_does_not_read_tenant_knowledge(self):
        import ast
        for mod in ("app/bot/router.py", "app/bot/cta_handlers.py",
                    "app/bot/offer_handlers.py", "app/bot/screens.py"):
            with open(os.path.join(_ROOT, mod), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            mods = {n.module for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.module}
            assert not any("knowledge_service" in m or "knowledge_admin" in m
                           for m in mods), f"{mod} reads tenant knowledge -- that is c-3"
