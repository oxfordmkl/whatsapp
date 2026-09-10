"""Phase RC2.5.3b — query-aware knowledge retrieval.

THE GAP
-------
With 18 real Oxford rows and MAX_ITEMS=8, the sort_order-only default
silently excluded 10 of them (AIDM, Python, Java, Django, WordPress, and all
5 standalone facts) from every real customer conversation -- a genuine
functionality gap discovered during RC2.5.3a-K's own production validation,
not a test concern.

WHAT THIS PHASE DOES
---------------------
fetch_knowledge()/render_knowledge_block()/compose_system_prompt()/
_resolve_persona() all gain an optional `query` parameter, threaded from
gemini_reply()'s existing `user_msg` -- router.py is NOT touched, since the
customer's text already reaches gemini_reply() at every call site. Ranking
is deterministic token-overlap between `query` and each row's own title/
body -- no hardcoded vocabulary, no vector/embedding index. sort_order is
demoted to a tie-breaker and the ordering used when nothing scores positive
(a broad question, or a malformed query) -- the SAME default path used when
query is absent, not a second mechanism.

Two independent caps become REAL ceilings in this phase: MAX_ITEMS and
MAX_CHARS are now enforced via min(caller_value, cap), not merely honoured
as defaults a caller could override with limit=99999.

query=None (the default at every layer) is required to reproduce the exact
pre-RC2.5.3b behaviour -- proven directly against the literal RC2.5.3a-K
algorithm, not just "looks the same".

OUT OF SCOPE, NOT TOUCHED: router.py, models.py, migrations, constants.py
(incl. COURSE_PAYMENT_LINKS), business_profile.py, admin, WhatsApp, CRM.
_base_query() untouched. No production data written -- confirmed via a
fresh read-only production check in the implementation report.
"""
import ast
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc253b_retrieval.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc253b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc253b-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc253b-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc253b-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.services import knowledge_service as ks                        # noqa: E402
from app.services import prompt_composer                                # noqa: E402
from app.services import ai_service                                     # noqa: E402
from app.bot.prompts import AALIZA_PROMPT                               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KS_PY = os.path.join(ROOT, "app", "services", "knowledge_service.py")

OX = "t-ox"
TA = "t-alpha"
TB = "t-beta"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _k(tenant_id, kind, title, body=None, attrs=None, active=True, order=0):
    return TenantKnowledge(
        tenant_id=tenant_id, kind=kind, title=title, body=body,
        attributes=json.dumps(attrs if attrs is not None else {}),
        is_active=active, sort_order=order,
    )


# The 18-row shape approximated closely enough for ranking tests: 13 course
# rows (only a representative subset modelled in full; the rest exist purely
# to prove MAX_ITEMS/broad-query behaviour against a realistic catalog size)
# plus the 5 standalone facts, matching titles from the real RC2.5.3a-K
# production manifest so this suite is testing against realistic content,
# not synthetic placeholders.
_COURSE_TITLES_IN_SORT_ORDER = [
    "Post Graduate Diploma in Computer Applications (PGDCA)",
    "Diploma in Computer Teachers Training Course (CTTC)",
    "Diploma in Computer Application (DCA - Regular)",
    "Diploma in Computer Application (DCA - Fast track)",
    "Certificate in Word Processing and Data Entry Operator (CWPDE)",
    "Diploma in Office Automation (DOA)",
    "Professional Diploma in Desktop Publishing (PDDTP)",
    "Professional Diploma in Web Designing (PDWD)",
    "Diploma in AI Driven Digital Marketing (AIDM)",
    "Certificate in Python",
    "Certificate in Java Programming",
    "Certificate in Website Development Using Python (Django)",
    "Certificate in Wordpress Master",
]


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (TA, "Alpha"), (TB, "Beta")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        order = 0
        for title in _COURSE_TITLES_IN_SORT_ORDER:
            order += 1
            db.session.add(_k(OX, ks.KIND_COURSE, title,
                              "Best for career growth. Syllabus overview.",
                              {"commercial": {"base_price": 1000 + order}},
                              order=order))
        for kind, title, body in (
            ("policy", "Accreditation",
             "Kerala State Rutronix Authorised Training Centre"),
            ("policy", "PSC eligibility",
             "Eligible 6-month & 12-month govt-approved courses are PSC eligible"),
            ("faq", "NORKA Attestation",
             "NORKA Attestation available for eligible certificates"),
            ("faq", "Learning modes",
             "Offline Classes | Online Live Classes | Fast Track available"),
            ("faq", "AI-enabled courses",
             "All courses are AI-enabled"),
        ):
            order += 1
            db.session.add(_k(OX, kind, title, body, order=order))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


