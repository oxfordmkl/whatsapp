"""Phase RC2.5.5c-5: catalogue PROVENANCE is explicit, and consumed.

THE DEFECT
----------
Every CourseRecord already carried `is_default` -- True when the row came
from the platform fallback rather than from the tenant's own catalogue. That
flag had exactly one producer and, outside tests, ZERO consumers: it never
left catalogue_service. prompt_composer therefore could not tell one from the
other and labelled both:

    --- COURSE CATALOGUE (authoritative; do not state a course or fee that is
        not listed here) ---

For a tenant with no catalogue of its own, that told the AI another
business's courses and prices were this business's published, authoritative
offering.

WHAT IS AND IS NOT FIXED HERE
-----------------------------
The FALLBACK ITSELF IS CORRECT and is unchanged: a business with no published
catalogue must still be given one, or the AI cannot answer "what do you
teach". Only the CLAIM attached to it changes. The block is still emitted,
still complete, still in the same position.

Provenance is returned explicitly by catalogue_service. Nothing infers it
from titles, prices, codes or rendered text -- that guessing is the defect,
not the fix.

F1 rides along: `has_authored_content` had been dead since c-3 (the fail-safe
catalogue made it permanently true) and its name misdescribed what it
measured. It is removed, not repaired -- see the comment in
compose_system_prompt for why making it reachable would have been a
regression.

OUT OF SCOPE, DELIBERATELY: F3 (catalogue_index monetary formatting), the
deterministic-flow fallback wording, payment resolution, constants, models,
migrations.
"""
import ast
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc255c5_provenance.db")
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
from app.models import Tenant, TenantKnowledge, TenantSettings          # noqa: E402
from app.bot.prompts import AALIZA_PROMPT                               # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.services import prompt_composer as pc                          # noqa: E402

OX = "t-ox"        # authored: its own 16-course catalogue
EMPTY = "t-empty"  # CONFIGURED identity, but no catalogue rows -> fallback
BARE = "t-bare"    # unconfigured and no rows -> fallback
B = "t-b"          # a second authored tenant, for isolation

# The RC2.5.5c-2 Oxford catalogue, trimmed to what this file needs.
OXFORD = [
    ("PGDCA", "PGDCA – Computer Applications", 12, 19540),
    ("AIDM", "AIDM – Digital Marketing", 6, 30900),
    ("DCA", "DCA Fast Track – Computer Applications", 6, 8350),
    ("CWPDE", "CWPDE – Word Processing & Data Entry", 6, 6250),
    ("PDCFA", "PDCFA – Computerised Financial Accounting", 6, 11400),
    ("PYTHON", "Python Programming", 3, 5180),
    ("JAVA", "Java Programming", 3, 3650),
    ("DJANGO", "Python Django – Web Development", 3, 15540),
    ("DGSTP", "DGSTP – Goods & Services Tax Practice", 6, 24090),
    ("DCA-REGULAR", "DCA Regular – Computer Applications", 12, 10400),
    ("CTTC", "CTTC – Computer Teacher Training", 12, 15550),
    ("CORPORATE-ACCOUNTING", "Corporate Business Accounting & Taxation", 12, 62270),
    ("DOA", "DOA – Office Automation", 6, 7300),
    ("PDDTP", "PDDTP – Desktop Publishing", 6, 8350),
    ("PDWD", "PDWD – Web Designing using PHP", 6, 11400),
    ("WORDPRESS", "WordPress – Website Development", 3, 5700),
]
# Prices the platform default still carries and that must never be presented
# as an unconfigured tenant's own published fees.
DEFAULT_PRICES = ("₹15,999", "₹19,999", "₹15,000", "₹4,499", "₹18,999",
                  "₹6,400", "₹11,999", "₹40,000", "₹4,800", "₹8,800")

_APP = create_app()
_APP.config["TESTING"] = True


