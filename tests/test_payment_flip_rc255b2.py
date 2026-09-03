"""Phase RC2.5.5b-2: flip the four payment emission paths to the resolver.

WHAT CHANGED, PRECISELY
------------------------
    URL                        -> resolve_payment_url(tenant_id, code)
    code/full_name/price/dur   -> COURSE_PAYMENT_LINKS / OFFER_MENU, unchanged

The constants remain the CATALOGUE. They still supply the display copy and,
for the two name-keyed paths, the course-name -> code index. Only the URL
column (tuple index [4]) stops being read.

WHY NOT SOURCE THE PRICE FROM TENANTKNOWLEDGE TOO
--------------------------------------------------
Because it would silently re-price Oxford. The b-2 audit compared the two
sources on the four monetised courses:

    PGDCA   constant Rs.15,999   TenantKnowledge 19540   (+22%)
    AIDM    constant Rs.19,999   TenantKnowledge 30900   (+55%)
    DCA     constant Rs. 6,400   TenantKnowledge  8350   (+30%)
    CWPDE   constant Rs. 4,800   TenantKnowledge  6250   (+30%)

Those TenantKnowledge figures are the RC2.5.3a-K "FORMULA A" Rutronix
REGULATORY amounts (registration + net tuition to ATC), stored deliberately
as regulatory facts. They were never Oxford's customer-facing selling price.
What a business charges is a commercial decision, not something a refactor
gets to change on the way past -- so this phase moves the URL and nothing
else. TestOxfordPriceParity below pins that.

FAIL-CLOSED EVERYWHERE
-----------------------
No resolver result means NO LINK: no tenant, no row, inactive, blank URL,
ambiguous match, DB error, foreign tenant. Each path falls through to the
branch it already had for a course without a link -- the counselor handoff,
or the offer menu -- and critically does NOT write st["stage"] =
"payment_pending", so no conversation waits for a transaction id against a
link that was never sent.

TENANT #2 SCOPE, STATED HONESTLY
---------------------------------
st["course"] is still populated from Oxford's hardcoded ALL_COURSES catalogue,
so a second tenant's customer browses Oxford's course names, which match none
of their own codes. They therefore always reach the counselor branch. That is
SAFE -- no money can be misrouted, which is what this phase is for -- but it
is not yet FUNCTIONAL. De-Oxfordising the catalogue is RC2.5.5c.

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

_DB = os.path.join(tempfile.gettempdir(), "rc255b2_payment_flip.db")
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
from app.bot import cta_handlers as cta                                 # noqa: E402
from app.bot import offer_handlers as oh                                # noqa: E402
from app.bot.constants import COURSE_PAYMENT_LINKS, OFFER_MENU          # noqa: E402

OX = "t-ox"
B = "t-b"
URL_B = "https://pay.example.com/b/pgdca"

_APP = create_app()
_APP.config["TESTING"] = True

# course name -> (code, live url), straight from the catalogue.
OXFORD = {name: (e[0], e[4]) for name, e in COURSE_PAYMENT_LINKS.items()}


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def krow(tenant_id, code, url, *, title=None, active=True, kind="course"):
    commercial = {"code": code}
    if url is not None:
        commercial["payment_url"] = url
    return TenantKnowledge(tenant_id=tenant_id, kind=kind,
                           title=title or code, body="b",
                           attributes=json.dumps({"commercial": commercial}),
                           is_active=active, sort_order=0)


@pytest.fixture()
def seeded():
    """Oxford's four monetised courses carry exactly the catalogue's URLs, so
    the flip is expected to be output-invisible for Oxford."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug="ox",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=B, name="Beta", slug="beta",
                              status="ACTIVE", billing_exempt=True))
        for name, (code, url) in OXFORD.items():
            db.session.add(krow(OX, code, url, title=name))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def state(course=""):
    return {"course": course, "stage": "start", "offer_course": ""}


# ── Oxford parity: the central proof ────────────────────────────────────────