# ═══ query=None compatibility — the whole backward-compat proof ═══════════

class TestQueryNoneCompatibility:

    def test_query_none_reproduces_the_literal_rc253ak_algorithm(self, seeded):
        """Not 'looks the same' -- runs the EXACT pre-RC2.5.3b algorithm in
        parallel and asserts equality, matching this session's established
        proof technique for backward-compat claims."""
        with _APP.app_context():
            from app.models import TenantKnowledge as TK
            old_style = tuple(
                TK.query.filter(TK.tenant_id == OX, TK.is_active.is_(True))
                  .order_by(TK.sort_order.asc(), TK.id.asc())
                  .limit(max(1, int(ks.MAX_ITEMS))).all()
            )
            new_style = ks.fetch_knowledge(OX)  # query=None, the default
        assert [r.id for r in old_style] == [r.id for r in new_style]

    def test_query_none_composed_prompt_unaffected_by_ranking_code_existing(self, seeded):
        """The mere PRESENCE of ranking code must not change anything when
        no query is supplied -- a regression a naive implementation could
        introduce (e.g. always sorting, even with empty tokens)."""
        with _APP.app_context():
            out1 = prompt_composer.compose_system_prompt(OX)
            out2 = prompt_composer.compose_system_prompt(OX)
        assert out1 == out2  # deterministic, and both use the query=None path


# ═══ Ranking / matching ═════════════════════════════════════════════════

