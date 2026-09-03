"""Phase RC2.5.5b-1: tenant-owned payment link resolver.

WHAT THIS PHASE IS
-------------------
The deterministic bot flow issues payment links from two module-level
constants (COURSE_PAYMENT_LINKS and OFFER_MENU in app/bot/constants.py),
neither of which is tenant-scoped. Four code paths read them. A second
tenant's customer selecting a course and tapping ENROLL would be handed
Oxford's Razorpay link and would pay Oxford.

b-1 builds the tenant-owned resolver and the `commercial.code` key it matches
on. It deliberately does NOT wire the resolver into any emission path -- the
constants still serve customers, byte-for-byte unchanged. Flipping the four
paths is b-2, and only after Oxford's rows carry the codes and URLs.

So the tests below prove two distinct things:
  1. the new resolver is correct and fails closed (most of the file);
  2. NOTHING customer-facing changed yet (TestPhaseBoundaryNotFlipped).

Source-level assertions are AST-based. This module's own docstrings name
COURSE_PAYMENT_LINKS, OFFER_MENU and legacy_payment_url as prose; a substring
check would match its own explanation and pass while the code was wrong.
That failure mode has recurred in RC2.5.3a/3b/4b/5a.

Import isolation follows test_platform_security_14c.py.
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

_DB = os.path.join(tempfile.gettempdir(), "rc255b1_payment_links.db")
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
from app.services import payment_link_service as pls                    # noqa: E402
from app.services import knowledge_admin_service as kas                 # noqa: E402

OX = "t-ox"          # primary tenant
B = "t-b"            # second tenant
URL_A = "https://rzp.io/rzp/OXFORD_A"
URL_B = "https://pay.example.com/tenant-b/pgdca"
LEGACY = "https://rzp.io/rzp/LEGACYONLY"

_APP = create_app()
_APP.config["TESTING"] = True


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def row(tenant_id, code=None, url=None, *, title="Course", active=True,
        kind="course", legacy=None, extra=None, sort_order=0):
    commercial = {}
    if code is not None:
        commercial["code"] = code
    if url is not None:
        commercial["payment_url"] = url
    if legacy is not None:
        commercial["legacy_payment_url"] = legacy
    attrs = {"commercial": commercial}
    if extra:
        attrs.update(extra)
    return TenantKnowledge(tenant_id=tenant_id, kind=kind, title=title,
                           body="b", attributes=json.dumps(attrs),
                           is_active=active, sort_order=sort_order)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug="ox",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=B, name="Beta", slug="beta",
                              status="ACTIVE", billing_exempt=True))
        db.session.add_all([
            row(OX, "PGDCA", URL_A, title="PGDCA"),
            row(OX, "AIDM", None, title="AIDM"),          # no link -- counselor
            row(OX, "OLDCODE", LEGACY, title="Legacy only", legacy=LEGACY),
        ])
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def resolve(tenant_id, code):
    with _APP.app_context():
        return pls.resolve_payment_url(tenant_id, code)


# ── isolation: the reason this phase exists ─────────────────────────────────

class TestTenantIsolation:

    def test_oxford_gets_its_own_url(self, seeded):
        assert resolve(OX, "PGDCA") == URL_A

    def test_tenant_b_with_no_rows_gets_nothing(self, seeded):
        assert resolve(B, "PGDCA") is None

    def test_tenant_b_never_receives_oxfords_url(self, seeded):
        """THE defect. B's customer selecting PGDCA must not pay Oxford."""
        assert resolve(B, "PGDCA") != URL_A

    def test_tenant_b_with_its_own_url_gets_only_that(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "PGDCA", URL_B, title="B's PGDCA"))
            db.session.commit()
        assert resolve(B, "PGDCA") == URL_B
        assert resolve(OX, "PGDCA") == URL_A, "B's row perturbed Oxford"

    def test_neither_tenant_can_see_the_other(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "BONLY", URL_B, title="B only"))
            db.session.commit()
        assert resolve(OX, "BONLY") is None
        assert resolve(B, "PGDCA") is None

    def test_a_third_unknown_tenant_gets_nothing(self, seeded):
        assert resolve("t-does-not-exist", "PGDCA") is None


