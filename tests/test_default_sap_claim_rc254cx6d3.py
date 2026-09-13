"""RC2.5.4c-x-6d3 — the default `_SAP` card must not claim SAP certification.

WHY
---
The card carried `🌟 SAP-Certified Skills — high demand in corporate sector`.
The x-6d2 audit found:

  * no provenance for it anywhere in the repository -- "SAP Alliance",
    "SAP-Certified" and "SAP partner" appear only in constants.py, a backup
    file, and the tests that pin their absence;
  * it entered in 18aed06 "Phase 7.0: Oxford Nova UX Polish", a UX commit;
  * no tenant on the platform offers a SAP course -- Oxford's nearest is
    PDCFA (Computerised Financial Accounting), which is not SAP;
  * it reached three customer-facing surfaces: screens.course_details,
    router.msg_course_detail, and the per-turn course_context handed to the
    AI, because all three render the default card body verbatim.

x-6d1 removed the other unsupported claims from this card ("SAP Alliance +
Rutronix" from the duration line and the "Dual Certification" line). This
phase removes the last one.

SCOPE -- deliberately one line
------------------------------
The COURSE itself is untouched: title, syllabus, duration, price, catalogue
membership, emi_available and is_default are all pinned below. Whether the
platform default catalogue should advertise a SAP course at all is a
different question (course existence, not a claim) and was deferred by the
x-6d2 audit.

NOT PINNED ON PURPOSE: the rest of the card byte-for-byte. Over-pinning would
make every later edit fail for the wrong reason. The invariant is "the
certification claim is gone while the course identity survives".
"""
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6d3_sap.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6d3-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6d3-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6d3-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "")

for _m in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.bot import constants as K                                      # noqa: E402
from app.bot import router, screens                                     # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402

OX = "t-ox"
BARE = "t-bare"          # authored nothing -> receives the default catalogue

_APP = create_app()
_APP.config["TESTING"] = True

SAP_CODE = "3"
REMOVED_CLAIM = "SAP-Certified Skills — high demand in corporate sector"

# Invariants the course keeps. These are the identity of the course, as
# opposed to the claim that was removed.
SAP_TITLE = "SAP Financial Accounting"
SAP_CARD_HEADING = "\U0001f4da *SAP Financial Accounting & Controlling*"
SAP_SYLLABUS = ("\U0001f4bb Syllabus: GL Accounting, AP/AR, Asset Accounting, "
                "SAP CO, Real-Time Project")
SAP_DURATION = "6 Months"
SAP_FEE = "₹15,000"


def _sap_card():
    """The card as the runtime reaches it -- through ALL_COURSES."""
    return K.ALL_COURSES[SAP_CODE][1]


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
        # An authored row that deliberately DOES claim SAP certification.
        # x-6d3 touches the default catalogue only; tenant-authored content
        # must be left exactly as its tenant wrote it.
        db.session.add(TenantKnowledge(
            tenant_id=OX, kind="course", title="Authored SAP Course",
            body="Authored body. \U0001f31f SAP-Certified Skills included.",
            attributes=json.dumps({
                "duration": "6 Months", "categories": ["accounting"],
                "keywords": ["authoredsap"],
                "commercial": {"code": "AUTHSAP", "currency": "INR",
                               "normal_total_fee": 11400, "base_price": 11400,
                               "emi_available": True, "offers": []}}),
            is_active=True, sort_order=1))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ A / B — the claim is gone ═════════════════════════════════════════════

class TestClaimRemoved:

    def test_card_does_not_contain_the_full_claim(self):
        assert REMOVED_CLAIM not in _sap_card()

    def test_card_does_not_contain_sap_certified(self):
        assert "SAP-Certified" not in _sap_card()

    def test_card_does_not_contain_sap_certified_case_insensitively(self):
        """A re-introduction must not be able to hide behind capitalisation
        or a different dash."""
        low = _sap_card().lower()
        for variant in ("sap-certified", "sap certified", "sapcertified"):
            assert variant not in low, f"card still claims {variant!r}"

    def test_constant_addressed_directly_is_also_clean(self):
        assert "SAP-Certified" not in K._SAP


# ═══ C / D / E / F / G / H / I — the course survives intact ════════════════