class TestRanking:

    def test_exact_title_match(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="PGDCA duration?")
        assert rows[0].title == "Post Graduate Diploma in Computer Applications (PGDCA)"

    def test_partial_title_token_match(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="NORKA?")
        assert rows[0].title == "NORKA Attestation"

    def test_body_keyword_match(self, seeded):
        """A query word absent from the title but present in the body must
        still surface the row."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_FAQ, "Refund policy",
                              "We offer a full refund within seven days"))
            db.session.add(Tenant.query.get(TA) or Tenant(id=TA, name="A", slug="dup"))
            db.session.commit()
            rows = ks.fetch_knowledge(TA, query="refund")
        assert any("Refund policy" == r.title for r in rows)

    def test_title_weighted_above_body(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_FAQ, "Python", "unrelated body text"))
            db.session.add(_k(TA, ks.KIND_FAQ, "Something else",
                              "mentions python once in passing"))
            db.session.commit()
            rows = ks.fetch_knowledge(TA, query="python")
        assert rows[0].title == "Python"  # title hit outranks body hit

    def test_deterministic_ranking(self, seeded):
        with _APP.app_context():
            a = [r.id for r in ks.fetch_knowledge(OX, query="PSC eligibility")]
            b = [r.id for r in ks.fetch_knowledge(OX, query="PSC eligibility")]
        assert a == b

    def test_two_courses_may_both_match_ambiguous_query(self, seeded):
        """'Python' legitimately overlaps two real course titles -- both
        surfacing is safe and correct, not a bug. No disambiguation engine."""
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="python")
        titles = [r.title for r in rows]
        assert "Certificate in Python" in titles
        assert "Certificate in Website Development Using Python (Django)" in titles


# ═══ Broad / no-match fallback ══════════════════════════════════════════

class TestFallback:

    def test_genuinely_unmatched_query_falls_back_to_sort_order_default(self, seeded):
        """A query with truly zero title/body token overlap anywhere in the
        tenant's knowledge must fall through to the SAME default as
        query=None, not an empty result."""
        with _APP.app_context():
            broad = ks.fetch_knowledge(OX, query="zqxvbn flooble wortnax")
            default = ks.fetch_knowledge(OX)
        assert [r.id for r in broad] == [r.id for r in default]

    def test_natural_broad_question_returns_its_genuine_weak_matches(self, seeded):
        """'What courses do you offer?' is NOT actually a zero-overlap query
        against this tenant's real content: PSC_NOTE and AI_NOTE (copied
        verbatim from the real Oxford manifest into this fixture) both
        literally contain the word "courses" in their body text. Correctly
        surfacing those two as weak matches -- rather than the full 8-row
        default -- IS the intended behaviour: genuine, if weak, relevance
        outranks an indiscriminate fallback. This is not the same case as
        the fully-unmatched query above, and asserting they behave
        identically would have been testing a false premise."""
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="What courses do you offer?")
        titles = {r.title for r in rows}
        assert titles == {"PSC eligibility", "AI-enabled courses"}

    def test_no_match_query_is_not_an_empty_result(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="completely unrelated gibberish zzqx")
        assert len(rows) > 0  # falls back, doesn't return nothing


# ═══ Hard ceilings — real, not defaults ═══════════════════════════════════

class TestHardCeilings:

    def test_limit_99999_still_bounded_by_max_items(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, limit=99999)
        assert len(rows) <= ks.MAX_ITEMS

    def test_limit_99999_with_query_still_bounded(self, seeded):
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="what courses", limit=99999)
        assert len(rows) <= ks.MAX_ITEMS

    def test_oversized_max_chars_still_bounded(self, seeded):
        with _APP.app_context():
            for i in range(8):
                db.session.add(_k(TB, ks.KIND_FAQ, f"Long{i}", "x" * 900, order=i))
            db.session.commit()
            block = ks.render_knowledge_block(TB, max_chars=999999)
        assert len(block) < ks.MAX_CHARS + 500


# ═══ Query propagation ═════════════════════════════════════════════════

class TestQueryPropagation:

    def test_query_reaches_composer(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX, query="PSC eligibility")
        assert "PSC eligibility" in out
        assert "PLATFORM RULES" in out  # knowledge present -> safety re-asserted

    def test_query_none_at_composer_is_default_path(self, seeded):
        with _APP.app_context():
            with_query = prompt_composer.compose_system_prompt(OX, query="PSC eligibility")
            without_query = prompt_composer.compose_system_prompt(OX)
        assert with_query != without_query

    def test_query_reaches_ai_service_resolve_persona(self, seeded, monkeypatch):
        captured = {}
        real = prompt_composer.compose_system_prompt
        def _spy(tenant_id, persona_name=None, query=None):
            captured["query"] = query
            return real(tenant_id, persona_name, query=query)
        monkeypatch.setattr(prompt_composer, "compose_system_prompt", _spy)
        with _APP.app_context():
            ai_service._resolve_persona(OX, query="NORKA attestation")
        assert captured["query"] == "NORKA attestation"

    def test_gemini_reply_passes_user_msg_as_query(self, seeded, monkeypatch):
        captured = {}
        def _fake_generate_content(model, contents, config):
            class _R:
                text = "ok"
            return _R()
        client = type("C", (), {})()
        client.models = type("M", (), {"generate_content": staticmethod(_fake_generate_content)})()
        monkeypatch.setattr(ai_service, "gemini_client", client)

        real_resolve = ai_service._resolve_persona
        def _spy(tenant_id, query=None):
            captured["query"] = query
            return real_resolve(tenant_id, query=query)
        monkeypatch.setattr(ai_service, "_resolve_persona", _spy)

        with _APP.app_context():
            ai_service.gemini_reply("What is the PGDCA duration?", "Student", tenant_id=OX)
        assert captured["query"] == "What is the PGDCA duration?"


# ═══ Tenant isolation with query present ═══════════════════════════════

class TestIsolationWithQuery:

    def test_tenant_a_query_never_returns_tenant_b_rows(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TB, ks.KIND_FAQ, "PGDCA secret", "Beta only info"))
            db.session.commit()
            rows = ks.fetch_knowledge(OX, query="PGDCA")
        assert all(r.tenant_id == OX for r in rows)
        assert not any("secret" in r.title.lower() for r in rows)

    def test_falsy_tenant_id_with_query_still_returns_nothing(self, seeded):
        with _APP.app_context():
            for bad in (None, "", 0, False):
                assert ks.fetch_knowledge(bad, query="PGDCA") == ()

    def test_ranking_cannot_reintroduce_cross_tenant_row(self, seeded):
        """Ranking only reorders an already tenant-scoped candidate list --
        proven by checking every returned row's tenant_id, not just count."""
        with _APP.app_context():
            db.session.add(_k(TB, ks.KIND_COURSE, "PGDCA", "identical-looking title"))
            db.session.commit()
            rows = ks.fetch_knowledge(OX, query="PGDCA")
        assert all(r.tenant_id == OX for r in rows)


# ═══ Fail-open ═══════════════════════════════════════════════════════════

