"""Phase RC2.5.4c-x: catalogue reads `base_price` when `normal_total_fee` is absent.

THE DEFECT
----------
Two halves of the system were never reconciled:

    RC2.5.4b course admin UI  WRITES  commercial.base_price
    RC2.5.5c-3 catalogue      READS   commercial.normal_total_fee

`grep` across the whole admin surface -- knowledge_admin_service, the tenant
routes, the course form -- finds no write of normal_total_fee anywhere. So a
course CREATED through the admin UI had no normal_total_fee at all and its
price was invisible to every deterministic customer-facing path; and a
RC2.5.5c-2 course EDITED through the admin UI got a new base_price while its
original normal_total_fee survived, which is why the admin saw the new price
and the customer kept being quoted the old one.

Found in production on FST01 (RC2.5.4c post-deployment audit):

    normal_total_fee : None      <- what customers read
    base_price       : 37000     <- what the admin edited
    catalogue index  : "FST01 | full stack web development | 12 Months | No EMI"
                                 <- no fee at all

PRECEDENCE IS ONE-DIRECTIONAL
------------------------------
An existing normal_total_fee is authoritative and base_price NEVER overrides
it. The two disagree on precisely the rows an admin has edited, and silently
preferring base_price would re-price the sixteen courses RC2.5.5c-2 authored
-- the same "a refactor must not change what a business charges" rule that
governed RC2.5.5b-2. test_base_price_never_overrides_normal_total_fee is the
assertion that holds that line.

READ-SIDE ONLY: nothing is stored, nothing is migrated, no row is modified.

OUT OF SCOPE: the admin write path, any backfill, payment behaviour, and the
separate keyword defect the same audit found
(match_keyword("full stack web development") -> PDWD), which is recorded as
FOLLOW-UP -- COURSE KEYWORD RESOLUTION / FST01 and is NOT addressed here.
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

_DB = os.path.join(tempfile.gettempdir(), "rc254cx_price_fallback.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx-broadcast-key")
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
from app.bot import cta_handlers as cta, screens                        # noqa: E402

OX = "t-ox"
B = "t-b"

_APP = create_app()
_APP.config["TESTING"] = True


def _row(tenant, code, title, *, normal=None, base=None, duration="12 Months",
         url=None, sort=1, cats=("job",)):
    """One TenantKnowledge row with either/both price keys, as production has."""
    commercial = {"code": code, "currency": "INR", "offers": []}
    if normal is not None:
        commercial["normal_total_fee"] = normal
    if base is not None:
        commercial["base_price"] = base
    if url:
        commercial["payment_url"] = url
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title, body=f"About {title}.",
        attributes=json.dumps({"duration": duration, "categories": list(cats),
                               "commercial": commercial}),
        is_active=True, sort_order=sort)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid in (OX, B):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        # 1. an RC2.5.5c-2 course: BOTH keys, agreeing (the backfill shape)
        db.session.add(_row(OX, "PGDCA", "PGDCA – Computer Applications",
                            normal=19540, base=19540,
                            url="https://rzp.io/rzp/KAQ2C7t", sort=1))
        # 2. a c-2 course EDITED through the admin UI: base_price updated,
        #    normal_total_fee left behind. THE stale-price shape.
        db.session.add(_row(OX, "AIDM", "AIDM – Digital Marketing",
                            normal=30900, base=31500, sort=2))
        # 3. a course CREATED through the admin UI: base_price only (FST01)
        db.session.add(_row(OX, "FST01", "full stack web development",
                            base=37000, sort=3))
        # 4. a course with neither price key
        db.session.add(_row(OX, "FREE01", "Orientation Session", sort=4))
        # 5. tenant B, its own course, base_price only
        db.session.add(_row(B, "BETA1", "Beta Yoga Foundation", base=1234, sort=1))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ 1, 4, 5 + the negative: precedence is one-directional ════════════════

class TestPrecedence:

    def test_normal_total_fee_wins_when_present(self, seeded):
        with _APP.app_context():
            rec = cat.get_course(OX, "PGDCA")
        assert rec.normal_total_fee == 19540

    def test_base_price_never_overrides_normal_total_fee(self, seeded):
        """THE negative assertion. AIDM carries normal_total_fee=30900 and a
        newer base_price=31500. Preferring base_price would silently re-price
        a course RC2.5.5c-2 deliberately authored -- a refactor must not
        change what a business charges."""
        with _APP.app_context():
            rec = cat.get_course(OX, "AIDM")
        assert rec.normal_total_fee == 30900, "base_price overrode the authored fee"
        assert rec.normal_total_fee != 31500

    def test_existing_oxford_value_is_unchanged(self, seeded):
        with _APP.app_context():
            fees = {c.code: c.normal_total_fee for c in cat.list_courses(OX)}
        assert fees["PGDCA"] == 19540
        assert fees["AIDM"] == 30900

    def test_tenant_owned_values_survive_the_shim(self, seeded):
        """Every course that already had a fee keeps exactly that fee."""
        with _APP.app_context():
            for code, expected in (("PGDCA", 19540), ("AIDM", 30900)):
                assert cat.get_course(OX, code).normal_total_fee == expected


# ═══ 2, 6: the admin-created / admin-edited course now has a price ════════

class TestBasePriceFallback:

    def test_base_price_used_when_normal_total_fee_absent(self, seeded):
        with _APP.app_context():
            rec = cat.get_course(OX, "FST01")
        assert rec is not None
        assert rec.normal_total_fee == 37000

    def test_the_fallback_reads_and_stores_nothing(self, seeded):
        """Read-side only: the row on disk must still have no
        normal_total_fee after the record is built."""
        with _APP.app_context():
            cat.get_course(OX, "FST01")
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="full stack web development").one()
            commercial = json.loads(row.attributes)["commercial"]
        assert "normal_total_fee" not in commercial
        assert commercial["base_price"] == 37000

    def test_an_edited_base_price_is_what_the_customer_sees(self, seeded):
        """The production symptom, inverted: edit base_price, and the value
        the customer is quoted moves with it."""
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="full stack web development").one()
            attrs = json.loads(row.attributes)
            attrs["commercial"]["base_price"] = 41000
            row.attributes = json.dumps(attrs)
            db.session.commit()
            rec = cat.get_course(OX, "FST01")
            fees, _ = cta.fees_reply("full stack web development", OX)
        assert rec.normal_total_fee == 41000
        assert "41,000" in fees


# ═══ 3: absent stays absent ═══════════════════════════════════════════════

class TestNoFeeStaysNoFee:

    def test_both_absent_invents_nothing(self, seeded):
        with _APP.app_context():
            rec = cat.get_course(OX, "FREE01")
        assert rec is not None, "the course itself must still resolve"
        assert rec.normal_total_fee is None

    def test_no_fee_course_renders_no_amount(self, seeded):
        with _APP.app_context():
            idx, _ = cat.catalogue_index_with_provenance(OX)
        line = next(e for e in idx if e.startswith("FREE01"))
        assert "Total fee" not in line
        assert cat.format_money(None) == ""


# ═══ 7, 8: the deterministic paths and the index use the fallback ═════════

class TestDeterministicOutput:

    def test_fees_reply_quotes_the_fallback_value(self, seeded):
        with _APP.app_context():
            fees, preset = cta.fees_reply("full stack web development", OX)
        assert preset == "FEES"
        assert "37,000" in fees

    def test_course_details_quotes_the_fallback_value(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("FST01", OX)
        assert screen is not None and "37,000" in screen.body

    def test_catalogue_index_carries_the_fallback_value(self, seeded):
        with _APP.app_context():
            idx, _ = cat.catalogue_index_with_provenance(OX)
        line = next(e for e in idx if e.startswith("FST01"))
        assert "Total fee ₹37,000" in line

    def test_whole_catalogue_listing_includes_it(self, seeded):
        with _APP.app_context():
            fees, _ = cta.fees_reply("", OX)
        assert "37,000" in fees and "19,540" in fees

    def test_formatting_still_goes_through_format_money(self, seeded):
        """RC2.5.5c-6a must not regress: the fallback value is formatted, not
        interpolated raw."""
        with _APP.app_context():
            idx, _ = cat.catalogue_index_with_provenance(OX)
        line = next(e for e in idx if e.startswith("FST01"))
        assert "Total fee 37000" not in line
        assert "₹37,000" in line


# ═══ 9, 10: payment and isolation unchanged ═══════════════════════════════

class TestBoundariesUnchanged:

    def test_payment_url_behaviour_is_untouched(self, seeded):
        with _APP.app_context():
            assert pls.resolve_payment_url(OX, "PGDCA") == "https://rzp.io/rzp/KAQ2C7t"
            assert pls.resolve_payment_url(OX, "FST01") is None
            assert pls.resolve_payment_url(None, "PGDCA") is None
            assert pls.resolve_payment_url(B, "PGDCA") is None

    def test_a_fallback_price_does_not_create_a_payment_link(self, seeded):
        """FST01 now has a displayable price but still no payment URL: the
        fee shim must not make a course payable."""
        st = {"course": "full stack web development", "stage": "start",
              "offer_course": ""}
        with _APP.app_context():
            text, _ = cta.enroll_reply("Cust", st["course"], st, OX)
        assert "rzp.io" not in text
        assert st["stage"] != "payment_pending"

    def test_tenant_isolation_holds_for_fallback_priced_courses(self, seeded):
        """Both tenants have a base_price-only course. Neither may see the
        other's, and neither may see the other's price."""
        with _APP.app_context():
            assert cat.get_course(B, "FST01") is None
            assert cat.get_course(OX, "BETA1") is None
            beta = cat.get_course(B, "BETA1")
            ox_fees, _ = cta.fees_reply("", OX)
        assert beta is not None and beta.normal_total_fee == 1234
        assert "1,234" not in ox_fees, "tenant B's price leaked into Oxford"


# ═══ source guard ═════════════════════════════════════════════════════════

class TestSourceContract:

    def test_the_fallback_is_read_only(self):
        """The shim must not write, commit or mutate the row."""
        import ast
        src = open(os.path.join(_ROOT, "app/services/catalogue_service.py"),
                   encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_record_from_row")
        calls = {getattr(n.func, "attr", getattr(n.func, "id", None))
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        for forbidden in ("commit", "add", "flush", "merge", "delete"):
            assert forbidden not in calls, f"_record_from_row calls {forbidden}()"
