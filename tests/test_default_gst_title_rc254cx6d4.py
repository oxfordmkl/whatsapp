"""RC2.5.4c-x-6d4 — the platform-default course 5 is "GST & Taxation", not
"GST & Payroll".

WHY
---
The x-6d4 audit found "Payroll" in the default course-5 TITLE (ALL_COURSES)
and as a discovery keyword, with no source behind it: RC2.5.5c-2 established
from the official course pages that the GST course does not include Payroll,
and x-6d1 had already banned the word from every card body. The card's own
heading has always been "Diploma in GST & Taxation", so the new title shows
the customer nothing the same screen did not already say.

The same audit found a silent hazard. _default_catalogue() reads price and
duration with COURSE_FEES.get(title, ("", "")), keyed by the TITLE. Rename a
title without its COURSE_FEES key and every surface renders a blank price --
"Course Fee: **" -- with no exception and no log line. TestFeeKeyInvariant
makes that state impossible to commit.

TWO DISTINCT COURSES -- deliberately NOT reconciled
---------------------------------------------------
  default course "5"  GST & Taxation                         18,999  no EMI
  Oxford "DGSTP"      DGSTP – Goods & Services Tax Practice  24,090  EMI

The default course is not DGSTP and must never become it: DGSTP's code,
price, EMI, syllabus and accreditation belong to one tenant.
LEGACY_NAME_TO_CODE keeps "gst & payroll" -> DGSTP for Oxford's historical
conversations and COURSE_NAME_ALIASES keeps its historical CRM target;
neither is changed.

UNCHANGED ON PURPOSE: code "5", catalogue position, price, duration, the
_GST card, GOAL_COURSES (only its index column is read at runtime) and
FULL_FEE_TABLE (dormant -- nothing imports it).
"""
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6d4_gst.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6d4-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6d4-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6d4-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "")

for _m in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.bot import constants as K                                      # noqa: E402
from app.bot import cta_handlers as cta, navigation as nav              # noqa: E402
from app.bot import router, screens                                     # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.services import prompt_composer                                # noqa: E402

OX = "t-ox"
BARE = "t-bare"          # authored nothing -> receives the default catalogue

_APP = create_app()
_APP.config["TESTING"] = True

CODE = "5"
NEW_TITLE = "GST & Taxation"
OLD_TITLE = "GST & Payroll"
FEE = "₹18,999"
DURATION = "6 Months"

# Every default title. Only "5" changed in x-6d4.
EXPECTED_TITLES = {
    "1": "PGDCA", "2": "AIDM Digital Marketing", "3": "SAP Financial Accounting",
    "4": "Python Programming", "5": NEW_TITLE, "6": "DCA Fast Track",
    "7": "Computer Teacher Training", "8": "Corporate Business Accounting",
    "9": "Word Processing & Data Entry", "10": "Professional Web Designing",
}

# The WHOLE keyword table: the pre-x-6d4 table minus "payroll" and nothing
# else. Pinned entire so a new keyword, a dropped gst/tally/taxation or a
# re-pointed unrelated keyword all fail here.
EXPECTED_KEYWORDS = {
    "pgdca": "1", "pgd": "1",
    "aidm": "2", "digital marketing": "2", "digital": "2",
    "sap": "3", "erp": "3",
    "python": "4", "programming": "4", "coding": "4",
    "gst": "5", "tally": "5", "taxation": "5",
    "dca": "6", "fast track": "6",
    "teacher": "7", "teaching": "7",
    "accounting": "8", "corporate": "8",
    "data entry": "9", "typing": "9", "word processing": "9",
    "web": "10", "web design": "10", "wordpress": "10", "html": "10",
}

# The _GST card, byte for byte. x-6d4 renames the structured title only.
GST_CARD = (
    "\U0001f4da *Diploma in GST & Taxation*\n"
    "\U0001f4bc Best for: Accounting professionals & commerce students\n"
    "\U0001f31f High demand skill — GST expertise needed across all businesses\n"
    "⏱ Duration: 6 Months\n"
    "\U0001f4bb Syllabus: GST Concepts, Income Tax, Tally Prime, E-filing\n"
    "\U0001f4b0 Course Fee\n"
    "   ₹18,999"
)

# GOAL_COURSES is left untouched: only its index column is read at runtime.
EXPECTED_ACCOUNTING_GOAL = [
    ("3", "SAP Financial Accounting", "6 Months", "₹15,000"),
    ("5", "GST & Payroll Diploma", "6 Months", "₹18,999"),
    ("8", "Corporate Business Accounting", "1 Year", "₹40,000"),
]

INDEX_LINE = f"{CODE} | {NEW_TITLE} | {DURATION} | Total fee {FEE} | No EMI"