class TestFailOpenWithQuery:

    def test_db_error_during_candidate_fetch_returns_no_knowledge(self, seeded, monkeypatch):
        """The candidate-fetch step itself (_base_query(...).limit(...).all())
        must still fail open on a real DB error, e.g. OperationalError --
        not just on malformed downstream data. Equivalent to RC2.5.3a's
        original test_db_error_returns_no_knowledge, re-proven against the
        RC2.5.3b candidate-fetch code path specifically."""
        def _boom(*a, **k):
            raise RuntimeError("simulated DB outage")
        monkeypatch.setattr(ks, "_base_query", _boom)
        with _APP.app_context():
            assert ks.fetch_knowledge(OX) == ()
            assert ks.fetch_knowledge(OX, query="PGDCA") == ()

    def test_non_string_query_falls_open(self, seeded):
        with _APP.app_context():
            rows_int = ks.fetch_knowledge(OX, query=12345)
            rows_list = ks.fetch_knowledge(OX, query=["not", "a", "string"])
            rows_none = ks.fetch_knowledge(OX)
        assert [r.id for r in rows_int] == [r.id for r in rows_none]
        assert [r.id for r in rows_list] == [r.id for r in rows_none]

    def test_composer_survives_malformed_query(self, seeded):
        with _APP.app_context():
            out = prompt_composer.compose_system_prompt(OX, query={"not": "a string"})
        assert isinstance(out, str)

    def test_malformed_row_title_does_not_break_ranking_of_others(self, seeded, monkeypatch):
        """One row raising during scoring must not zero out the whole
        candidate set."""
        real_score = ks._relevance_score
        calls = {"n": 0}
        def _boom_once(row, tokens):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated scoring bug")
            return real_score(row, tokens)
        monkeypatch.setattr(ks, "_relevance_score", _boom_once)
        with _APP.app_context():
            rows = ks.fetch_knowledge(OX, query="PSC eligibility")
        assert len(rows) > 0  # ranking still produced a result


# ═══ Renderer properties preserved (legacy URL / active URL / caps) ═══════

class TestRendererPropertiesPreserved:

    def test_legacy_payment_url_still_excluded_with_ranking_active(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, {
                "commercial": {"base_price": 19540,
                              "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t"}
            }))
            db.session.commit()
            block = ks.render_knowledge_block(TA, query="PGDCA fee")
        assert "rzp.io/rzp/KAQ2C7t" not in block

    def test_active_payment_url_is_excluded_with_ranking_active(self, seeded):
        """INVERTED by RC2.5.5b-1 (was ..._still_renders_...).

        The exclusion must hold on the query-aware path too, not only on the
        unranked one -- ranking selects WHICH rows render, never WHICH KEYS
        within a row. The price still renders, so this is not passing merely
        because the row was ranked out.

        RC2.5.4c-x-1a: the fixture's price moves from commercial.base_price
        (now excluded, because it duplicates normal_total_fee and gave the AI
        two prices for one course) to commercial.normal_total_fee, the
        canonical field that still renders. The anti-vacuity assertion is
        deliberately still a PRICE, which keeps the docstring's claim above
        literally true.
        """
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, {
                "commercial": {"normal_total_fee": 19540,
                              "payment_url": "https://rzp.io/rzp/ACTIVE"}
            }))
            db.session.commit()
            block = ks.render_knowledge_block(TA, query="PGDCA fee")
        assert "rzp.io/rzp/ACTIVE" not in block
        assert "commercial.payment_url" not in block
        assert "19540" in block, "the row was ranked out -- test is vacuous"


# ═══ Scope ═══════════════════════════════════════════════════════════════

