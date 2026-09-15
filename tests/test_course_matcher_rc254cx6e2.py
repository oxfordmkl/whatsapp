"""RC2.5.4c-x-6e2 — code-aware, ambiguity-preserving, tenant-neutral course
matching (catalogue_service.match_keyword).

WHY
---
The x-6e1 audit found one shared matcher defect behind several symptoms:

  * every keyword matched as a plain SUBSTRING, so a keyword inside another
    word selected a course -- "dgstp" -> a course keyed "gst" (the platform
    default "GST & Taxation"), "asap" -> "sap", "encoding" -> "coding",
    "cobweb" -> "web", "totally" -> "tally";
  * each course was scored by its FIRST matching keyword, so the "longest
    wins" rule compared the wrong lengths;
  * codes and titles were never consulted, so a typed title was taken by
    another course's keyword -- on the audited Oxford-shaped catalogue
    "Java Programming" -> PYTHON, and one "pay" later PYTHON's payment link.

A false course match propagates into conversation state, the AI's course
context, the fee reply, enrolment and the transaction's course identity.
Ambiguity is therefore always preferred to a guess.

THREE CATALOGUES, ONE ALGORITHM
-------------------------------
  * the platform DEFAULT catalogue (course 5 = "GST & Taxation");
  * an AUTHORED catalogue in the shape of Oxford's 16 production courses
    (code DGSTP = "DGSTP – Goods & Services Tax Practice") -- test data here,
    never a branch in the implementation;
  * a SYNTHETIC second tenant with unrelated codes, titles and keywords.

Default course 5 and the authored DGSTP are distinct and stay distinct.
Payment URLs in this file are https://pay.invalid/... dummies.
"""
import ast
import json
import os
import sys
import tempfile
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6e2_matcher.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx6e2-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx6e2-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx6e2-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "")

for _m in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.bot import constants as K                                      # noqa: E402
from app.bot import router                                              # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402
from app.state import get_or_create_state                               # noqa: E402

OX = "t-ox"        # authored, Oxford-shaped
BARE = "t-bare"    # no catalogue -> platform default
SYN = "t-syn"      # synthetic second tenant

_APP = create_app()
_APP.config["TESTING"] = True

PAY = "https://pay.invalid/"

# (code, title, keywords, categories, has_payment_url) -- the shape of the
# audited production catalogue, used ONLY as test data.
OXFORD_SHAPED = [
    ("PGDCA", "PGDCA – Computer Applications", ["pgdca", "pgd"], ["job"], True),
    ("AIDM", "AIDM – Digital Marketing", ["aidm", "digital marketing", "digital"], ["business"], True),
    ("PDCFA", "PDCFA – Computerised Financial Accounting",
     ["pdcfa", "financial accounting", "tally prime"], ["accounting"], True),
    ("PYTHON", "Python Programming", ["python", "programming", "coding"], ["job", "business"], True),
    ("JAVA", "Java Programming", ["java"], ["job"], True),
    ("DJANGO", "Python Django – Web Development", ["django"], ["job", "business"], False),
    ("DGSTP", "DGSTP – Goods & Services Tax Practice", ["gst", "taxation", "tally"], ["accounting"], False),
    ("DCA", "DCA Fast Track – Computer Applications", ["dca fast track", "fast track"], ["job", "basic"], True),
    ("DCA-REGULAR", "DCA Regular – Computer Applications", ["dca regular"], ["job", "basic"], True),
    ("CTTC", "CTTC – Computer Teacher Training", ["cttc", "teacher", "teaching"], ["basic"], False),
    ("CORPORATE-ACCOUNTING", "Corporate Business Accounting & Taxation",
     ["corporate", "corporate accounting", "accounting"], ["accounting"], False),
    ("CWPDE", "CWPDE – Word Processing & Data Entry",
     ["cwpde", "data entry", "typing", "word processing"], ["basic"], True),
    ("DOA", "DOA – Office Automation", ["doa", "office automation"], ["basic"], True),
    ("PDDTP", "PDDTP – Desktop Publishing", ["pddtp", "dtp", "desktop publishing"], ["basic"], False),
    ("PDWD", "PDWD – Web Designing using PHP",
     ["pdwd", "web", "web design", "web designing", "php", "html"], ["job", "business"], False),
    ("WORDPRESS", "WordPress – Website Development", ["wordpress"], ["business", "job"], False),
]
DGSTP_BODY = "\U0001f4da *Diploma in Goods and Services Tax Practice* (authored body)"