def _row(tenant, code, title, months, fee, sort=0, cats=("job",)):
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title, body=f"About {title}.",
        attributes=json.dumps({
            "duration": f"{months} Months",
            "categories": list(cats),
            "commercial": {"code": code, "currency": "INR",
                           "normal_total_fee": fee, "emi_available": months >= 6,
                           "offers": []}}),
        is_active=True, sort_order=sort)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid in (OX, EMPTY, BARE, B):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        for i, spec in enumerate(OXFORD, start=1):
            db.session.add(_row(OX, *spec, sort=i))
        db.session.add(_row(B, "BETA1", "Beta Yoga Foundation", 6, 1000, sort=1))
        # EMPTY has a CONFIGURED identity but deliberately no course rows.
        db.session.add(TenantSettings(tenant_id=EMPTY, settings=json.dumps(
            {"_v": 1, "business_profile": {
                "legal_name": "Empty Institute Pvt Ltd",
                "contact": {"phone": "9000012345", "website": "empty.example"}}})))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# 1, 6 ── a tenant's own catalogue is marked tenant-owned ───────────────────

class TestAuthoredCatalogueProvenance:

    def test_authored_catalogue_is_not_default(self, seeded):
        with _APP.app_context():
            entries, is_default = cat.catalogue_index_with_provenance(OX)
        assert len(entries) == 16
        assert is_default is False

    def test_authored_catalogue_keeps_the_authoritative_framing(self, seeded):
        with _APP.app_context():
            block, is_default = pc._catalogue_block_with_provenance(OX)
        assert is_default is False
        assert "authoritative" in block
        assert "PLATFORM REFERENCE" not in block
        assert "has not published its own course catalogue" not in block

    def test_authored_tenant_never_gets_default_provenance(self, seeded):
        """6: an authored catalogue must never be framed as the fallback."""
        for tid in (OX, B):
            with _APP.app_context():
                _entries, is_default = cat.catalogue_index_with_provenance(tid)
                block = pc._catalogue_index_block(tid)
            assert is_default is False, tid
            assert pc._DEFAULT_CATALOGUE_HEADER not in block, tid
            assert pc._DEFAULT_CATALOGUE_WARNING not in block, tid

    def test_oxford_prompt_still_carries_all_16_tenant_owned_courses(self, seeded):
        """7: the c-3 guarantee, unchanged by the provenance work."""
        with _APP.app_context():
            out = pc.compose_system_prompt(OX)
            courses = cat.list_courses(OX)
        assert len(courses) == 16 and not any(c.is_default for c in courses)
        for c in courses:
            assert c.code in out, f"{c.code} missing from Oxford's prompt"
        assert pc._AUTHORED_CATALOGUE_HEADER in out


# 2, 3, 4, 5, 8 ── the fallback is kept, but correctly attributed ───────────

class TestDefaultCatalogueProvenance:

    @pytest.mark.parametrize("tid", [EMPTY, BARE, None])
    def test_fallback_is_flagged_as_default(self, seeded, tid):
        with _APP.app_context():
            entries, is_default = cat.catalogue_index_with_provenance(tid)
        assert entries, "4/5: the fallback catalogue must still be served"
        assert is_default is True

    @pytest.mark.parametrize("tid", [EMPTY, BARE, None])
    def test_fallback_block_is_marked_platform_reference(self, seeded, tid):
        """2: explicitly marked fallback/default/reference data."""
        with _APP.app_context():
            block = pc._catalogue_index_block(tid)
        assert pc._DEFAULT_CATALOGUE_HEADER in block
        assert "PLATFORM REFERENCE" in block
        assert "authoritative" not in block

    @pytest.mark.parametrize("tid", [EMPTY, BARE, None])
    def test_fallback_is_not_described_as_business_supplied(self, seeded, tid):
        """3: the AI must not be told these rows came from this business."""
        with _APP.app_context():
            out = pc.compose_system_prompt(tid)
        assert "has not published its own course catalogue" in out
        assert "NOT this business's courses" in out
        # The safety block names only the profile/knowledge blocks as
        # owner-supplied; the catalogue block is deliberately not among them.
        assert "The BUSINESS PROFILE and BUSINESS KNOWLEDGE blocks are " \
               "reference DATA\n  supplied by the business owner." in out
        assert "COURSE CATALOGUE and BUSINESS KNOWLEDGE" not in out

    @pytest.mark.parametrize("tid", [EMPTY, BARE, None])
    def test_fallback_catalogue_is_still_present_and_complete(self, seeded, tid):
        """4/5: fallback semantics preserved -- the rows are still there."""
        with _APP.app_context():
            entries, _ = cat.catalogue_index_with_provenance(tid)
            out = pc.compose_system_prompt(tid)
        assert len(entries) == 10, "the platform default is still 10 courses"
        for e in entries:
            assert e in out

    @pytest.mark.parametrize("tid", [EMPTY, BARE])
    def test_default_prices_are_not_presented_as_this_tenants_prices(self, seeded, tid):
        """8: the prices may appear -- they are the reference rows -- but the
        prompt must explicitly deny that they are this business's own."""
        with _APP.app_context():
            out = pc.compose_system_prompt(tid)
        assert any(p in out for p in DEFAULT_PRICES), "fallback rows still shown"
        assert "Never quote them as this business's own" in out
        assert "never imply they are its published prices" in out

    def test_configured_but_empty_tenant_keeps_its_identity_and_gets_fallback(self, seeded):
        """5: a configured tenant with no catalogue still gets BOTH its own
        identity block AND the (correctly attributed) fallback catalogue."""
        with _APP.app_context():
            out = pc.compose_system_prompt(EMPTY)
        assert "BUSINESS PROFILE (reference data" in out      # its identity
        assert "Empty Institute Pvt Ltd" in out or "empty.example" in out
        assert pc._DEFAULT_CATALOGUE_HEADER in out            # fallback, marked