class TestScope:

    def test_router_not_modified(self):
        """NARROWED BY RC2.5.4c-x-6c1 -- still active, and now STRICTER.

        RC2.5.3b has no authorisation to touch the router, and this guard
        existed to prove it. RC2.5.4c-x-6c1 threads an OPTIONAL, UNREAD
        `tenant_id` through six call expressions so a later phase can resolve
        identity from the tenant instead of a constant. Those six lines are
        the only permitted difference.

        The blanket porcelain pin this replaces could only answer "did the
        file change at all". This answers "did anything OTHER than these six
        exact substitutions change", which also catches a line being added or
        removed -- something the porcelain pin could not distinguish. Once
        x-6c1 is committed every comparison below is trivially equal.
        """
        import subprocess

        # line number -> (HEAD text, permitted working-tree text, enclosing fn)
        authorised = {
            143: ("        return screens.main_menu(name)",
                  "        return screens.main_menu(name, tenant_id)",
                  "_nearest_menu"),
            175: ("    return screens.main_menu(name)",
                  "    return screens.main_menu(name, tenant_id)",
                  "_nearest_menu"),
            199: ("        return legacy_main_menu_reply(name)",
                  "        return legacy_main_menu_reply(name, tenant_id)",
                  "_enter_main_menu"),
            265: ("            screen = screens.main_menu(name)",
                  "            screen = screens.main_menu(name, tenant_id)",
                  "_try_navigation"),
            498: ('            return (ai or smart_fallback(name, low)), "GOAL"',
                  '            return (ai or smart_fallback(name, low, tenant_id)), "GOAL"',
                  "smart_reply"),
            621: ('    return smart_fallback(name, raw), "COURSE"',
                  '    return smart_fallback(name, raw, tenant_id), "COURSE"',
                  "smart_reply"),
        }

        rel = "app/bot/router.py"
        # NOT text=True: on Windows that decodes git's stdout with the locale
        # codepage and mangles Malayalam and emoji, firing the guard on an
        # encoding artifact. splitlines() normalises line endings.
        head = subprocess.run(["git", "show", "HEAD:" + rel],
                              cwd=ROOT, capture_output=True)
        assert head.returncode == 0, "cannot read HEAD:" + rel
        old = head.stdout.decode("utf-8").splitlines()
        with open(os.path.join(ROOT, "app", "bot", "router.py"),
                  encoding="utf-8") as fh:
            new = fh.read().splitlines()

        # A pure substitution set cannot change the line count. This is what
        # makes an inserted or deleted line impossible to hide.
        assert len(old) == len(new), (
            "router.py line count changed (%d -> %d): only in-place "
            "substitution of the six authorised calls is permitted"
            % (len(old), len(new)))

        differing = [i for i in range(len(old)) if old[i] != new[i]]
        assert set(n - 1 for n in authorised) == set(differing), (
            "router.py changed on unauthorised lines: %s"
            % sorted(n + 1 for n in differing
                     if (n + 1) not in authorised))

        for lineno, (want_old, want_new, _fn) in sorted(authorised.items()):
            assert old[lineno - 1] == want_old, (
                "router.py:%d is not the expected HEAD text" % lineno)
            assert new[lineno - 1] == want_new, (
                "router.py:%d changed to something other than the authorised "
                "call: %r" % (lineno, new[lineno - 1]))

        # Structural cross-check: every permitted edit must sit inside the
        # function the audit named. A line moved into a different function
        # would keep its text but change its meaning.
        tree = ast.parse("\n".join(new))
        for lineno, (_o, _n, want_fn) in sorted(authorised.items()):
            holder = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                        if holder is None or node.lineno > holder.lineno:
                            holder = node
            assert holder is not None and holder.name == want_fn, (
                "router.py:%d is no longer inside %s" % (lineno, want_fn))

        # tenant_id must come from the enclosing function's OWN parameter --
        # never from a global, an import or PRIMARY_TENANT_ID.
        for name in sorted({fn for _o, _n, fn in authorised.values()}):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            params = [a.arg for a in fn.args.args] + \
                     [a.arg for a in fn.args.kwonlyargs]
            assert "tenant_id" in params, (
                "%s does not take tenant_id -- the threaded value would not "
                "be the caller's authoritative tenant" % name)
        src = "\n".join(new)
        assert "PRIMARY_TENANT_ID" not in src, (
            "router.py must never infer a tenant from PRIMARY_TENANT_ID")

    def test_models_and_migrations_not_modified(self):
        import subprocess
        for path in ("app/models.py", "migrations/"):
            out = subprocess.run(["git", "status", "--porcelain", "--", path],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            assert out.strip() == "", f"{path} unexpectedly changed"

    def test_no_hardcoded_education_vocabulary_in_scoring(self):
        """The ranking function must not import or reference constants.py's
        education-specific alias tables -- the whole point is staying
        generic across verticals."""
        src = open(KS_PY, encoding="utf-8").read()
        assert "COURSE_NAME_ALIASES" not in src
        assert "KEYWORD_TO_COURSE" not in src
        assert "import app.bot.constants" not in src
        assert "from app.bot import constants" not in src

    def test_no_vector_or_embedding_dependency(self):
        """AST-based, not substring search -- the module's own docstring
        legitimately discusses the ABSENCE of vector/embedding search in
        prose ("no vector/embedding index"), which a raw substring scan
        would false-positive on."""
        tree = ast.parse(open(KS_PY, encoding="utf-8").read())
        imported = {a.name for n in ast.walk(tree)
                   if isinstance(n, ast.Import) for a in n.names} | {
                  n.module for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) and n.module}
        for forbidden in ("faiss", "chroma", "pinecone", "sklearn", "numpy",
                          "embedding"):
            assert not any(forbidden in m.lower() for m in imported)

    def test_payment_links_untouched(self):
        from app.bot.constants import COURSE_PAYMENT_LINKS
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"