# A second tenant whose catalogue shares nothing with the other two.
SYNTHETIC = [
    ("YOGA", "Hatha Yoga Teacher Training", ["hatha", "yoga teacher"]),
    ("MED-1", "Mindful Meditation", ["meditation", "yoga"]),
    # Codes deliberately NOT ordinary words: a code that is a word ("bass")
    # counts as a code mention, and two mentioned codes are ambiguous by rule.
    ("GTR-100", "Guitar Foundations", ["guitar", "electric bass guitar"]),
    ("BAS-200", "Bass Guitar Basics", ["bass guitar"]),
    ("ART", "Watercolour Art", ["art", "painting"]),
    ("PHOTO", "Smartphone Photography", ["photography", "photo"]),
    ("PIANO-1", "Piano Level One", ["beginner piano"]),
    ("PIANO-10", "Piano Level Ten", ["advanced piano"]),
    ("DESIGN-A", "Interior Design", ["design"]),
    ("DESIGN-B", "Fashion Design Studio", ["design"]),
]


def _row(tenant, code, title, keywords, categories, sort, pay=False, body=None):
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title,
        body=body or f"Body of {title}",
        attributes=json.dumps({
            "duration": "6 Months", "categories": categories, "keywords": keywords,
            "commercial": {"code": code, "currency": "INR", "normal_total_fee": 10000,
                           "base_price": 10000, "emi_available": False, "offers": [],
                           "payment_url": (PAY + code) if pay else None}}),
        is_active=True, sort_order=sort)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid in (OX, BARE, SYN):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status="ACTIVE", billing_exempt=True))
        db.session.commit()
        for i, (code, title, kws, cats, pay) in enumerate(OXFORD_SHAPED, start=1):
            db.session.add(_row(OX, code, title, kws, cats, i, pay,
                                DGSTP_BODY if code == "DGSTP" else None))
        for i, (code, title, kws) in enumerate(SYNTHETIC, start=1):
            db.session.add(_row(SYN, code, title, kws, ["job"], i))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def m(tenant, text):
    with _APP.app_context():
        return cat.match_keyword(tenant, text)


def code_of(match):
    return match.course.code if match.kind == "exact" else None


def cands(match):
    return {c.code for c in match.candidates}


def assert_exact(tenant, text, code):
    got = m(tenant, text)
    assert got.kind == "exact" and got.course.code == code, (tenant, text, got.kind, code_of(got), cands(got))


def assert_none(tenant, text):
    got = m(tenant, text)
    assert got.kind == "none", (tenant, text, got.kind, code_of(got), cands(got))


def assert_ambiguous(tenant, text, expected):
    got = m(tenant, text)
    assert got.kind == "ambiguous" and cands(got) == set(expected), (tenant, text, got.kind, code_of(got), cands(got))


# ═══ 25 — anti-vacuity: the three catalogues really are what the tests assume

class TestFixturesAreReal:

    def test_three_distinct_catalogues(self, seeded):
        with _APP.app_context():
            ox, bare, syn = cat.list_courses(OX), cat.list_courses(BARE), cat.list_courses(SYN)
        assert len(ox) == 16 and not any(c.is_default for c in ox)
        assert len(bare) == 10 and all(c.is_default for c in bare)
        assert len(syn) == 10 and not any(c.is_default for c in syn)
        assert not ({c.code for c in ox} & {c.code for c in syn})

    def test_the_matcher_does_return_courses(self, seeded):
        """A matcher that returned `none` for everything would pass every
        false-positive test in this file. It must not."""
        assert_exact(BARE, "gst", "5")
        assert_exact(OX, "gst", "DGSTP")
        assert_exact(SYN, "painting", "ART")