def _fee_key_gaps(all_courses, course_fees):
    """ALL_COURSES titles with no EXACT key in COURSE_FEES, sorted.

    Exact dict membership on purpose: _default_catalogue() does an exact,
    case-sensitive COURSE_FEES.get(title, ...). Any normalisation here --
    lower(), strip() -- would approve a title the runtime then prices as
    blank."""
    return sorted(title for title, _card in all_courses.values()
                  if title not in course_fees)


def _default_record(code=CODE):
    return next(r for r in cat._default_catalogue() if r.code == code)


def _rows(screen):
    return [r for s in screen.sections for r in s.rows]


def _text(out):
    return out[0] if isinstance(out, tuple) else getattr(out, "body", str(out))


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid in (OX, BARE):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        # Oxford's authored DGSTP, in its production shape. A DISTINCT course
        # from default "5": x-6d4 must neither reach it nor imitate it.
        db.session.add(TenantKnowledge(
            tenant_id=OX, kind="course",
            title="DGSTP – Goods & Services Tax Practice",
            body="\U0001f4da *Diploma in Goods and Services Tax Practice*",
            attributes=json.dumps({
                "duration": "6 Months", "categories": ["accounting"],
                "keywords": ["gst", "taxation", "tally"],
                "commercial": {"code": "DGSTP", "currency": "INR",
                               "normal_total_fee": 24090, "base_price": 24090,
                               "emi_available": True, "offers": [],
                               "payment_url": None},
                "regulatory": {"components": [
                    {"type": "registration_fee", "amount": 4890},
                    {"type": "net_tuition_fee", "amount": 19200}]}}),
            is_active=True, sort_order=7))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ 1 — the ALL_COURSES <-> COURSE_FEES key invariant ═════════════════════

class TestFeeKeyInvariant:

    def test_every_all_courses_title_has_a_course_fees_key(self):
        for idx, (title, _card) in K.ALL_COURSES.items():
            assert title in K.COURSE_FEES, (
                f"ALL_COURSES[{idx!r}] title {title!r} has no exact COURSE_FEES "
                f"key -- the default catalogue would render a BLANK price")
        assert _fee_key_gaps(K.ALL_COURSES, K.COURSE_FEES) == []

    def test_no_course_fees_key_is_orphaned(self):
        """The reverse direction: a key left behind by a rename is the other
        half of a half-applied rename."""
        titles = {title for title, _card in K.ALL_COURSES.values()}
        assert set(K.COURSE_FEES) == titles, (
            f"orphaned: {sorted(set(K.COURSE_FEES) - titles)}; "
            f"unpriced: {sorted(titles - set(K.COURSE_FEES))}")

    def test_every_default_record_is_actually_priced(self):
        """The runtime consequence, independent of how the tables are read."""
        recs = cat._default_catalogue()
        assert len(recs) == 10
        for r in recs:
            assert isinstance(r.normal_total_fee, str) and r.normal_total_fee.startswith("₹"), (
                f"default course {r.code} ({r.title}) has a blank price")
            assert r.duration, f"default course {r.code} ({r.title}) has a blank duration"


class TestFeeKeyInvariantBites:
    """The invariant's own checker must reject every way the tables can
    drift. If it is weakened -- case-folded, stripped, or short-circuited --
    these fail even while the live tables happen to be consistent."""

    @staticmethod
    def _tables():
        return dict(K.ALL_COURSES), dict(K.COURSE_FEES)

    def test_detects_title_renamed_without_fee_key(self):
        a, f = self._tables()
        a[CODE] = ("Something Else", a[CODE][1])
        assert _fee_key_gaps(a, f) == ["Something Else"]

    def test_detects_fee_key_renamed_without_title(self):
        a, f = self._tables()
        f["Something Else"] = f.pop(NEW_TITLE)
        assert _fee_key_gaps(a, f) == [NEW_TITLE]

    def test_detects_case_difference(self):
        a, f = self._tables()
        f[NEW_TITLE.lower()] = f.pop(NEW_TITLE)
        assert _fee_key_gaps(a, f) == [NEW_TITLE]

    def test_detects_whitespace_difference(self):
        a, f = self._tables()
        f[NEW_TITLE + " "] = f.pop(NEW_TITLE)
        assert _fee_key_gaps(a, f) == [NEW_TITLE]

    def test_detects_an_unrelated_missing_key(self):
        a, f = self._tables()
        del f["PGDCA"]
        assert _fee_key_gaps(a, f) == ["PGDCA"]

    def test_invariant_tests_are_present(self):
        """Deleting the invariant test outright must not pass silently."""
        for name in ("test_every_all_courses_title_has_a_course_fees_key",
                     "test_no_course_fees_key_is_orphaned",
                     "test_every_default_record_is_actually_priced"):
            assert callable(getattr(TestFeeKeyInvariant, name, None)), name


# ═══ 2 — course 5's identity ═══════════════════════════════════════════════

