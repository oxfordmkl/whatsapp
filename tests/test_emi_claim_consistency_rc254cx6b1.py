"""Phase RC2.5.4c-x-6b1: no unconditional EMI claim reaches a customer.

THE DEFECT
----------
EMI is a PER-COURSE fact -- commercial.emi_available -- and RC2.5.5c-3 made
the deterministic flow say so. screens.py records the rule in its own comment:
"EMI is a per-course fact now, not a blanket claim."

Three claim sites were never part of that change and kept asserting EMI
unconditionally:

    constants.TRUST_LINES       "EMI / installment option available aanu."
    constants.FEES_VALUE_LINES  "EMI option und, so full amount tension venda"
    ai_service.smart_fallback   "EMI / installment option um und!"

TRUST_LINES and FEES_VALUE_LINES are drawn with pick() -- a UNIFORM RANDOM
choice -- and appended AFTER the per-course line. So cta_handlers.fees_reply()
could emit, in ONE message, for a 3-month course:

    EMI not available for this course
    ...
    EMI / installment option available aanu.

roughly a quarter of the time, on all four 3-month certificates. Reproduced
against production data in the RC2.5.4c-x-6b audit. smart_fallback fires on
fee questions specifically, where a wrong EMI claim matters most.

THE FIX IS A REMOVAL, NOT A REWRITE
------------------------------------
None of the three sites has a course in scope, so none can make the claim
conditional. The per-course lines in cta_handlers, router, screens and the
catalogue index are the only places EMI is stated, and they are UNTOUCHED --
this suite asserts that too.

EXPLICITLY NOT DECIDED HERE: whether EMI is actually offered. That is the open
business question from the audit (section I) and belongs to B2/B3. This phase
only stops the system contradicting itself.

The randomness is handled by exhausting the pools rather than sampling: every
line is asserted directly, so the test cannot pass by luck.
"""
import ast
import json
import os
import random
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6b1_emi.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6b1-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6b1-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6b1-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.bot import constants as K                                      # noqa: E402
from app.bot import cta_handlers as cta, objections, router, screens    # noqa: E402
from app.services import ai_service                                     # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402

OX = "t-ox"
B = "t-b"

_APP = create_app()
_APP.config["TESTING"] = True

# Any phrasing that ASSERTS EMI exists. Deliberately broader than the exact
# removed strings: a future edit that reintroduces the claim in different
# words must also fail. "EMI not available" and "No EMI" are negations and are
# excluded by the checker below.
_AFFIRMING = ("emi / installment", "emi / instalment", "emi option",
              "emi available", "emi und", "installment option",
              "instalment option")


# A line qualifies for inspection if it mentions EMI *or* instalments at all.
# Requiring the literal "emi" was too narrow: "Installment option available
# aanu." is the same unconditional financial claim without the acronym, and a
# mutation proved it slipped through.
_TRIGGERS = ("emi", "installment", "instalment")

# Phrases that mean the claim is being DENIED, not made.
_NEGATIONS = ("not available", "no emi", "illa", "avail alla")


def _affirms_emi(text):
    """Return every line of `text` that AFFIRMS an EMI/instalment facility.

    A line denying it is not an affirmation, so the two legitimate negative
    renderings ("EMI not available for this course", "No EMI") are recognised
    and skipped rather than flagged.
    """
    low = (text or "").lower()
    out = []
    for line in low.splitlines():
        if not any(t in line for t in _TRIGGERS):
            continue
        if any(n in line for n in _NEGATIONS):
            continue                      # the legitimate negative claim
        if any(p in line for p in _AFFIRMING):
            out.append(line.strip())
    return out


def _row(tenant, code, title, *, emi, months, fee, order=1):
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title,
        body=f"About {title}.",
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
        for tid in (OX, B):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        # NOEMI mirrors the four 3-month certificates: emi_available False.
        db.session.add(_row(OX, "NOEMI", "Python Programming",
                            emi=False, months=3, fee=5180, order=1))
        db.session.add(_row(OX, "HASEMI", "PGDCA Computer Applications",
                            emi=True, months=12, fee=19540, order=2))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ 1-3 — the three claim sites carry no unconditional EMI ═══════════════