# ═══ Course identity: two distinct courses, both unchanged ════════════════

class TestIdentitiesStayDistinct:

    def test_gst_and_taxation_remains_default_course_5(self, seeded):
        assert K.ALL_COURSES["5"][0] == "GST & Taxation"
        with _APP.app_context():
            rec = cat.get_course(BARE, "5")
        assert rec.title == "GST & Taxation" and rec.is_default is True
        assert set(rec.keywords) == {"gst", "tally", "taxation"}

    def test_dgstp_remains_dgstp(self, seeded):
        with _APP.app_context():
            rec = cat.get_course(OX, "DGSTP")
            assert cat.get_course(BARE, "DGSTP") is None
            assert cat.get_course(OX, "5") is None
        assert rec.title == "DGSTP – Goods & Services Tax Practice" and rec.is_default is False

    def test_legacy_mapping_is_untouched(self):
        assert cat.LEGACY_NAME_TO_CODE["gst & payroll"] == "DGSTP"
        assert cat.AMBIGUOUS_TERMS == {"dca": ("DCA", "DCA-REGULAR")}


# ═══ 1, 2 — empty input and the bare-ambiguity contract ═══════════════════

class TestEmptyAndAmbiguous:

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_blank_input_is_none(self, seeded, text):
        assert m(OX, text).kind == "none"
        assert m(BARE, text).kind == "none"

    def test_bare_dca_stays_ambiguous_where_both_exist(self, seeded):
        got = m(OX, "dca")
        assert got.kind == "ambiguous" and got.course is None
        assert cands(got) == {"DCA", "DCA-REGULAR"}

    def test_dca_with_a_single_candidate_is_not_ambiguous(self, seeded):
        """The default catalogue has one DCA course -- nothing to ask."""
        assert_exact(BARE, "dca", "6")

    def test_explicit_dca_variants_still_resolve(self, seeded):
        assert_exact(OX, "dca fast track", "DCA")
        assert_exact(OX, "dca regular", "DCA-REGULAR")


# ═══ 3, 4 — whole-text code and whole-text title ══════════════════════════

class TestExactCodeAndTitle:

    @pytest.mark.parametrize("code", [c for c, *_ in OXFORD_SHAPED if c != "DCA"])
    def test_every_non_ambiguous_authored_code_typed_alone(self, seeded, code):
        assert_exact(OX, code.lower(), code)
        assert_exact(OX, code, code)

    def test_whole_text_code_beats_another_courses_keyword(self, seeded):
        """SYN course MED-1 carries the keyword "yoga"; YOGA is a CODE. The
        code identifies a course, a keyword only suggests one."""
        assert_exact(SYN, "yoga", "YOGA")

    @pytest.mark.parametrize("code, title", [(c, t) for c, t, *_ in OXFORD_SHAPED])
    def test_every_authored_title_typed_alone(self, seeded, code, title):
        assert_exact(OX, title, code)
        assert_exact(OX, title.lower(), code)

    @pytest.mark.parametrize("code, title", [(c, t) for c, t, _k in SYNTHETIC])
    def test_every_synthetic_title_typed_alone(self, seeded, code, title):
        assert_exact(SYN, title, code)

    def test_every_default_title_typed_alone(self, seeded):
        for code, (title, _card) in K.ALL_COURSES.items():
            assert_exact(BARE, title, code)

    def test_java_programming_title_is_java(self, seeded):
        """x-6e1: "programming" (PYTHON) used to take this title."""
        assert_exact(OX, "Java Programming", "JAVA")
        assert_exact(OX, "java programming", "JAVA")

    def test_pdcfa_title_is_pdcfa(self, seeded):
        """x-6e1: "accounting" (CORPORATE-ACCOUNTING) used to take this title."""
        assert_exact(OX, "PDCFA – Computerised Financial Accounting", "PDCFA")

    def test_django_title_is_django(self, seeded):
        """x-6e1: "python" and "web" tied, so this title was ambiguous."""
        assert_exact(OX, "Python Django – Web Development", "DJANGO")

    def test_default_sap_title_is_course_3(self, seeded):
        """x-6e1: "accounting" (course 8) used to take course 3's title."""
        assert_exact(BARE, "SAP Financial Accounting", "3")