class TestTenantIdIsMandatory:

    @pytest.mark.parametrize("bad", [None, "", 0, False])
    def test_falsy_tenant_never_resolves(self, seeded, bad):
        assert resolve(bad, "PGDCA") is None

    def test_falsy_tenant_does_not_mean_any_tenant(self, seeded):
        """A missing filter would return the first matching row globally."""
        assert resolve(None, "PGDCA") is None

    def test_falsy_tenant_is_refused_before_any_query_runs(self, seeded, monkeypatch):
        """The guard must REFUSE, not merely happen to find nothing.

        Without it the function still returns None today -- but only because
        TenantKnowledge.tenant_id is NOT NULL, so `tenant_id == None` matches
        no rows. That is an accident of the schema, not a decision by this
        module: it would start returning rows the moment a nullable tenant
        column or an outer join appeared, and it burns a DB round-trip on a
        call that is already known to be invalid. So the observable contract
        is that the database is never reached at all.
        """
        touched = []

        class Tripwire:
            def filter(self, *a, **k):
                touched.append(True)
                raise AssertionError("queried the DB for a falsy tenant")

        with _APP.app_context():
            monkeypatch.setattr(TenantKnowledge, "query", Tripwire())
            for bad in (None, "", 0, False):
                assert pls.resolve_payment_url(bad, "PGDCA") is None
        assert touched == [], "the falsy-tenant guard was removed"


# ── fail-closed ─────────────────────────────────────────────────────────────

class TestFailsClosed:

    def test_row_without_a_url_returns_none(self, seeded):
        assert resolve(OX, "AIDM") is None

    def test_unknown_code_returns_none(self, seeded):
        assert resolve(OX, "NOSUCHCODE") is None

    @pytest.mark.parametrize("bad", [None, "", "   ", "has space", "a" * 33,
                                     "semi;colon", "sla/sh"])
    def test_unusable_code_returns_none(self, seeded, bad):
        assert resolve(OX, bad) is None

    def test_inactive_row_returns_none(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "OFF", URL_B, title="off", active=False))
            db.session.commit()
        assert resolve(B, "OFF") is None

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-url", "javascript:alert(1)",
                                     "data:text/html,x", "ftp://x/y", "//x/y"])
    def test_blank_or_non_http_url_returns_none(self, seeded, bad):
        with _APP.app_context():
            db.session.add(row(B, "BAD", bad, title="bad"))
            db.session.commit()
        assert resolve(B, "BAD") is None

    def test_over_long_url_returns_none(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "LONG", "https://x.example/" + "a" * 600,
                               title="long"))
            db.session.commit()
        assert resolve(B, "LONG") is None

    def test_non_course_kind_is_not_matched(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "FAQC", URL_B, title="faq", kind="faq"))
            db.session.commit()
        assert resolve(B, "FAQC") is None

    def test_malformed_attributes_json_returns_none(self, seeded):
        with _APP.app_context():
            r = row(B, "JUNK", URL_B, title="junk")
            r.attributes = "{not json"
            db.session.add(r)
            db.session.commit()
        assert resolve(B, "JUNK") is None

    def test_attributes_not_a_dict_returns_none(self, seeded):
        with _APP.app_context():
            r = row(B, "ARR", URL_B, title="arr")
            r.attributes = "[1,2,3]"
            db.session.add(r)
            db.session.commit()
        assert resolve(B, "ARR") is None

    def test_db_error_returns_none_not_an_exception(self, seeded, monkeypatch):
        """Fail CLOSED -- knowledge_service degrades to less context here, but
        this path would degrade to the WRONG ACCOUNT."""
        class Boom:
            def filter(self, *a, **k):
                raise RuntimeError("db down")
        with _APP.app_context():
            monkeypatch.setattr(TenantKnowledge, "query", Boom())
            assert pls.resolve_payment_url(OX, "PGDCA") is None