class TestCourseFiveIdentity:

    def test_title_is_gst_and_taxation(self):
        assert K.ALL_COURSES[CODE][0] == NEW_TITLE

    def test_card_is_still_the_gst_card(self):
        assert K.ALL_COURSES[CODE][1] is K._GST

    def test_card_body_is_byte_identical(self):
        assert K._GST == GST_CARD

    def test_old_title_is_gone_from_all_courses_and_course_fees(self):
        titles = [title for title, _card in K.ALL_COURSES.values()]
        assert OLD_TITLE not in titles
        assert all(OLD_TITLE.lower() not in k.lower() for k in K.COURSE_FEES)

    def test_price_and_duration_are_byte_identical(self):
        assert K.COURSE_FEES[NEW_TITLE] == (FEE, DURATION)

    def test_no_payroll_in_any_default_title_or_keyword(self):
        for idx, (title, _card) in K.ALL_COURSES.items():
            assert "payroll" not in title.lower(), f"course {idx}: {title!r}"
        for kw in K.KEYWORD_TO_COURSE:
            assert "payroll" not in kw.lower(), kw

    def test_the_other_nine_titles_are_unchanged(self):
        assert {i: t for i, (t, _c) in K.ALL_COURSES.items()} == EXPECTED_TITLES

    def test_numbering_and_position_are_unchanged(self):
        expected = [str(i) for i in range(1, 11)]
        assert sorted(K.ALL_COURSES, key=int) == expected
        recs = cat._default_catalogue()
        assert [r.code for r in recs] == expected
        assert _default_record().sort_order == 5

    def test_default_record(self):
        r = _default_record()
        assert (r.code, r.title, r.normal_total_fee, r.duration) == (
            CODE, NEW_TITLE, FEE, DURATION)
        assert r.body == GST_CARD
        assert r.categories == ("accounting",)
        assert set(r.keywords) == {"gst", "tally", "taxation"}
        assert r.emi_available is False and r.is_default is True


# ═══ 3 — keywords ══════════════════════════════════════════════════════════

class TestKeywords:

    def test_keyword_table_is_the_old_table_minus_payroll_only(self):
        assert K.KEYWORD_TO_COURSE == EXPECTED_KEYWORDS

    def test_payroll_maps_to_no_course(self):
        assert "payroll" not in K.KEYWORD_TO_COURSE

    @pytest.mark.parametrize("kw", ["gst", "tally", "taxation"])
    def test_gst_terms_still_reach_course_five(self, seeded, kw):
        with _APP.app_context():
            m = cat.match_keyword(BARE, kw)
        assert m.kind == "exact" and m.course.code == CODE, (kw, m)

    def test_payroll_no_longer_opens_course_five(self, seeded):
        with _APP.app_context():
            m = cat.match_keyword(BARE, "payroll")
        assert m.kind == "none", m


# ═══ 4 — companions deliberately left alone ════════════════════════════════

class TestCompanionsUntouched:

    def test_crm_alias_keeps_its_historical_target(self):
        assert K.COURSE_NAME_ALIASES["gst & payroll diploma"] == OLD_TITLE

    def test_oxford_legacy_mapping_is_unchanged(self):
        assert cat.LEGACY_NAME_TO_CODE["gst & payroll"] == "DGSTP"

    def test_goal_courses_accounting_row_is_unchanged(self):
        assert K.GOAL_COURSES["accounting"] == EXPECTED_ACCOUNTING_GOAL

    def test_dormant_fee_table_is_unchanged(self):
        assert "5️⃣  GST & Payroll          ₹18,999  (6M)" in K.FULL_FEE_TABLE

    def test_no_payment_constant_names_the_gst_course(self):
        for table in (K.OFFER_MENU, K.COURSE_PAYMENT_LINKS):
            assert "GST" not in str(table)


# ═══ 5 — the default course is NOT Oxford's DGSTP ══════════════════════════

class TestDefaultIsNotDGSTP:

    def test_default_catalogue_contains_no_dgstp(self):
        for r in cat._default_catalogue():
            blob = " ".join([r.code, r.title, r.body or ""] + list(r.keywords))
            assert "DGSTP" not in blob.upper(), f"default course {r.code} carries DGSTP"

    def test_course_five_carries_none_of_dgstps_commercials(self):
        r = _default_record()
        assert r.normal_total_fee == FEE and "24,090" not in r.normal_total_fee
        assert r.emi_available is False
        assert r.registration_fee is None and r.net_tuition_fee is None
        assert "Rutronix" not in (r.body or "")

    def test_default_index_contains_no_dgstp(self, seeded):
        with _APP.app_context():
            idx = cat.catalogue_index(BARE)
        assert idx and not any("DGSTP" in line for line in idx)


# ═══ 6 — customer-facing surfaces, default tenant ══════════════════════════

