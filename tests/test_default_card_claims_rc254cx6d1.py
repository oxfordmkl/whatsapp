"""RC2.5.4c-x-6d1 — the ten DEFAULT course cards must carry no unsupported
or contradictory customer-facing claim.

WHY THIS SUITE EXISTS
---------------------
`catalogue_service._default_catalogue()` builds one CourseRecord per card in
`constants.ALL_COURSES` for every tenant that has not authored a catalogue --
11 of 12 tenants at the time of writing. The cards predate multi-tenancy, so
each claim in them was Oxford's, asserted unconditionally to whoever received
the card:

  * "EMI Available" contradicted the record's OWN emi_available=False.
    course_details rendered the card body and said EMI was available;
    fees_reply read the flag and said it was not. Opposite answers, same
    course, different surface -- harder to notice than a single-message
    contradiction.
  * Rutronix / "Government Approved" / "Industry-Recognised Certificate" /
    "SAP Alliance" asserted accreditations that no default record has any
    provenance for: 0/10 default records carry regulatory data, while 13/17
    of Oxford's AUTHORED rows carry regulatory.source.
  * "Payroll" is documented in catalogue_service itself as NOT source-backed.

SCOPE OF x-6d1 -- deliberately narrow
-------------------------------------
Claim text only. Prices, durations, titles, syllabi and audience lines are
untouched, emi_available stays False, and catalogue_service is not modified.
The per-course EMI line in cta_handlers/screens remains the ONLY place EMI is
stated. Whether EMI is offered at all is still an open business question.

NOT IN SCOPE (asserted elsewhere or deliberately deferred):
  * default PRICES -- stale versus Oxford's authored figures, but there is no
    evidence Oxford's current prices are correct for any other tenant. A
    price is pinned below precisely so this phase cannot drift one.
  * `ALL_COURSES` course NAMES -- "GST & Payroll" still carries Payroll as a
    *name*, rendered as the screen title. Out of this phase's card scope.
  * main_menu's "Full fee list + EMI options" row, TRUST_LINES, prompts.py,
    identity fallback -- separate phases.
"""
import ast
import io
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6d1_cards.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6d1-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6d1-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6d1-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "")

for _m in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.bot import constants as K                                      # noqa: E402
from app.bot import cta_handlers as cta, screens                        # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402

OX = "t-ox"
BARE = "t-bare"          # authored nothing -> receives the default catalogue

_APP = create_app()
_APP.config["TESTING"] = True

# The ten authorised card constants, in ALL_COURSES order.
CARD_NAMES = ["_PGDCA", "_AIDM", "_SAP", "_PYTHON", "_GST",
              "_DCA", "_TEACHER", "_ACCOUNTING", "_WORD", "_WEB"]

# Claim text this phase removed. Matched case-insensitively so a
# re-introduction cannot hide behind capitalisation.
BANNED_CLAIMS = [
    "EMI Available",
    "EMI / installment",
    "Rutronix",
    "Government Approved",
    "Government Recognised",
    "recognised for Govt",
    "Industry-Recognised Certificate",
    "SAP Alliance",
    "Dual Certification",
    "Payroll",
]

# Descriptive markers that MUST survive -- these are the anti-vacuity anchors.
# Without them "no banned claim present" would pass on an empty string.
REQUIRED_MARKERS = ["\U0001f4da", "\U0001f4bc Best for:", "\U0001f4bb Syllabus:",
                    "⏱ Duration:", "\U0001f4b0 Course Fee"]

# Prices and durations pinned EXACTLY: x-6d1 must not drift a commercial value.
EXPECTED_FEES = {
    "1": "₹15,999", "2": "₹19,999", "3": "₹15,000",
    "4": "₹4,499", "5": "₹18,999", "6": "₹6,400",
    "7": "₹11,999", "8": "₹40,000", "9": "₹4,800",
    "10": "₹8,800",
}
# The STRUCTURED duration, from COURSE_FEES -- what CourseRecord.duration
# carries and what the renderer prints as its own "Duration:" line.
EXPECTED_DURATIONS = {
    "1": "12 Months", "2": "6 Months", "3": "6 Months", "4": "3 Months",
    "5": "6 Months", "6": "6 Months", "7": "1 Year", "8": "1 Year",
    "9": "6 Months", "10": "6 Months",
}
# The duration written in the CARD TEXT. Cards 7 and 8 say "12 Months" where
# COURSE_FEES says "1 Year" -- a PRE-EXISTING discrepancy documented by the
# x-6d audit, NOT introduced here. Both are pinned separately so x-6d1 cannot
# drift either, and so the discrepancy stays visible instead of being
# smoothed over by a single loose assertion.
EXPECTED_CARD_DURATIONS = dict(EXPECTED_DURATIONS, **{"7": "12 Months",
                                                      "8": "12 Months"})