class TestAmbiguityFailsClosed:

    def test_two_active_rows_same_code_refuses(self, seeded):
        """Never 'first row wins' -- that makes which account gets paid a
        function of row ids."""
        with _APP.app_context():
            db.session.add_all([
                row(B, "DUP", URL_B, title="one", sort_order=0),
                row(B, "DUP", "https://pay.example.com/other", title="two",
                    sort_order=1),
            ])
            db.session.commit()
        assert resolve(B, "DUP") is None

    def test_case_variants_count_as_the_same_code(self, seeded):
        with _APP.app_context():
            db.session.add_all([
                row(B, "dup2", URL_B, title="lower"),
                row(B, "DUP2", "https://pay.example.com/other", title="upper"),
            ])
            db.session.commit()
        assert resolve(B, "DUP2") is None, "case drift created two matches"

    def test_deactivating_one_duplicate_restores_resolution(self, seeded):
        with _APP.app_context():
            db.session.add_all([
                row(B, "DUP3", URL_B, title="live"),
                row(B, "DUP3", "https://pay.example.com/other", title="dead",
                    active=False),
            ])
            db.session.commit()
        assert resolve(B, "DUP3") == URL_B


# ── legacy_payment_url stays quarantined ────────────────────────────────────

class TestLegacyPaymentUrlIsNeverUsed:

    def test_row_with_only_a_legacy_url_resolves_to_nothing(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, "LEGONLY", None, title="legacy",
                               legacy=LEGACY))
            db.session.commit()
        assert resolve(B, "LEGONLY") is None

    def test_active_url_wins_and_legacy_is_not_consulted(self, seeded):
        assert resolve(OX, "OLDCODE") == LEGACY, "precondition: same value"
        with _APP.app_context():
            db.session.add(row(B, "BOTH", URL_B, title="both", legacy=LEGACY))
            db.session.commit()
        assert resolve(B, "BOTH") == URL_B

    def test_resolver_source_never_names_legacy_as_a_read(self):
        """AST: the string may appear only inside the exclusion frozenset,
        never as a dict lookup."""
        tree = ast.parse(_src("app/services/payment_link_service.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == "legacy_payment_url":
                        raise AssertionError("resolver reads legacy_payment_url")


# ── the resolver reads no Oxford constant ───────────────────────────────────

class TestResolverReadsNoGlobalConstants:

    def test_does_not_import_bot_constants(self):
        tree = ast.parse(_src("app/services/payment_link_service.py"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
            elif isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
        assert not any(m.startswith("app.bot") for m in mods), \
            f"resolver imports bot modules: {mods}"

    @pytest.mark.parametrize("name", ["COURSE_PAYMENT_LINKS", "OFFER_MENU",
                                      "OFFERS_BY_CODE", "FULL_FEE_TABLE"])
    def test_never_references_an_oxford_constant(self, name):
        tree = ast.parse(_src("app/services/payment_link_service.py"))
        loaded = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert name not in loaded

    def test_has_no_fallback_return_of_a_literal_url(self):
        """No branch may return a hardcoded http(s) string."""
        tree = ast.parse(_src("app/services/payment_link_service.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "resolve_payment_url")
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str):
                assert not n.value.value.startswith("http"), \
                    "resolver returns a hardcoded URL"

    def test_query_filters_on_tenant_id(self):
        src = _src("app/services/payment_link_service.py")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "resolve_payment_url")
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert "tenant_id" in attrs, "the tenant filter is gone"
        assert "is_active" in attrs, "the is_active filter is gone"


# ── code matching, not title matching ───────────────────────────────────────

class TestStableCodeMatching:

    def test_matching_is_by_code_not_title(self, seeded):
        """Renaming the course must not break its payment link."""
        with _APP.app_context():
            r = TenantKnowledge.query.filter_by(tenant_id=OX,
                                                title="PGDCA").first()
            r.title = "Post Graduate Diploma (renamed)"
            db.session.commit()
        assert resolve(OX, "PGDCA") == URL_A

    def test_the_title_is_not_a_usable_key(self, seeded):
        assert resolve(OX, "Post Graduate Diploma") is None

    @pytest.mark.parametrize("variant", ["pgdca", "PGDCA", " pgdca ", "PgDcA"])
    def test_code_lookup_is_case_and_space_insensitive(self, seeded, variant):
        assert resolve(OX, variant) == URL_A

    def test_row_without_a_code_is_unmatchable(self, seeded):
        with _APP.app_context():
            db.session.add(row(B, None, URL_B, title="no code"))
            db.session.commit()
        assert resolve(B, "") is None
        assert resolve(B, "NOCODE") is None

    def test_normalise_code_rejects_junk(self):
        for bad in [None, "", "  ", "a b", "x" * 33, "semi;", "sla/sh", "q*"]:
            assert pls.normalise_code(bad) is None
        assert pls.normalise_code(" pgdca ") == "PGDCA"
        assert pls.normalise_code("DCA-2") == "DCA-2"
        assert pls.normalise_code("DCA_2") == "DCA_2"

    def test_has_payment_url_mirrors_resolve(self, seeded):
        with _APP.app_context():
            assert pls.has_payment_url(OX, "PGDCA") is True
            assert pls.has_payment_url(OX, "AIDM") is False
            assert pls.has_payment_url(B, "PGDCA") is False


# ── commercial.code through the admin CRUD ──────────────────────────────────

class TestCodeInAdminCrud:

    def test_code_is_stored_uppercased(self, seeded):
        with _APP.app_context():
            cleaned, errors = kas.validate_payload(
                {"title": "T", "kind": "course", "code": "pgdca"})
            assert errors == [] and cleaned["code"] == "PGDCA"

    def test_blank_code_is_none_not_an_error(self, seeded):
        with _APP.app_context():
            cleaned, errors = kas.validate_payload(
                {"title": "T", "kind": "course", "code": "  "})
            assert errors == [] and cleaned["code"] is None

    @pytest.mark.parametrize("bad", ["has space", "semi;colon", "sla/sh", "a" * 33])
    def test_invalid_code_is_rejected(self, seeded, bad):
        with _APP.app_context():
            _cleaned, errors = kas.validate_payload(
                {"title": "T", "kind": "course", "code": bad})
            assert errors, f"accepted invalid code {bad!r}"

    def test_create_then_resolve_end_to_end(self, seeded):
        with _APP.app_context():
            created, errors = kas.create_knowledge(B, {
                "title": "B Course", "kind": "course", "code": "newcode",
                "payment_url": URL_B})
            assert errors == [] and created is not None
        assert resolve(B, "NEWCODE") == URL_B

    def test_clearing_the_code_makes_the_row_unmatchable(self, seeded):
        with _APP.app_context():
            created, _ = kas.create_knowledge(B, {
                "title": "C", "kind": "course", "code": "TEMP",
                "payment_url": URL_B})
            rid = created.id
        assert resolve(B, "TEMP") == URL_B
        with _APP.app_context():
            _row, errors = kas.update_knowledge(B, rid, {
                "title": "C", "kind": "course", "code": "",
                "payment_url": URL_B})
            assert errors == []
        assert resolve(B, "TEMP") is None, "a cleared code still matched"

    def test_editing_preserves_legacy_payment_url(self, seeded):
        with _APP.app_context():
            r = TenantKnowledge.query.filter_by(tenant_id=OX,
                                                title="Legacy only").first()
            rid = r.id
            kas.update_knowledge(OX, rid, {
                "title": "Legacy only", "kind": "course", "code": "OLDCODE",
                "payment_url": LEGACY})
            stored = json.loads(TenantKnowledge.query.get(rid).attributes)
            assert stored["commercial"]["legacy_payment_url"] == LEGACY

    def test_code_is_never_writable_from_a_foreign_tenant(self, seeded):
        """RC2.5.4b's contract: a foreign row returns (None, None) -- errors is
        None to mean "not found", deliberately distinct from a list of
        validation errors, so the route renders an ordinary 404 rather than
        confirming the row exists elsewhere. What matters here is that the
        write does not land."""
        with _APP.app_context():
            r = TenantKnowledge.query.filter_by(tenant_id=OX,
                                                title="PGDCA").first()
            rid = r.id
            updated, errors = kas.update_knowledge(B, rid, {
                "title": "hijack", "kind": "course", "code": "PGDCA",
                "payment_url": URL_B})
            assert updated is None and errors is None
            after = json.loads(TenantKnowledge.query.get(rid).attributes)
            assert after["commercial"]["payment_url"] == URL_A
            assert TenantKnowledge.query.get(rid).title == "PGDCA"
        assert resolve(OX, "PGDCA") == URL_A


# ── b-1 must not have flipped anything ──────────────────────────────────────

class TestPhaseBoundaryNotFlipped:
    """b-1 builds the resolver and leaves it unwired. Customer-facing payment
    behaviour must be byte-for-byte what it was."""

    def test_constants_are_unchanged(self):
        from app.bot.constants import COURSE_PAYMENT_LINKS, OFFER_MENU
        assert len(COURSE_PAYMENT_LINKS) == 4
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"
        assert len(OFFER_MENU) == 4
        assert OFFER_MENU["4"][4] == "https://rzp.io/rzp/KAQ2C7t"

    @pytest.mark.parametrize("mod", ["app/bot/cta_handlers.py",
                                     "app/bot/offer_handlers.py",
                                     "app/bot/router.py"])
    def test_no_emission_path_imports_the_resolver_yet(self, mod):
        tree = ast.parse(_src(mod))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module)
            elif isinstance(n, ast.Import):
                names.update(a.name for a in n.names)
        assert not any("payment_link_service" in m for m in names), \
            f"{mod} was flipped to the resolver -- that is b-2, not b-1"

    @pytest.mark.parametrize("fn_name,mod", [
        ("enroll_reply", "app/bot/cta_handlers.py"),
        ("handle_pay_intent", "app/bot/offer_handlers.py"),
        ("handle_offer", "app/bot/offer_handlers.py"),
        ("handle_offer_number", "app/bot/offer_handlers.py"),
    ])
    def test_emission_paths_still_read_the_constants(self, fn_name, mod):
        """Inverted in b-2. Until then these prove nothing was flipped early."""
        tree = ast.parse(_src(mod))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == fn_name)
        loaded = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert loaded & {"COURSE_PAYMENT_LINKS", "OFFERS_BY_CODE", "OFFER_MENU"}, \
            f"{fn_name} no longer reads a constant -- b-2 happened early"

    def test_payment_link_reply_signature_unchanged(self):
        tree = ast.parse(_src("app/bot/cta_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "payment_link_reply")
        args = [a.arg for a in fn.args.args]
        assert args == ["code", "full_name", "price", "dur", "link"]

    def test_knowledge_service_does_not_depend_on_the_resolver(self):
        """The two consumers stay separate: the prompt path never calls the
        resolver, and the resolver never renders."""
        tree = ast.parse(_src("app/services/knowledge_service.py"))
        mods = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("payment_link_service" in m for m in mods)


# ── the prompt quarantine (RC2.5.5b-1 continuation) ─────────────────────────

class TestPaymentUrlNeverReachesThePrompt:
    """A payment link is a financial instrument, not a fact about a course.

    It is resolved deterministically by this phase's resolver and emitted in a
    fixed template. Handing it to a generative model invites it being surfaced
    in the wrong conversation, for the wrong course, or beside an inferred
    price. So no payment URL of either kind reaches the prompt -- while both
    remain fully readable by the code paths meant to read them.
    """

    def _row(self, tenant_id, **commercial):
        return TenantKnowledge(
            tenant_id=tenant_id, kind="course", title="PGDCA", body="b",
            attributes=json.dumps({"commercial": commercial}),
            is_active=True, sort_order=0)

    def test_active_payment_url_is_not_rendered(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(self._row(B, base_price=19540,
                                     payment_url="https://rzp.io/rzp/SECRETPAY"))
            db.session.commit()
            block = ks.render_knowledge_block(B)
        assert "SECRETPAY" not in block
        assert "payment_url" not in block
        assert "19540" in block, "row did not render at all -- test is vacuous"

    def test_legacy_payment_url_remains_excluded(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(self._row(B, base_price=1,
                                     legacy_payment_url="https://rzp.io/rzp/OLDPAY"))
            db.session.commit()
            block = ks.render_knowledge_block(B)
        assert "OLDPAY" not in block and "legacy_payment_url" not in block

    def test_both_keys_are_in_the_exclusion_set(self):
        from app.services import knowledge_service as ks
        assert ks._NON_RENDERABLE_KEYS == frozenset(
            {"legacy_payment_url", "payment_url"})

    def test_other_commercial_fields_still_render(self, seeded):
        """The exclusion is two key names, not the commercial subtree."""
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(self._row(B, base_price=19540, currency="INR",
                                     code="PGDCA",
                                     payment_url="https://rzp.io/rzp/X"))
            db.session.commit()
            block = ks.render_knowledge_block(B)
        assert "commercial.base_price: 19540" in block
        assert "commercial.currency: INR" in block
        assert "commercial.code: PGDCA" in block
        assert "rzp.io/rzp/X" not in block

    def test_excluded_at_any_nesting_depth(self, seeded):
        """Matched by bare key name, not dotted path -- the same rule the
        legacy key already relied on."""
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(TenantKnowledge(
                tenant_id=B, kind="course", title="T", body="b", is_active=True,
                sort_order=0, attributes=json.dumps({
                    "payment_url": "https://top.example/LEVEL0",
                    "marker": "KEEPME",
                    "offers": [{"payment_url": "https://deep.example/LEVEL2"}],
                })))
            db.session.commit()
            block = ks.render_knowledge_block(B)
        assert "LEVEL0" not in block and "LEVEL2" not in block
        assert "KEEPME" in block, "nothing rendered -- test is vacuous"

    def test_not_in_the_composed_system_prompt(self, seeded):
        """End-to-end: what actually reaches Gemini's system_instruction."""
        from app.services import prompt_composer
        with _APP.app_context():
            db.session.add(self._row(B, base_price=19540,
                                     payment_url="https://rzp.io/rzp/PROMPTLEAK"))
            db.session.commit()
            out = prompt_composer.compose_system_prompt(B)
        assert "PROMPTLEAK" not in out
        assert "payment_url" not in out

    def test_the_resolver_still_reads_the_field_directly(self, seeded):
        """The quarantine is rendering-time only. Storage is untouched, and
        the deterministic consumer is entirely unaffected -- that separation
        is the whole design."""
        with _APP.app_context():
            db.session.add(row(B, "RESOLVE", URL_B, title="R"))
            db.session.commit()
        assert resolve(B, "RESOLVE") == URL_B

        from app.services import knowledge_service as ks
        with _APP.app_context():
            block = ks.render_knowledge_block(B)
        assert URL_B not in block, "resolvable but must not be renderable"

    def test_exclusion_does_not_break_tenant_isolation(self, seeded):
        from app.services import knowledge_service as ks
        with _APP.app_context():
            db.session.add(self._row(OX, base_price=111, currency="OXMARK"))
            db.session.add(self._row(B, base_price=222, currency="BMARK"))
            db.session.commit()
            ox_block = ks.render_knowledge_block(OX)
            b_block = ks.render_knowledge_block(B)
        assert "OXMARK" in ox_block and "BMARK" not in ox_block
        assert "BMARK" in b_block and "OXMARK" not in b_block