# ═══ 5, 6 — codes mentioned inside a sentence ═════════════════════════════

class TestCodeInsideSentence:

    @pytest.mark.parametrize("text", ["dgstp fees?", "what is dgstp", "DGSTP course", "tell me about dgstp"])
    def test_dgstp_mentioned(self, seeded, text):
        assert_exact(OX, text, "DGSTP")

    def test_pdcfa_mentioned(self, seeded):
        assert_exact(OX, "tell me about pdcfa", "PDCFA")

    def test_dca_regular_code(self, seeded):
        assert_exact(OX, "dca-regular", "DCA-REGULAR")
        assert_exact(OX, "dca-regular fees?", "DCA-REGULAR")
        assert_exact(OX, "what is dca-regular", "DCA-REGULAR")

    def test_code_boundary_is_hyphen_aware(self, seeded):
        """PIANO-1 must not be found inside PIANO-10, nor inside PIANO-1-X."""
        assert_exact(SYN, "piano-10 fees", "PIANO-10")
        assert_exact(SYN, "piano-1 fees", "PIANO-1")
        assert_none(SYN, "piano-1x please")
        assert_none(SYN, "piano-1-advanced")

    def test_a_code_inside_a_longer_word_is_not_a_mention(self, seeded):
        assert_none(OX, "adoable")          # DOA
        assert_none(SYN, "startart")        # ART, not at a word start

    def test_a_live_ambiguous_term_is_never_treated_as_a_code(self, seeded):
        assert_ambiguous(OX, "what about dca", {"DCA", "DCA-REGULAR"})


# ═══ 7, 19, 20 — keyword word-start boundary, false positives, suffixes ═══

class TestKeywordBoundary:

    @pytest.mark.parametrize("text", ["dgstp", "DGSTP", "dgstp course", "dgstp fees?", "what is dgstp"])
    def test_dgstp_matches_nothing_on_the_default_catalogue(self, seeded, text):
        """DGSTP is not a default code, and "gst" no longer matches inside it."""
        assert_none(BARE, text)

    @pytest.mark.parametrize("tenant, text", [
        (BARE, "asap"), (BARE, "please reply asap"), (BARE, "interpretation"),
        (BARE, "encoding"), (BARE, "decoding"), (BARE, "cobweb"), (BARE, "shtml"),
        (BARE, "totally"), (OX, "encoding"), (OX, "decoding"), (OX, "cobweb"),
        (OX, "shtml"), (OX, "totally"), (SYN, "a smart choice"),
    ])
    def test_everyday_words_do_not_select_a_course(self, seeded, tenant, text):
        assert_none(tenant, text)

    @pytest.mark.parametrize("tenant, text, code", [
        (BARE, "teachers", "7"), (BARE, "pythonil", "4"), (BARE, "pgdcayude fees", "1"),
        (BARE, "digitally", "2"), (OX, "teachers", "CTTC"), (OX, "pythonil", "PYTHON"),
        (OX, "pgdcayude fees", "PGDCA"), (OX, "digitally", "AIDM"), (SYN, "artwork", "ART"),
    ])
    def test_suffix_forms_still_match(self, seeded, tenant, text, code):
        assert_exact(tenant, text, code)