EXPECTED_TITLES = {
    "1": "PGDCA", "2": "AIDM Digital Marketing", "3": "SAP Financial Accounting",
    "4": "Python Programming", "5": "GST & Payroll", "6": "DCA Fast Track",
    "7": "Computer Teacher Training", "8": "Corporate Business Accounting",
    "9": "Word Processing & Data Entry", "10": "Professional Web Designing",
}


def _cards():
    """The ten card bodies, addressed through ALL_COURSES (the real path)."""
    return {idx: K.ALL_COURSES[idx][1] for idx in EXPECTED_TITLES}


def _row(tenant, code, title, *, emi, months, fee, order=1):
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title,
        body=f"Authored body for {title}. ✅ EMI Available",
        attributes=json.dumps({
            "duration": f"{months} Months", "categories": ["job"],
            "keywords": [code.lower()],
            "commercial": {"code": code, "currency": "INR",
                           "normal_total_fee": fee, "base_price": fee,
                           "emi_available": emi, "offers": []}}),
        is_active=True, sort_order=order)


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
        db.session.add(_row(OX, "HASEMI", "PGDCA Computer Applications",
                            emi=True, months=12, fee=19540, order=1))
        db.session.add(_row(OX, "NOEMI", "Python Programming",
                            emi=False, months=3, fee=5180, order=2))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ A — no banned claim survives in any card ═══════════════════════════════

class TestClaimsRemoved:

    @pytest.mark.parametrize("idx", sorted(EXPECTED_TITLES, key=int))
    @pytest.mark.parametrize("claim", BANNED_CLAIMS)
    def test_card_carries_no_banned_claim(self, idx, claim):
        body = _cards()[idx]
        assert claim.lower() not in body.lower(), (
            f"card {idx} ({EXPECTED_TITLES[idx]}) still claims {claim!r}")

    @pytest.mark.parametrize("name", CARD_NAMES)
    def test_constant_itself_carries_no_banned_claim(self, name):
        """Addressed by constant name as well as through ALL_COURSES, so a
        card detached from the menu cannot keep a claim unnoticed."""
        body = getattr(K, name)
        for claim in BANNED_CLAIMS:
            assert claim.lower() not in body.lower(), \
                f"{name} still claims {claim!r}"

    def test_rutronix_label_is_not_interpolated_into_any_card(self):
        """The claim was an f-string interpolation, not a literal, so assert
        against the RESOLVED text -- a re-added f-string would be caught."""
        for idx, body in _cards().items():
            assert K.RUTRONIX_LABEL not in body, f"card {idx} interpolates RUTRONIX_LABEL"
            assert K.RUTRONIX_FULL not in body, f"card {idx} interpolates RUTRONIX_FULL"


# ═══ B / K — descriptive content survives (anti-vacuity) ════════════════════

class TestDescriptiveContentRetained:

    @pytest.mark.parametrize("idx", sorted(EXPECTED_TITLES, key=int))
    def test_card_is_not_empty(self, idx):
        body = _cards()[idx]
        assert body and body.strip(), f"card {idx} is empty"
        assert len(body) > 120, f"card {idx} is suspiciously short: {len(body)}"

    @pytest.mark.parametrize("idx", sorted(EXPECTED_TITLES, key=int))
    @pytest.mark.parametrize("marker", REQUIRED_MARKERS)
    def test_card_retains_descriptive_marker(self, idx, marker):
        assert marker in _cards()[idx], (
            f"card {idx} lost descriptive marker {marker!r} -- x-6d1 removes "
            f"claims, not description")

    def test_every_card_still_has_a_syllabus_with_content(self):
        for idx, body in _cards().items():
            line = next(l for l in body.splitlines() if "Syllabus:" in l)
            payload = line.split("Syllabus:", 1)[1].strip()
            assert len(payload) > 20, f"card {idx} syllabus emptied: {payload!r}"

    def test_every_card_still_has_an_audience_line_with_content(self):
        for idx, body in _cards().items():
            line = next(l for l in body.splitlines() if "Best for:" in l)
            payload = line.split("Best for:", 1)[1].strip()
            assert len(payload) > 10, f"card {idx} audience emptied: {payload!r}"