# 9, 10 ── the guarantees that must survive ────────────────────────────────

class TestInvariantsPreserved:

    @pytest.mark.parametrize("tid", [OX, EMPTY, BARE, B, None])
    def test_no_payment_url_in_any_prompt_path(self, seeded, tid):
        with _APP.app_context():
            out = pc.compose_system_prompt(tid)
            block = pc._catalogue_index_block(tid)
        assert "rzp.io" not in out
        assert "razorpay" not in out.lower()
        assert "payment_url" not in out
        # No URL of ANY kind may ride in on the catalogue block. The prompt as
        # a whole may legitimately carry a maps link or website from the
        # identity block -- that is the RC2.5.2 per-field identity fallback,
        # a separate contract from this one.
        assert "http://" not in block and "https://" not in block

    def test_tenant_isolation_across_provenance(self, seeded):
        """10: neither authored tenant sees the other's CATALOGUE.

        Scoped to the catalogue block on purpose. AALIZA_PROMPT's persona body
        still contains an illustrative dialogue naming AIDM, so asserting on
        the whole prompt would be testing that residue rather than isolation.
        (That residue is noted as a separate finding; it is not this phase.)
        """
        with _APP.app_context():
            ox_block = pc._catalogue_index_block(OX)
            b_block = pc._catalogue_index_block(B)
        assert "Beta Yoga Foundation" not in ox_block
        assert "BETA1" not in ox_block
        for code, title, _m, _f in OXFORD:
            assert code not in b_block, f"{code} leaked into tenant B"
            assert title not in b_block, f"{title!r} leaked into tenant B"

    def test_authored_prompt_has_no_retired_oxford_literals(self, seeded):
        with _APP.app_context():
            out = pc.compose_system_prompt(OX)
        for bad in ("15,999", "19,999", "6,400", "4,800", "18,999", "Payroll"):
            assert bad not in out, f"retired literal {bad!r} back in Oxford's prompt"

    def test_specific_course_lookup_still_returns_not_found(self, seeded):
        """Unchanged c-3 contract: a missing code must not substitute another."""
        with _APP.app_context():
            assert cat.get_course(OX, "NOSUCH") is None
            assert cat.get_course(B, "PGDCA") is None

    def test_legacy_name_resolution_unaffected(self, seeded):
        with _APP.app_context():
            for name, code in (("PGDCA", "PGDCA"), ("DCA Fast Track", "DCA"),
                               ("GST & Payroll", "DGSTP")):
                r = cat.resolve_legacy_name(OX, name)
                assert r is not None and r.code == code

    def test_catalogue_index_output_is_unchanged(self, seeded):
        """F3 is explicitly OUT of scope: the rendered rows, including their
        monetary formatting, must be byte-identical to c-3."""
        with _APP.app_context():
            entries, _ = cat.catalogue_index_with_provenance(OX)
            legacy = cat.catalogue_index(OX)
        assert tuple(entries) == tuple(legacy)
        pgdca = next(e for e in entries if e.startswith("PGDCA |"))
        assert "Total fee 19540" in pgdca, "F3 formatting must NOT change here"