class TestCourseIdentityRetained:

    def test_the_sap_card_still_exists(self):
        assert K._SAP and K._SAP.strip()

    def test_card_is_still_present_in_all_courses(self):
        assert SAP_CODE in K.ALL_COURSES
        assert K.ALL_COURSES[SAP_CODE][1] is K._SAP
        assert len(K.ALL_COURSES) == 10

    def test_catalogue_title_is_unchanged(self):
        assert K.ALL_COURSES[SAP_CODE][0] == SAP_TITLE

    def test_card_heading_is_unchanged(self):
        assert SAP_CARD_HEADING in _sap_card()

    def test_syllabus_is_unchanged(self):
        assert SAP_SYLLABUS in _sap_card()

    def test_duration_is_unchanged(self):
        assert f"Duration: {SAP_DURATION}" in _sap_card()
        assert K.COURSE_FEES[SAP_TITLE][1] == SAP_DURATION

    def test_price_is_unchanged(self):
        assert SAP_FEE in _sap_card()
        assert K.COURSE_FEES[SAP_TITLE][0] == SAP_FEE

    def test_card_remains_descriptive_and_non_empty(self):
        """Anti-vacuity: "claim absent" must not be satisfiable by an empty
        or gutted card."""
        card = _sap_card()
        assert len(card) > 120, f"card suspiciously short: {len(card)}"
        for marker in ("\U0001f4da", "\U0001f4bc Best for:", "\U0001f4bb Syllabus:",
                       "⏱ Duration:", "\U0001f4b0 Course Fee"):
            assert marker in card, f"card lost descriptive marker {marker!r}"
        audience = next(l for l in card.splitlines() if "Best for:" in l)
        assert len(audience.split("Best for:", 1)[1].strip()) > 10


# ═══ J / K — structured default-record state unchanged ═════════════════════

class TestDefaultRecordUnchanged:

    def test_emi_available_is_still_false(self):
        rec = next(r for r in cat._default_catalogue() if r.code == SAP_CODE)
        assert rec.emi_available is False

    def test_is_default_is_still_true(self):
        rec = next(r for r in cat._default_catalogue() if r.code == SAP_CODE)
        assert rec.is_default is True

    def test_record_title_fee_and_duration_unchanged(self):
        rec = next(r for r in cat._default_catalogue() if r.code == SAP_CODE)
        assert rec.title == SAP_TITLE
        assert rec.normal_total_fee == SAP_FEE
        assert rec.duration == SAP_DURATION

    def test_record_body_carries_no_sap_certification_claim(self):
        rec = next(r for r in cat._default_catalogue() if r.code == SAP_CODE)
        assert "SAP-Certified" not in (rec.body or "")

    def test_the_default_catalogue_still_has_ten_courses(self):
        assert len(cat._default_catalogue()) == 10


# ═══ L — the three card-body customer surfaces ═════════════════════════════

class TestCustomerFacingSurfaces:

    def test_course_details_carries_no_sap_certification_claim(self, seeded):
        with _APP.app_context():
            screen = screens.course_details(SAP_CODE, tenant_id=BARE)
        assert screen is not None
        assert "SAP-Certified" not in screen.body

    def test_course_details_still_describes_the_course(self, seeded):
        """Anti-vacuity for the surface: absence must not be because the
        screen stopped rendering the card."""
        with _APP.app_context():
            screen = screens.course_details(SAP_CODE, tenant_id=BARE)
        assert "Syllabus:" in screen.body
        assert SAP_FEE in screen.body

    def test_msg_course_detail_carries_no_sap_certification_claim(self, seeded):
        with _APP.app_context():
            out = router.msg_course_detail(SAP_CODE, tenant_id=BARE)
        body = out[0] if isinstance(out, tuple) else getattr(out, "body", str(out))
        assert "SAP-Certified" not in body
        assert "Syllabus:" in body          # still rendering the card

    def test_ai_course_context_carries_no_sap_certification_claim(self, seeded):
        """router.py builds `course_context` from the record BODY and hands it
        to gemini_reply. Constructed here, never sent."""
        with _APP.app_context():
            rec = cat.get_course(BARE, SAP_CODE)
        context = "Course details:\n" + (rec.body or rec.title)
        assert "SAP-Certified" not in context
        assert "Syllabus:" in context


# ═══ M — authored tenant content untouched ═════════════════════════════════

class TestAuthoredContentUnaffected:

    def test_authored_body_keeps_its_own_sap_claim(self, seeded):
        """x-6d3 edits a platform-default constant. A tenant that authored a
        SAP claim in its OWN catalogue must still have it rendered -- proving
        the change did not reach tenant-authored content."""
        with _APP.app_context():
            recs = cat.list_courses(OX)
            assert recs and not any(r.is_default for r in recs)
            body = next(r.body for r in recs if r.code == "AUTHSAP")
        assert "SAP-Certified Skills included." in body

    def test_authored_record_flags_are_its_own(self, seeded):
        with _APP.app_context():
            rec = cat.get_course(OX, "AUTHSAP")
        assert rec.emi_available is True and rec.is_default is False