# ═══ C / D — commercial values pinned ═══════════════════════════════════════

class TestCommercialValuesUnchanged:

    @pytest.mark.parametrize("idx", sorted(EXPECTED_FEES, key=int))
    def test_card_price_is_unchanged(self, idx):
        assert EXPECTED_FEES[idx] in _cards()[idx], (
            f"card {idx} price changed -- x-6d1 must not touch prices")

    @pytest.mark.parametrize("idx", sorted(EXPECTED_CARD_DURATIONS, key=int))
    def test_card_duration_text_is_unchanged(self, idx):
        assert f"Duration: {EXPECTED_CARD_DURATIONS[idx]}" in _cards()[idx], (
            f"card {idx} duration text changed")

    def test_cards_7_and_8_still_disagree_with_course_fees(self):
        """Pins the PRE-EXISTING duration discrepancy so it cannot be
        silently 'tidied' under cover of a claim-cleanup phase, and so it
        stays visible for the phase that is actually authorised to fix it."""
        for idx in ("7", "8"):
            assert "Duration: 12 Months" in _cards()[idx]
            assert EXPECTED_DURATIONS[idx] == "1 Year"
            assert K.COURSE_FEES[EXPECTED_TITLES[idx]][1] == "1 Year"

    def test_course_fees_table_is_unchanged(self):
        """COURSE_FEES is the structured source for normal_total_fee. x-6d1
        has no authorisation to touch it."""
        for idx, title in EXPECTED_TITLES.items():
            fee, dur = K.COURSE_FEES[title]
            assert fee == EXPECTED_FEES[idx], f"COURSE_FEES[{title}] fee changed"
            assert dur == EXPECTED_DURATIONS[idx], f"COURSE_FEES[{title}] duration changed"


# ═══ E / F — the default catalogue itself is unchanged ══════════════════════

class TestDefaultCatalogueUnchanged:

    def test_exactly_ten_courses_with_the_same_identity(self):
        recs = cat._default_catalogue()
        assert len(recs) == 10
        assert {r.code for r in recs} == set(EXPECTED_TITLES)
        for r in recs:
            assert r.title == EXPECTED_TITLES[r.code]

    def test_emi_available_is_still_false_on_every_default_record(self):
        recs = cat._default_catalogue()
        assert recs and all(r.emi_available is False for r in recs)

    def test_is_default_is_still_true_on_every_default_record(self):
        assert all(r.is_default is True for r in cat._default_catalogue())

    def test_structured_fee_and_duration_survive(self):
        for r in cat._default_catalogue():
            assert r.normal_total_fee == EXPECTED_FEES[r.code]
            assert r.duration == EXPECTED_DURATIONS[r.code]

    def test_record_bodies_are_the_cleaned_cards(self):
        for r in cat._default_catalogue():
            for claim in BANNED_CLAIMS:
                assert claim.lower() not in (r.body or "").lower(), (
                    f"default record {r.code} body still claims {claim!r}")


# ═══ I — no tenant-specific data introduced ════════════════════════════════

class TestCardsCarryNoTenantIdentity:

    @pytest.mark.parametrize("idx", sorted(EXPECTED_TITLES, key=int))
    def test_card_names_no_institution_and_no_contact(self, idx):
        body = _cards()[idx]
        for leak in ("Oxford", "theoxfordedu", "9447329972", "Malayinkeezhu",
                     "Thiruvananthapuram", "rzp.io", "http://", "https://"):
            assert leak.lower() not in body.lower(), (
                f"card {idx} carries tenant-specific/contact data {leak!r}")


# ═══ G / H / J — runtime behaviour ═════════════════════════════════════════