# ═══ 8, 9 — longest keyword per course; ties ══════════════════════════════

class TestScoring:

    def test_longest_keyword_per_course_not_first(self, seeded):
        """GTR-100's FIRST keyword is "guitar" (6) but its longest here is
        "electric bass guitar" (20); BAS-200 has "bass guitar" (11)."""
        assert_exact(SYN, "electric bass guitar", "GTR-100")
        assert_exact(SYN, "bass guitar", "BAS-200")

    def test_codes_that_are_ordinary_words_count_as_mentions(self, seeded):
        """YOGA and ART are codes AND words: naming both is two codes."""
        assert_ambiguous(SYN, "yoga art", {"YOGA", "ART"})

    def test_longest_per_course_on_the_authored_catalogue(self, seeded):
        """CORPORATE-ACCOUNTING scores "corporate accounting" (20), not its first
        keyword "corporate"; PDCFA's best is "tally prime" (11)."""
        assert_exact(OX, "tally prime corporate accounting", "CORPORATE-ACCOUNTING")
        assert_exact(OX, "tally prime", "PDCFA")

    def test_tie_is_ambiguous(self, seeded):
        assert_ambiguous(SYN, "design", {"DESIGN-A", "DESIGN-B"})


# ═══ 10, 11, 12 — reconciling codes with keywords ═════════════════════════

class TestReconcile:

    def test_code_and_keyword_agree(self, seeded):
        assert_exact(SYN, "art painting class", "ART")
        assert_exact(OX, "python course fees?", "PYTHON")

    def test_code_with_no_keyword(self, seeded):
        assert_exact(SYN, "piano-10 please", "PIANO-10")

    def test_code_settles_a_keyword_tie_that_includes_it(self, seeded):
        assert_exact(SYN, "design-b design", "DESIGN-B")

    def test_code_and_keyword_conflict_is_ambiguous(self, seeded):
        """YOGA is mentioned as a code; the keywords point at MED-1."""
        assert_ambiguous(SYN, "yoga meditation", {"YOGA", "MED-1"})

    def test_code_outside_a_keyword_tie_is_still_ambiguous(self, seeded):
        """The keywords tie between DESIGN-A and DESIGN-B, which do NOT include
        the mentioned code YOGA. That is a disagreement, not a tie-breaker: the
        code may settle a tie only among candidates that include it."""
        assert_ambiguous(SYN, "yoga design", {"YOGA", "DESIGN-A", "DESIGN-B"})

    def test_java_programming_course_is_never_python(self, seeded):
        got = m(OX, "java programming course")
        assert code_of(got) != "PYTHON"
        assert got.kind == "ambiguous" and "JAVA" in cands(got)

    def test_several_codes_are_ambiguous(self, seeded):
        assert_ambiguous(SYN, "yoga or art", {"YOGA", "ART"})
        assert_ambiguous(OX, "pdcfa or dgstp", {"PDCFA", "DGSTP"})


# ═══ 13, 14 — the x-6e1 defect on both catalogues ═════════════════════════

class TestDefaultAndAuthoredGst:

    @pytest.mark.parametrize("text", ["gst", "tally", "taxation", "gst course", "gst & taxation"])
    def test_default_gst_navigation_still_reaches_course_5(self, seeded, text):
        assert_exact(BARE, text, "5")

    def test_default_payroll_and_numeric_codes_are_not_matched(self, seeded):
        assert_none(BARE, "payroll")
        for digit in [str(i) for i in range(1, 11)]:
            assert_none(BARE, digit)

    @pytest.mark.parametrize("text", ["dgstp", "DGSTP", "gst", "gst & payroll", "dgstp fees?", "what is dgstp"])
    def test_authored_dgstp(self, seeded, text):
        assert_exact(OX, text, "DGSTP")

    def test_authored_payroll_is_none(self, seeded):
        assert_none(OX, "payroll")

    def test_existing_discovery_contracts(self, seeded):
        assert_exact(OX, "wordpress", "WORDPRESS")
        assert_exact(OX, "python", "PYTHON")
        assert_exact(OX, "django", "DJANGO")
        assert_none(OX, "sap")
        assert_none(OX, "erp")