class TestOxfordByteIdenticalOutput:
    """For every monetised course, each path must produce EXACTLY the text it
    produced before the flip. The pre-flip text is reconstructed here from the
    catalogue -- the same five values the old code unpacked."""

    def expected(self, name):
        code, full, price, dur, link = COURSE_PAYMENT_LINKS[name]
        return cta.payment_link_reply(code, full, price, dur, link)

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_enroll_reply_output_unchanged(self, seeded, name):
        with _APP.app_context():
            got = cta.enroll_reply("Alice", name, state(name), OX)
        assert got == self.expected(name)

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_handle_pay_intent_output_unchanged(self, seeded, name):
        with _APP.app_context():
            got = oh.handle_pay_intent(state(name), OX)
        assert got == self.expected(name)

    @pytest.mark.parametrize("digit", sorted(OFFER_MENU))
    def test_handle_offer_output_unchanged(self, seeded, digit):
        code, full, price, dur, link = OFFER_MENU[digit]
        with _APP.app_context():
            got = oh.handle_offer(code, state(), OX)
        assert got == cta.payment_link_reply(code, full, price, dur, link)

    @pytest.mark.parametrize("digit", sorted(OFFER_MENU))
    def test_handle_offer_number_output_unchanged(self, seeded, digit):
        code, full, price, dur, link = OFFER_MENU[digit]
        with _APP.app_context():
            got = oh.handle_offer_number(digit, state(), OX)
        assert got == cta.payment_link_reply(code, full, price, dur, link)

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_the_live_url_is_actually_present(self, seeded, name):
        """Guards against parity passing because both sides are equally
        broken -- the real Razorpay URL must appear in the emitted text."""
        _code, url = OXFORD[name]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert url in text


class TestOxfordPriceParity:
    """The audit's blocking finding, pinned. Sourcing price from
    TenantKnowledge would have changed what Oxford charges by 22-55%."""

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_price_comes_from_the_catalogue(self, seeded, name):
        price = COURSE_PAYMENT_LINKS[name][2]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert f"Fee: *{price}*" in text

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_display_fields_all_come_from_the_catalogue(self, seeded, name):
        code, full, price, dur, _ = COURSE_PAYMENT_LINKS[name]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert code in text and full in text and price in text and dur in text

    def test_a_different_tenant_price_never_leaks_into_oxfords_message(self, seeded):
        """Even with a TenantKnowledge row carrying a different price, the
        emitted price is the catalogue's."""
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(tenant_id=OX,
                                                  title="PGDCA").first()
            attrs = json.loads(row.attributes)
            attrs["commercial"]["base_price"] = 19540
            row.attributes = json.dumps(attrs)
            db.session.commit()
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert "19540" not in text
        assert "₹15,999" in text


# ── state transitions ───────────────────────────────────────────────────────

class TestStateTransitionsUnchanged:

    def test_enroll_sets_payment_pending_and_offer_course(self, seeded):
        st = state("PGDCA")
        with _APP.app_context():
            cta.enroll_reply("Alice", "PGDCA", st, OX)
        assert st["stage"] == "payment_pending" and st["offer_course"] == "PGDCA"

    def test_pay_intent_sets_payment_pending(self, seeded):
        st = state("DCA Fast Track")
        with _APP.app_context():
            oh.handle_pay_intent(st, OX)
        assert st["stage"] == "payment_pending" and st["offer_course"] == "DCA"

    def test_handle_offer_sets_payment_pending(self, seeded):
        st = state()
        with _APP.app_context():
            oh.handle_offer("AIDM", st, OX)
        assert st["stage"] == "payment_pending" and st["offer_course"] == "AIDM"

    def test_no_payment_pending_without_a_link(self, seeded):
        """THE state-safety property: a conversation must never wait for a
        transaction id against a link that was never sent."""
        st = state("PGDCA")
        with _APP.app_context():
            cta.enroll_reply("Alice", "PGDCA", st, B)
        assert st["stage"] != "payment_pending"
        assert st["offer_course"] == ""

    def test_handle_offer_writes_no_state_without_a_link(self, seeded):
        st = state()
        with _APP.app_context():
            assert oh.handle_offer("PGDCA", st, B) is None
        assert st["stage"] != "payment_pending" and st["offer_course"] == ""

    def test_pay_intent_falls_through_to_the_offer_menu(self, seeded):
        """offer_menu_reply() interpolates a RANDOM urgency line, so two calls
        are not comparable; assert the stable parts instead."""
        st = state("PGDCA")
        with _APP.app_context():
            text, preset = oh.handle_pay_intent(st, B)
        assert st["stage"] == "offer_menu"
        assert preset == "OFFER"
        assert "*Special Offer" in text
        assert "Seat reserve cheyyan course number reply cheyyoo." in text
        assert "rzp.io" not in text