class TestClaimSitesAreClean:

    def test_trust_lines_has_no_unconditional_emi_claim(self):
        for line in K.TRUST_LINES:
            assert not _affirms_emi(line), f"TRUST_LINES still affirms EMI: {line!r}"

    def test_fees_value_lines_has_no_unconditional_emi_claim(self):
        for line in K.FEES_VALUE_LINES:
            assert not _affirms_emi(line), \
                f"FEES_VALUE_LINES still affirms EMI: {line!r}"

    def test_both_pools_are_still_non_empty_and_useful(self):
        """A removal must not empty a pool -- pick() on [] would raise."""
        assert len(K.TRUST_LINES) >= 3
        assert len(K.FEES_VALUE_LINES) >= 3
        assert all(isinstance(x, str) and x.strip() for x in K.TRUST_LINES)
        assert all(isinstance(x, str) and x.strip() for x in K.FEES_VALUE_LINES)

    def test_pick_can_never_return_an_emi_claim_from_either_pool(self):
        """Exhaustive over both pools -- not a sample."""
        for pool in (K.TRUST_LINES, K.FEES_VALUE_LINES):
            for line in pool:
                assert not _affirms_emi(line)

    @pytest.mark.parametrize("msg", [
        "what is the fee", "price ethra", "cost of course", "vila ethra",
        "fees please", "course fee details",
    ])
    def test_smart_fallback_never_affirms_emi(self, msg):
        out = ai_service.smart_fallback("Cust", msg)
        assert not _affirms_emi(out), f"smart_fallback affirms EMI: {out!r}"

    def test_smart_fallback_still_answers_a_fee_question(self):
        """The removal must not gut the reply."""
        out = ai_service.smart_fallback("Cust", "what is the fee")
        assert "FEES" in out and "COURSES" in out
        assert len(out) > 60


# ═══ 4 — the whole-message invariant, exhaustively ════════════════════════

class TestNoContradictionInOneMessage:

    def test_fees_reply_for_a_no_emi_course_never_affirms_emi(self, seeded):
        """THE defect. fees_reply appends pick(TRUST_LINES) after the
        per-course line. Run enough draws to cover the pool many times over,
        with a fixed seed for reproducibility."""
        random.seed(1234)
        with _APP.app_context():
            for _ in range(200):
                text, _preset = cta.fees_reply("NOEMI", OX)
                bad = _affirms_emi(text)
                assert not bad, f"contradiction in one message: {bad}"

    def test_that_message_does_state_the_negative(self, seeded):
        """Anti-vacuity: the per-course EMI line must still be there, so the
        test above is not passing because EMI vanished entirely."""
        with _APP.app_context():
            text, _ = cta.fees_reply("NOEMI", OX)
        assert "EMI not available for this course" in text

    def test_whole_catalogue_fees_reply_never_affirms_emi(self, seeded):
        random.seed(99)
        with _APP.app_context():
            for _ in range(100):
                text, _ = cta.fees_reply("", OX)
                assert not _affirms_emi(text)

    def test_objections_never_affirms_emi(self, seeded):
        """objections.handle_objection('fees_high', ...) draws from
        FEES_VALUE_LINES -- the second unconditional claim site. Driven
        through the real entry point, detect_objection -> handle_objection,
        with its actual (kind, name, st) signature."""
        random.seed(7)
        probes = ["expensive", "costly", "fee kooduthal", "budget", "afford"]
        kinds = {objections.detect_objection(p) for p in probes}
        assert "fees_high" in kinds, f"probe set did not reach fees_high: {kinds}"

        for _ in range(200):
            text, _preset = objections.handle_objection(
                "fees_high", "Cust", {})
            bad = _affirms_emi(text)
            assert not bad, f"objections affirms EMI: {bad}"

    def test_the_objection_reply_is_still_substantive(self, seeded):
        """Anti-vacuity: removing one pool entry must not empty the reply."""
        text, preset = objections.handle_objection("fees_high", "Cust", {})
        assert preset == "FEES" and len(text) > 80