# ═══ keywords are discovery metadata; the code is identity ════════════════

def _edit_course(tenant, code, mutate):
    with _APP.app_context():
        for r in TenantKnowledge.query.filter_by(tenant_id=tenant, kind="course").all():
            a = json.loads(r.attributes)
            if a["commercial"]["code"] == code:
                mutate(r, a)
                r.attributes = json.dumps(a)
                db.session.commit()
                return
    raise AssertionError("no course " + code)


class TestKeywordsVersusIdentity:

    def test_clearing_keywords_does_not_clear_code_identity(self, seeded):
        assert_exact(OX, "gst", "DGSTP")
        _edit_course(OX, "DGSTP", lambda r, a: a.pop("keywords"))
        assert_none(OX, "gst")
        assert_none(OX, "taxation")
        assert_exact(OX, "dgstp", "DGSTP")
        assert_exact(OX, "dgstp fees?", "DGSTP")
        assert_exact(OX, "DGSTP – Goods & Services Tax Practice", "DGSTP")

    def test_deactivation_removes_every_route_to_the_course(self, seeded):
        assert_exact(OX, "dgstp", "DGSTP")
        _edit_course(OX, "DGSTP", lambda r, a: setattr(r, "is_active", False))
        for text in ("dgstp", "DGSTP", "dgstp fees?", "gst", "taxation",
                     "DGSTP – Goods & Services Tax Practice"):
            got = m(OX, text)
            assert code_of(got) != "DGSTP" and "DGSTP" not in cands(got), (text, got)


# ═══ 21 — tenant neutrality ═══════════════════════════════════════════════

class TestTenantNeutral:

    def test_synthetic_tenant_uses_the_same_algorithm(self, seeded):
        assert_exact(SYN, "photography class", "PHOTO")
        assert_exact(SYN, "hatha", "YOGA")
        assert_exact(SYN, "YOGA", "YOGA")

    def test_codes_and_keywords_do_not_leak_across_tenants(self, seeded):
        for text in ("yoga", "piano-10", "hatha", "design"):
            assert_none(OX, text)
            assert_none(BARE, text)
        for text in ("dgstp", "gst", "java programming", "pdcfa"):
            assert_none(SYN, text)


# ═══ 24 — no tenant-specific hardcoding in the implementation ═════════════

class TestNoHardcoding:

    @staticmethod
    def _fn():
        path = os.path.join(_ROOT, "app", "services", "catalogue_service.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "match_keyword")

    def test_no_course_tenant_or_keyword_literal(self):
        fn = self._fn()
        forbidden = ("dgstp", "gst", "taxation", "tally", "payroll", "oxford", "java",
                     "python", "pdcfa", "django", "pgdca", "cwpde", "wordpress",
                     "t-ox", "primary", "sap", "dca")
        body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) else fn.body
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for bad in forbidden:
                        assert bad not in node.value.lower(), (
                            "match_keyword hardcodes " + repr(node.value))

    def test_no_tenant_or_global_source(self):
        fn = self._fn()
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        for bad in ("os", "environ", "getenv", "current_app", "PRIMARY_TENANT_ID",
                    "_default_catalogue", "_tenant_rows", "db", "TenantKnowledge", "Tenant"):
            assert bad not in names and bad not in attrs, "match_keyword references " + bad
        calls = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert calls.count("list_courses") == 1

    def test_tenant_id_is_only_passed_to_list_courses(self):
        fn = self._fn()
        uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "tenant_id"]
        assert len(uses) == 1, "tenant_id is inspected, not only used to fetch the catalogue"


# ═══ 22, 23 — downstream: state, AI course context, payment ═══════════════