# ── isolation ───────────────────────────────────────────────────────────────

class TestForeignTenantCannotReceiveOxfordUrls:

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_enroll_reply_gives_tenant_b_no_oxford_url(self, seeded, name):
        _code, url = OXFORD[name]
        with _APP.app_context():
            text, _ = cta.enroll_reply("Bob", name, state(name), B)
        assert url not in text and "rzp.io" not in text

    @pytest.mark.parametrize("name", sorted(COURSE_PAYMENT_LINKS))
    def test_pay_intent_gives_tenant_b_no_oxford_url(self, seeded, name):
        _code, url = OXFORD[name]
        with _APP.app_context():
            text, _ = oh.handle_pay_intent(state(name), B)
        assert url not in text and "rzp.io" not in text

    @pytest.mark.parametrize("digit", sorted(OFFER_MENU))
    def test_offer_paths_give_tenant_b_nothing(self, seeded, digit):
        code = OFFER_MENU[digit][0]
        with _APP.app_context():
            assert oh.handle_offer(code, state(), B) is None
            assert oh.handle_offer_number(digit, state(), B) is None

    def test_tenant_b_with_its_own_url_gets_only_that(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", URL_B, title="B PGDCA"))
            db.session.commit()
            text, _ = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert URL_B in text
        assert OXFORD["PGDCA"][1] not in text

    def test_oxford_is_unaffected_by_tenant_b_rows(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", URL_B, title="B PGDCA"))
            db.session.commit()
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert OXFORD["PGDCA"][1] in text and URL_B not in text


class TestTenantIdIsRequired:

    @pytest.mark.parametrize("bad", [None, "", 0, False])
    def test_no_tenant_means_no_link(self, seeded, bad):
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), bad)
        assert "rzp.io" not in text

    def test_default_argument_does_not_leak_a_link(self, seeded):
        """Signatures default tenant_id to None for call-site compatibility;
        that default must never produce a payment link."""
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"))
            assert "rzp.io" not in text
            assert oh.handle_offer("PGDCA", state()) is None
            t2, _ = oh.handle_pay_intent(state("PGDCA"))
        assert "rzp.io" not in t2


# ── fail-closed ─────────────────────────────────────────────────────────────