class TestDefaultTenantSurfaces:

    def test_course_list_keeps_code_position_price_and_duration(self, seeded):
        with _APP.app_context():
            screen = screens.course_list("accounting", tenant_id=BARE)
        rows = [r for r in _rows(screen) if r.id.startswith("CRS:")]
        assert [r.id for r in rows] == ["CRS:3", "CRS:5", "CRS:8"]
        row = rows[1]
        assert row.title == NEW_TITLE
        assert FEE in row.description and DURATION in row.description
        assert "Payroll" not in " ".join(r.title + r.description for r in rows)

    def test_course_button_is_still_code_five(self):
        assert nav.course_id(CODE) == "CRS:5"
        action = nav.parse_action("CRS:5")
        assert action is not None and action.value == CODE

    def test_course_details(self, seeded):
        with _APP.app_context():
            screen = screens.course_details(CODE, tenant_id=BARE)
        assert screen is not None
        body = screen.body
        assert f"\U0001f4da *{NEW_TITLE}*" in body
        assert f"⏱ Duration: {DURATION}" in body
        assert f"\U0001f4b0 Course Fee: *{FEE}*" in body
        assert "Course Fee: **" not in body
        assert "Payroll" not in body

    def test_msg_course_detail(self, seeded):
        with _APP.app_context():
            body = _text(router.msg_course_detail(CODE, tenant_id=BARE))
        assert f"✅ *{NEW_TITLE}*" in body
        assert f"Course Fee: *{FEE}*" in body and f"Duration: {DURATION}" in body
        assert "Course Fee: **" not in body and "Payroll" not in body

    def test_fee_reply(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply(NEW_TITLE, BARE)
        assert f"\U0001f4b0 *{NEW_TITLE} — Fee Details*" in text
        assert f"Total Fee: *{FEE}*" in text
        assert f"Duration: {DURATION}" in text
        assert "EMI not available for this course" in text
        assert "Total Fee: **" not in text and "Payroll" not in text

    def test_whole_catalogue_fee_listing(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("", BARE)
        assert f"• *{NEW_TITLE}*  {FEE}  ({DURATION})" in text
        assert "Payroll" not in text

    def test_ai_catalogue_index_and_prompt(self, seeded):
        with _APP.app_context():
            idx = cat.catalogue_index(BARE)
            prompt = prompt_composer.compose_system_prompt(BARE)
        assert INDEX_LINE in idx
        assert INDEX_LINE in prompt
        assert OLD_TITLE not in prompt

    def test_ai_course_context(self, seeded):
        """router.py hands `Course details:\\n<body or title>` to the AI on a
        keyword question. Constructed here, never sent."""
        with _APP.app_context():
            rec = cat.get_course(BARE, CODE)
        context = "Course details:\n" + (rec.body or rec.title)
        assert "Syllabus:" in context and "Payroll" not in context

    def test_old_title_in_saved_state_never_substitutes_a_course(self, seeded):
        """A default-tenant conversation saved with the OLD title (0 such rows
        in production at x-6d4) resolves to nothing rather than guessing, and
        the fee reply falls back to the listing -- which still prices course 5."""
        with _APP.app_context():
            assert cat.resolve_legacy_name(BARE, OLD_TITLE) is None
            text, _ = cta.fees_reply(OLD_TITLE, BARE)
        assert f"• *{NEW_TITLE}*  {FEE}  ({DURATION})" in text


# ═══ 7 — Oxford's authored DGSTP is unaffected ═════════════════════════════

class TestOxfordDGSTPUnaffected:

    def test_oxford_catalogue_is_authored_not_default(self, seeded):
        with _APP.app_context():
            recs = cat.list_courses(OX)
        assert recs and not any(r.is_default for r in recs)
        assert all(r.title != NEW_TITLE for r in recs)

    def test_legacy_payroll_name_still_resolves_to_dgstp_for_oxford(self, seeded):
        with _APP.app_context():
            r = cat.resolve_legacy_name(OX, OLD_TITLE)
        assert r is not None and r.code == "DGSTP"

    def test_new_default_title_never_resolves_to_dgstp(self, seeded):
        with _APP.app_context():
            assert cat.resolve_legacy_name(OX, NEW_TITLE) is None
            assert cat.get_course(OX, CODE) is None

    def test_dgstp_record_keeps_its_own_commercials(self, seeded):
        with _APP.app_context():
            r = cat.get_course(OX, "DGSTP")
        assert r.title == "DGSTP – Goods & Services Tax Practice"
        assert r.normal_total_fee == 24090 and r.emi_available is True

    def test_oxford_keywords_are_its_own(self, seeded):
        with _APP.app_context():
            assert cat.match_keyword(OX, "gst").course.code == "DGSTP"
            assert cat.match_keyword(OX, "payroll").kind == "none"