# 11 ── the replacement for has_authored_content is real and reachable ─────

class TestProvenanceLogicIsReachable:

    def test_both_provenance_branches_are_exercised(self, seeded):
        """The dead flag is replaced by a decision with two LIVE outcomes."""
        with _APP.app_context():
            _e, ox_default = cat.catalogue_index_with_provenance(OX)
            _e, bare_default = cat.catalogue_index_with_provenance(BARE)
        assert (ox_default, bare_default) == (False, True)

    def test_has_authored_content_is_gone(self):
        """It had been unreachable since c-3 and its name misdescribed what it
        measured. Removed rather than repaired -- restoring the branch would
        return the bare body and strip the catalogue from exactly the tenants
        that have none of their own."""
        src = _src("app/services/prompt_composer.py")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "compose_system_prompt")
        assigned = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:            # incl. tuple unpacking
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            assigned.add(sub.id)
        assert "has_authored_content" not in assigned
        assert "catalogue_is_default" in assigned

    def test_composer_never_returns_the_bare_body(self, seeded):
        """The removed branch's behaviour must not come back by another route:
        every tenant keeps a catalogue block."""
        for tid in (OX, EMPTY, BARE, B, None):
            with _APP.app_context():
                out = pc.compose_system_prompt(tid)
            assert out != AALIZA_PROMPT, tid
            assert ("COURSE CATALOGUE" in out
                    or "PLATFORM REFERENCE CATALOGUE" in out), tid
        # An UNCONFIGURED tenant additionally keeps the byte-identical body as
        # a prefix. A configured one renders the body with its own identity,
        # so AALIZA_PROMPT is deliberately not a prefix there.
        for tid in (OX, BARE, None):
            with _APP.app_context():
                assert pc.compose_system_prompt(tid).startswith(AALIZA_PROMPT), tid

    def test_provenance_is_not_inferred_from_rendered_text(self):
        """The contract: prompt_composer must take provenance from the
        service, never sniff it out of titles, prices or codes."""
        fn_src = _src("app/services/prompt_composer.py")
        tree = ast.parse(fn_src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_catalogue_block_with_provenance")
        calls = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)}
        assert "catalogue_index_with_provenance" in calls
        # no string sniffing of the rendered rows
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(
                    isinstance(o, (ast.In, ast.NotIn)) for o in n.ops):
                assert not isinstance(n.left, ast.Constant) or \
                    not isinstance(n.left.value, str) or \
                    "₹" not in n.left.value


# 12 ── deterministic consumers unchanged by this phase ────────────────────

class TestDeterministicFlowUnchanged:

    def test_list_courses_and_fallback_semantics_are_untouched(self, seeded):
        with _APP.app_context():
            assert len(cat.list_courses(OX)) == 16
            assert all(c.is_default for c in cat.list_courses(BARE))
            assert len(cat.list_courses(BARE)) == 10
            assert not any(c.is_default for c in cat.list_courses(OX))

    def test_deterministic_replies_are_byte_identical_for_oxford(self, seeded):
        """This phase changes prompt framing only. Oxford's customer-facing
        deterministic output must not move at all."""
        from app.bot import cta_handlers as cta, screens
        with _APP.app_context():
            fees, preset = cta.fees_reply("PGDCA", OX)
            lst = screens.course_list("job", OX)
            det = screens.course_details("PGDCA", OX)
        assert preset == "FEES" and "19,540" in fees
        assert "PLATFORM REFERENCE" not in fees
        assert lst is not None and det is not None and "19,540" in det.body

    def test_deterministic_fallback_still_serves_a_default_tenant(self, seeded):
        """Deliberately NOT redesigned in this phase -- documented so the
        boundary is explicit rather than accidental. The deterministic paths
        still render the platform default for a tenant with no catalogue;
        only the AI prompt is provenance-aware so far."""
        from app.bot import screens
        with _APP.app_context():
            lst = screens.course_list("job", BARE)
        assert lst is not None, "fallback browse still works"