class TestRuntimeSurfaces:

    def test_default_tenant_course_detail_has_no_banned_claim(self, seeded):
        with _APP.app_context():
            screen = screens.course_details("1", tenant_id=BARE)
        assert screen is not None
        for claim in BANNED_CLAIMS:
            assert claim.lower() not in screen.body.lower(), (
                f"default course_details still claims {claim!r}")

    def test_default_tenant_course_detail_still_shows_description(self, seeded):
        """J — the card body still REACHES the customer-facing surface."""
        with _APP.app_context():
            screen = screens.course_details("1", tenant_id=BARE)
        assert "Syllabus:" in screen.body
        assert "Best for:" in screen.body
        assert "₹15,999" in screen.body

    def test_default_tenant_fees_reply_still_denies_emi(self, seeded):
        """F — the conditional negative rendering is untouched, and it is now
        the ONLY EMI statement a default tenant can receive."""
        with _APP.app_context():
            text, _ = cta.fees_reply("PGDCA", tenant_id=BARE)
        assert "EMI not available for this course" in text

    def test_default_tenant_sees_no_contradictory_emi_pair(self, seeded):
        """The x-6d defect: course_details affirmed EMI while fees_reply
        denied it. Neither surface may now affirm it."""
        with _APP.app_context():
            detail = screens.course_details("1", tenant_id=BARE).body
            fees, _ = cta.fees_reply("PGDCA", tenant_id=BARE)
        for text in (detail, fees):
            for line in text.splitlines():
                if "emi" in line.lower() and "not available" not in line.lower():
                    pytest.fail(f"EMI affirmed on a default surface: {line!r}")

    def test_authored_tenant_body_is_untouched_by_this_phase(self, seeded):
        """G/H — authored rows come from TenantKnowledge, never from these
        constants. The seeded authored body deliberately contains
        "EMI Available"; it must still be rendered, proving x-6d1 changed the
        DEFAULT path only and did not reach tenant-authored content."""
        with _APP.app_context():
            recs = cat.list_courses(OX)
            assert recs and not any(r.is_default for r in recs)
            body = next(r.body for r in recs if r.code == "HASEMI")
        assert "Authored body for" in body
        assert "✅ EMI Available" in body, (
            "x-6d1 must not modify tenant-authored bodies")

    def test_authored_tenant_emi_flag_still_drives_its_own_rendering(self, seeded):
        with _APP.app_context():
            assert cat.get_course(OX, "HASEMI").emi_available is True
            assert cat.get_course(OX, "NOEMI").emi_available is False


# ═══ scope contract ═══════════════════════════════════════════════════════

class TestScopeContract:

    def test_only_the_ten_cards_changed_in_constants(self):
        """Every other top-level construct in constants.py is byte-identical
        to HEAD. Catches a price table, payment link or TRUST_LINES edit
        smuggled in alongside the authorised card cleanup."""
        import subprocess
        rel = "app/bot/constants.py"
        # NOT text=True: on Windows that decodes git's stdout with the locale
        # codepage and mangles Malayalam and emoji, firing on an artifact.
        head = subprocess.run(["git", "show", "HEAD:" + rel],
                              cwd=_ROOT, capture_output=True)
        assert head.returncode == 0, "cannot read HEAD:" + rel
        old_src = "\n".join(head.stdout.decode("utf-8").splitlines())
        with io.open(os.path.join(_ROOT, "app", "bot", "constants.py"),
                     encoding="utf-8") as fh:
            new_src = "\n".join(fh.read().splitlines())

        def named(src):
            out = {}
            for node in ast.parse(src).body:
                if (isinstance(node, ast.Assign)
                        and isinstance(node.targets[0], ast.Name)):
                    out[node.targets[0].id] = ast.get_source_segment(src, node)
                elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    out[node.name] = ast.get_source_segment(src, node)
            return out

        old, new = named(old_src), named(new_src)
        assert set(old) == set(new), (
            "top-level constructs added/removed in constants.py: "
            + str(set(old) ^ set(new)))
        for name, seg in old.items():
            if name in CARD_NAMES:
                continue                      # the authorised ten
            assert new[name] == seg, (
                f"constants.py::{name} changed, but x-6d1 authorises only "
                f"{CARD_NAMES}")

    def test_catalogue_service_still_sets_emi_available_false(self):
        with io.open(os.path.join(_ROOT, "app", "services",
                                  "catalogue_service.py"), encoding="utf-8") as fh:
            src = fh.read()
        assert "emi_available=False" in src, (
            "the default catalogue must still report emi_available=False")

    def test_payment_constants_untouched(self):
        """x-6d1 is claim cleanup, not a payment change."""
        assert "rzp.io" in str(K.OFFER_MENU)
        assert len(K.OFFER_MENU) == 4
        assert len(K.ALL_COURSES) == 10
        assert len(K.COURSE_FEES) == 10