class TestFailsClosedToTheCounselorBranch:

    def _counselor(self, name):
        return f"{name}-nte payment link prepare aavunnu"

    def test_missing_row_reaches_the_counselor_branch(self, seeded):
        with _APP.app_context():
            text, preset = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert self._counselor("PGDCA") in text and preset == "COURSE"

    def test_inactive_row_reaches_the_counselor_branch(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", URL_B, active=False))
            db.session.commit()
            text, _ = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert self._counselor("PGDCA") in text

    def test_blank_url_reaches_the_counselor_branch(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", "   "))
            db.session.commit()
            text, _ = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert self._counselor("PGDCA") in text

    def test_ambiguous_rows_reach_the_counselor_branch(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", URL_B, title="one"))
            db.session.add(krow(B, "PGDCA", "https://pay.example.com/other",
                                title="two"))
            db.session.commit()
            text, _ = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert self._counselor("PGDCA") in text
        assert URL_B not in text

    def test_db_error_reaches_the_counselor_branch(self, seeded, monkeypatch):
        class Boom:
            def filter(self, *a, **k):
                raise RuntimeError("db down")
        with _APP.app_context():
            monkeypatch.setattr(TenantKnowledge, "query", Boom())
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert self._counselor("PGDCA") in text
        assert "rzp.io" not in text

    def test_link_less_oxford_course_is_unchanged(self, seeded):
        """One of the six catalogue courses that never had a link."""
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "Python Programming",
                                            state("Python Programming"), OX)
        assert self._counselor("Python Programming") in text
        assert preset == "COURSE"

    def test_no_course_selected_is_unchanged(self, seeded):
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "", state(), OX)
        assert preset == "GOAL" and "course select cheyyoo" in text

    def test_unknown_offer_code_still_returns_none(self, seeded):
        with _APP.app_context():
            assert oh.handle_offer("NOSUCH", state(), OX) is None
            assert oh.handle_offer_number("9", state(), OX) is None


# ── no constant URL fallback anywhere ───────────────────────────────────────

class TestNoConstantUrlFallback:

    @pytest.mark.parametrize("fn_name,mod", [
        ("enroll_reply", "app/bot/cta_handlers.py"),
        ("handle_pay_intent", "app/bot/offer_handlers.py"),
        ("handle_offer", "app/bot/offer_handlers.py"),
        ("handle_offer_number", "app/bot/offer_handlers.py"),
    ])
    def test_no_path_reads_index_four(self, fn_name, mod):
        tree = ast.parse(_src(mod))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == fn_name)
        for n in ast.walk(fn):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                    and n.slice.value == 4:
                raise AssertionError(f"{fn_name} reads the catalogue URL column")

    @pytest.mark.parametrize("mod", ["app/bot/cta_handlers.py",
                                     "app/bot/offer_handlers.py"])
    def test_no_hardcoded_payment_url_literal(self, mod):
        tree = ast.parse(_src(mod))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and ("rzp.io" in n.value or "razorpay" in n.value.lower()):
                raise AssertionError(f"{mod} hardcodes a payment URL")

    def test_link_variable_originates_only_from_the_resolver(self):
        """In each emitting function `link` must be assigned from the resolver
        call and from nothing else."""
        for mod, fns in [("app/bot/cta_handlers.py", ["enroll_reply"]),
                         ("app/bot/offer_handlers.py",
                          ["handle_pay_intent", "handle_offer"])]:
            tree = ast.parse(_src(mod))
            for fname in fns:
                fn = next(n for n in ast.walk(tree)
                          if isinstance(n, ast.FunctionDef) and n.name == fname)
                sources = []
                for n in ast.walk(fn):
                    if isinstance(n, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "link"
                            for t in n.targets):
                        sources.append(n.value)
                assert sources, f"{fname}: no `link` assignment"
                for v in sources:
                    assert isinstance(v, ast.Call) and \
                        getattr(v.func, "id", None) == "resolve_payment_url", \
                        f"{fname}: `link` assigned from something else"

    def test_catalogue_constants_are_untouched(self):
        assert len(COURSE_PAYMENT_LINKS) == 4 and len(OFFER_MENU) == 4
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"
        assert OFFER_MENU["4"][4] == "https://rzp.io/rzp/KAQ2C7t"
        for entry in COURSE_PAYMENT_LINKS.values():
            assert len(entry) == 5, "catalogue tuple shape changed"


class TestRouterThreadsTenantId:

    @pytest.mark.parametrize("callee", ["handle_offer", "handle_pay_intent",
                                        "handle_offer_number"])
    def test_router_passes_tenant_id(self, callee):
        tree = ast.parse(_src("app/bot/router.py"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == callee]
        assert calls, f"{callee} is not called from the router"
        for c in calls:
            names = [getattr(a, "id", None) for a in c.args]
            assert "tenant_id" in names, f"{callee} called without tenant_id"

    def test_handle_cta_passes_tenant_id_to_enroll_reply(self):
        tree = ast.parse(_src("app/bot/cta_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "handle_cta")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "enroll_reply"]
        assert calls, "handle_cta no longer calls enroll_reply"
        assert "tenant_id" in [getattr(a, "id", None) for a in calls[0].args]


class TestOutOfScopeUnchanged:
    """b-2 touches three bot modules. Nothing else."""

    def test_payment_link_reply_signature_unchanged(self):
        tree = ast.parse(_src("app/bot/cta_handlers.py"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "payment_link_reply")
        assert [a.arg for a in fn.args.args] == \
            ["code", "full_name", "price", "dur", "link"]

    def test_resolver_module_not_modified_by_this_phase(self):
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/payment_link_service.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", "payment_link_service.py changed in b-2"

    def test_constants_not_modified_by_this_phase(self):
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "app/bot/constants.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", "constants.py changed in b-2"