@pytest.fixture()
def router_env(seeded, monkeypatch):
    ai_calls = []

    def fake_gemini(raw, name, context=None, tenant_id=None, **kw):
        ai_calls.append(context or "")
        return "AI-REPLY"

    class FakeAssembler:
        @staticmethod
        def assemble(tenant_id=None, phone=None, wa_message_id=None, course_context=None, **kw):
            return "CTX:" + (course_context or "")

    import app.context.assembler as assembler
    monkeypatch.setattr(router, "threading",
                        types.SimpleNamespace(Thread=lambda *a, **k: types.SimpleNamespace(start=lambda: None)))
    monkeypatch.setattr(router, "update_lead_status", lambda *a, **k: None)
    monkeypatch.setattr(router, "log_lead_event_in_thread", lambda *a, **k: None)
    monkeypatch.setattr(router, "gemini_reply", fake_gemini)
    monkeypatch.setattr(assembler, "ContextAssembler", FakeAssembler)
    counter = {"n": 0}

    def say(tenant, text, stage="course_viewed", fresh=True):
        with _APP.app_context():
            if fresh:
                counter["n"] += 1
                say.phone = "+91990%07d" % counter["n"]
                st = get_or_create_state(say.phone, "Sim", tenant_id=tenant)
                st["stage"] = stage
                st["course"] = ""
            out = router.smart_reply(text, "Sim", say.phone, False, tenant_id=tenant)
            st = get_or_create_state(say.phone, "Sim", tenant_id=tenant)
            body = out[0] if isinstance(out, tuple) else str(out)
            return body or "", st["course"], st["stage"], st.get("offer_course")

    say.ai = ai_calls
    return say


class TestDownstream:

    def test_default_dgstp_does_not_select_gst_and_taxation(self, router_env):
        body, course, _stage, _offer = router_env(BARE, "dgstp")
        assert "GST & Taxation" not in body
        assert course == ""

    def test_default_dgstp_question_does_not_inject_the_gst_card(self, router_env):
        router_env(BARE, "dgstp fees?")
        assert router_env.ai, "the AI path was not reached"
        assert "Diploma in GST & Taxation" not in router_env.ai[-1]
        assert "GST & Taxation" not in router_env.ai[-1]

    def test_authored_dgstp_question_injects_dgstp_only(self, router_env):
        _body, course, _stage, _offer = router_env(OX, "dgstp fees?")
        assert course == "DGSTP – Goods & Services Tax Practice"
        assert DGSTP_BODY in router_env.ai[-1]
        assert "GST & Taxation" not in router_env.ai[-1]

    def test_default_gst_still_selects_course_5(self, router_env):
        body, course, stage, _offer = router_env(BARE, "gst")
        assert "GST & Taxation" in body
        assert course == "GST & Taxation" and stage == "course_viewed"

    def test_numeric_and_button_paths_are_untouched(self, router_env):
        body, course, _stage, _offer = router_env(BARE, "5")
        assert course == "GST & Taxation" and "GST & Taxation" in body
        body, course, _stage, _offer = router_env(BARE, "CRS:5")
        assert "GST & Taxation" in body and course == "GST & Taxation"

    def test_java_programming_course_never_reaches_pythons_payment_link(self, router_env):
        _body, course, _stage, _offer = router_env(OX, "java programming course")
        assert course != "Python Programming"
        body, _course, stage, offer = router_env(OX, "pay", fresh=False)
        assert PAY + "PYTHON" not in body
        assert offer != "PYTHON" and stage != "payment_pending"

    def test_java_programming_title_pays_for_java_only(self, router_env):
        _body, course, _stage, _offer = router_env(OX, "java programming")
        assert course == "Java Programming"
        body, _course, stage, offer = router_env(OX, "pay", fresh=False)
        assert PAY + "JAVA" in body and PAY + "PYTHON" not in body
        assert offer == "JAVA" and stage == "payment_pending"