# ═══ 5 — per-course EMI behaviour is UNCHANGED ════════════════════════════

class TestPerCourseEmiUnchanged:

    def test_an_emi_course_still_states_it(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("HASEMI", OX)
            det = screens.course_details("HASEMI", OX)
            card = router.msg_course_detail("HASEMI", OX)
            idx, _d = cat.catalogue_index_with_provenance(OX)
        assert "EMI Available (on tuition)" in text
        assert "✅ EMI Available (on tuition)" in det.body
        assert "EMI Available (on tuition)" in card[0]
        assert "EMI available" in next(e for e in idx if e.startswith("HASEMI"))

    def test_a_no_emi_course_still_states_the_negative(self, seeded):
        with _APP.app_context():
            text, _ = cta.fees_reply("NOEMI", OX)
            det = screens.course_details("NOEMI", OX)
            card = router.msg_course_detail("NOEMI", OX)
            idx, _d = cat.catalogue_index_with_provenance(OX)
        assert "EMI not available for this course" in text
        assert "EMI" not in det.body
        assert "EMI" not in card[0]
        assert "No EMI" in next(e for e in idx if e.startswith("NOEMI"))

    def test_the_stored_flag_is_untouched(self, seeded):
        with _APP.app_context():
            for code, expected in (("NOEMI", False), ("HASEMI", True)):
                row = next(r for r in TenantKnowledge.query.filter_by(
                    tenant_id=OX).all()
                    if json.loads(r.attributes)["commercial"]["code"] == code)
                c = json.loads(row.attributes)["commercial"]
                assert c["emi_available"] is expected
                assert cat.get_course(OX, code).emi_available is expected

    def test_catalogue_record_semantics_unchanged(self, seeded):
        """bool() coercion and the default-catalogue False are untouched."""
        with _APP.app_context():
            assert cat.get_course(OX, "HASEMI").emi_available is True
            assert cat.get_course(OX, "NOEMI").emi_available is False
            defaults = cat.list_courses("t-nobody")
        assert defaults and all(c.emi_available is False for c in defaults)

    def test_tenant_isolation_of_the_emi_claim(self, seeded):
        with _APP.app_context():
            assert cat.get_course(B, "HASEMI") is None
            b_idx, b_default = cat.catalogue_index_with_provenance(B)
        assert b_default is True
        assert not any("HASEMI" in e for e in b_idx)


# ═══ 6 — source contract: nothing else was touched ════════════════════════

class TestScopeContract:

    def test_only_the_three_claim_sites_lost_their_emi_text(self):
        """The per-course renderers must still contain their EMI strings."""
        for rel, needle in (
            ("app/bot/cta_handlers.py", "EMI Available (on tuition)"),
            ("app/bot/cta_handlers.py", "EMI not available for this course"),
            ("app/bot/router.py", "EMI Available (on tuition)"),
            ("app/bot/screens.py", "EMI Available (on tuition)"),
            ("app/services/catalogue_service.py", "No EMI"),
        ):
            src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
            assert needle in src, f"{rel} lost {needle!r}"

    def test_emi_available_is_still_read_from_commercial(self):
        src = open(os.path.join(_ROOT, "app/services/catalogue_service.py"),
                   encoding="utf-8").read()
        assert 'emi_available=bool(commercial.get("emi_available"))' in src

    def test_no_admin_writer_was_introduced(self):
        """B1 must not implement the Phase B admin field."""
        src = open(os.path.join(_ROOT, "app/services/knowledge_admin_service.py"),
                   encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_merge_attributes")
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)):
                assert getattr(node.targets[0].slice, "value", None) != "emi_available"
        assert "emi_available" not in src

    def test_untouched_files_stay_untouched(self):
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/catalogue_service.py",
             "app/services/knowledge_admin_service.py",
             "app/services/knowledge_service.py",
             "app/services/payment_link_service.py",
             "app/routes/tenant.py", "app/models.py", "migrations/"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", f"out-of-scope file changed: {out}"
