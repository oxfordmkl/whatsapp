"""Phase RC2.5.5b-2, tripwires INVERTED by RC2.5.5c-3a.

WHAT b-2 PINNED, AND WHY IT NO LONGER HOLDS
--------------------------------------------
b-2 flipped ONE thing and pinned everything else:

    URL                        -> resolve_payment_url(tenant_id, code)
    code/full_name/price/dur   -> COURSE_PAYMENT_LINKS / OFFER_MENU, unchanged

That was right for b-2. Sourcing the price from TenantKnowledge *at that time*
would have silently re-priced Oxford by 22-55%, because the only figures then
in the rows were the RC2.5.3a-K "FORMULA A" Rutronix REGULATORY amounts
(registration + net tuition to ATC) -- never a customer-facing selling price.
What a business charges is a commercial decision, not something a refactor
gets to change on the way past.

RC2.5.5c-2 then AUTHORED the real tenant catalogue: customer-facing titles,
durations and `commercial.normal_total_fee` per course. RC2.5.5c-3 made the
deterministic runtime read it. So the premise behind b-2's parity pins is gone
-- the constants are no longer the catalogue, and the prices they carry are
obsolete:

    code    obsolete constant   current tenant catalogue
    PGDCA   Rs.15,999           Rs.19,540
    AIDM    Rs.19,999           Rs.30,900
    DCA     Rs. 6,400           Rs. 8,350
    CWPDE   Rs. 4,800           Rs. 6,250

Those figures are no longer "the regulatory total wearing a price tag": c-2
authored them AS the customer-facing normal price. The 26 failures this file
produced against c-3 were therefore obsolete EXPECTATIONS, not application
regressions.

WHAT THIS FILE PINS NOW
------------------------
Every assertion below was converted, not deleted. Same paths, same shapes,
same count of guarantees -- the expected SOURCE and VALUES moved to the c-3
contract:

    identity/title/price/dur   -> catalogue_service -> CourseRecord
    URL                        -> resolve_payment_url(tenant_id, code)   (as before)

The two boundaries stay independent, and the URL boundary is byte-for-byte the
one b-2 built: the four payable courses must still emit exactly the URLs in
COURSE_PAYMENT_LINKS, which this file still reads -- as a fixture INDEX, never
as display copy.

FAIL-CLOSED EVERYWHERE (unchanged from b-2)
--------------------------------------------
No resolver result means NO LINK: no tenant, no row, inactive, blank URL,
ambiguous match, DB error, foreign tenant. Each path falls through to the
branch it already had -- the counselor handoff, or the offer menu -- and
critically does NOT write st["stage"] = "payment_pending", so no conversation
waits for a transaction id against a link that was never sent.

TENANT #2 (b-2 said "safe but not yet functional" -- c-3 finished it)
----------------------------------------------------------------------
st["course"] no longer comes from Oxford's hardcoded ALL_COURSES; the router
writes the tenant's own catalogue title. A second tenant's customer now
browses their OWN courses. Isolation is unchanged and still pinned here.

Import isolation follows test_platform_security_14c.py.
"""
import ast
import json
import os
import re
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
from app.services import catalogue_service as cat                       # noqa: E402
from app.bot.constants import COURSE_PAYMENT_LINKS, OFFER_MENU          # noqa: E402

OX = "t-ox"
B = "t-b"
URL_B = "https://pay.example.com/b/pgdca"

LF = chr(10)

# RC2.5.4c-x-6b1: the only constructs that phase is authorised to change in
# app/bot/constants.py. Both are marketing-copy pools that carried an
# UNCONDITIONAL EMI claim, contradicting the per-course
# commercial.emi_available flag inside a single rendered message.
_CONSTANTS_AUTHORISED = {"TRUST_LINES", "FEES_VALUE_LINES"}

# ── WIDENED BY RC2.5.4c-x-6d1 ──────────────────────────────────────────────
# The ten PLATFORM DEFAULT course cards. x-6d1 strips their unsupported
# accreditation/certification/government-approval claims and the EMI claim
# that contradicted the default record's own emi_available=False. They are
# listed EXPLICITLY -- no wildcard, no "any constant starting with _" -- so a
# new constant can never inherit the allowance.
_X6D1_CARDS = frozenset({
    "_PGDCA", "_AIDM", "_SAP", "_PYTHON", "_GST",
    "_DCA", "_TEACHER", "_ACCOUNTING", "_WORD", "_WEB",
})

# Claim text x-6d1 removed. A card may never carry any of it again.
_X6D1_BANNED = (
    "EMI Available", "EMI / installment", "Rutronix", "Government Approved",
    "Government Recognised", "recognised for Govt",
    "Industry-Recognised Certificate", "SAP Alliance", "Dual Certification",
    "Payroll",
)


def _x6d1_words(text):
    """Word tokens of a construct's SOURCE segment."""
    import re
    return set(re.findall(r"\w+", text))


def _assert_x6d1_card_change_is_removal_only(rel, name, old_seg, new_seg):
    """A permitted card edit may only DELETE claim content.

    Three independent clauses, so "allowed to change" never degrades into
    "allowed to become anything":

      1. no banned claim survives;
      2. the construct did not gain lines;
      3. NO NEW WORD appears. This is the strong one -- a changed price, a
         changed duration, a reworded title, a rewritten syllabus or an
         invented claim all introduce a token absent from HEAD, and are
         rejected even though the construct itself is allow-listed.
    """
    low = new_seg.lower()
    for claim in _X6D1_BANNED:
        assert claim.lower() not in low, (
            rel + "::" + name + " still carries the claim " + repr(claim))

    assert len(new_seg.splitlines()) <= len(old_seg.splitlines()), (
        rel + "::" + name + " gained lines; x-6d1 authorises removal only")

    added = _x6d1_words(new_seg) - _x6d1_words(old_seg)
    assert not added, (
        rel + "::" + name + " introduced new content " + str(sorted(added))
        + "; x-6d1 authorises claim REMOVAL only, never a price, duration, "
          "title, syllabus or claim rewrite")

    # 4. ANTI-VACUITY. Clauses 1-3 all permit deletion, so on their own they
    # would accept a card gutted to an empty string. The descriptive skeleton
    # must survive: x-6d1 removes CLAIMS, not the course description.
    for marker in ("📚", "Best for:", "Syllabus:", "Duration:",
                   "Course Fee"):
        assert marker in new_seg, (
            rel + "::" + name + " lost descriptive marker " + repr(marker)
            + "; x-6d1 removes claims, not description")



def _assert_constants_only_emi_lines_changed(root):
    """app/bot/constants.py may differ from HEAD ONLY in the two marketing
    pools, and only by LOSING EMI-affirming entries.

    Stronger than the working-tree pin it replaces: it also catches a
    construct being added or removed, it proves every payment/price constant
    is byte-identical, and it proves the permitted change was a removal of an
    EMI claim rather than an arbitrary edit to those pools.
    """
    import subprocess

    rel = "app/bot/constants.py"
    # NOT text=True: on Windows that decodes git's stdout with the locale
    # codepage and mangles this file's Malayalam and emoji, so the guard would
    # fire on an encoding artifact rather than a real change. splitlines()
    # normalises the line endings without needing a literal newline escape.
    head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root,
                          capture_output=True)
    assert head.returncode == 0, f"cannot read HEAD:{rel}"
    old_src = LF.join(head.stdout.decode("utf-8").splitlines())
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as fh:
        new_src = LF.join(fh.read().splitlines())

    def segments(src):
        named, other = {}, []
        for node in ast.parse(src).body:
            seg = ast.get_source_segment(src, node) or ""
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                named[node.name] = seg
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)):
                named[node.targets[0].id] = seg
            else:
                other.append(seg)
        return named, LF.join(other)

    old_named, old_other = segments(old_src)
    new_named, new_other = segments(new_src)

    assert old_other == new_other, f"module-level code in {rel} changed"
    assert set(old_named) == set(new_named), (
        f"top-level constructs added or removed in {rel}: "
        f"{set(old_named) ^ set(new_named)}")
    for name, old_seg in old_named.items():
        # x-6b1 pools and the x-6d1 cards are the ONLY
        # constructs permitted to differ; each is then
        # direction-checked separately below.
        if name in _CONSTANTS_AUTHORISED or name in _X6D1_CARDS:
            continue
        assert new_named[name] == old_seg, (
            f"{rel}::{name} changed, but only "
            f"{sorted(_CONSTANTS_AUTHORISED)} is authorised")

    # The permitted change must be a REMOVAL of EMI-affirming entries.
    for name in sorted(_CONSTANTS_AUTHORISED):
        new_l = [x for x in new_named[name].splitlines() if "emi" in x.lower()]
        assert not new_l, f"{rel}::{name} still carries an EMI line: {new_l}"
        assert len(new_named[name].splitlines()) <= len(
            old_named[name].splitlines()), (
            f"{rel}::{name} gained lines; only removal is authorised")

    # x-6d1: the ten cards get their own, stricter direction check.
    for name in sorted(_X6D1_CARDS):
        _assert_x6d1_card_change_is_removal_only(
            rel, name, old_named[name], new_named[name])


_APP = create_app()
_APP.config["TESTING"] = True

# ── the constants, used ONLY as fixture indexes ─────────────────────────────
#
# Reading them here is not a contract violation, it is the proof: this file
# derives the EXPECTED URLS from the same constant b-2 used, so "the four
# payable courses still emit the same links" is asserted against the original
# source of truth rather than against a number retyped by hand. Their title,
# price and duration columns are read only to assert they are ABSENT from
# customer-facing output.

# stored course NAME a pre-c-3 conversation still holds -> stable code.
LEGACY_NAME = {name: e[0] for name, e in COURSE_PAYMENT_LINKS.items()}
# code -> live payment URL. Unchanged by c-3; that is the point.
URL = {e[0]: e[4] for e in COURSE_PAYMENT_LINKS.values()}
# code -> the obsolete display copy c-3 retired.
OBSOLETE_TITLE = {e[0]: e[1] for e in COURSE_PAYMENT_LINKS.values()}
OBSOLETE_PRICE = {e[0]: e[2] for e in COURSE_PAYMENT_LINKS.values()}

# ── the RC2.5.5c-2 tenant catalogue this suite now seeds ────────────────────
# code -> (title, duration, normal_total_fee, rendered price)
CATALOGUE = {
    "PGDCA": ("PGDCA – Computer Applications", "12 Months", 19540, "₹19,540"),
    "AIDM": ("AIDM – Digital Marketing", "6 Months", 30900, "₹30,900"),
    "DCA": ("DCA Fast Track – Computer Applications", "6 Months", 8350, "₹8,350"),
    "CWPDE": ("CWPDE – Word Processing & Data Entry", "6 Months", 6250, "₹6,250"),
}
CODES = sorted(CATALOGUE)


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def krow(tenant_id, code, url, *, title=None, active=True, kind="course"):
    """A MINIMAL row: a code and a payment URL, nothing else.

    Kept exactly as b-2 wrote it. The fail-closed and isolation cases below
    are about URL resolution, and a row with no catalogue content is the
    sharpest way to test that -- it cannot accidentally satisfy a display
    assertion.
    """
    commercial = {"code": code}
    if url is not None:
        commercial["payment_url"] = url
    return TenantKnowledge(tenant_id=tenant_id, kind=kind,
                           title=title or code, body="b",
                           attributes=json.dumps({"commercial": commercial}),
                           is_active=active, sort_order=0)


def catrow(tenant_id, code, url, *, fee=None, sort=0):
    """A full RC2.5.5c-2-shaped catalogue row: the customer-facing title,
    duration and normal_total_fee alongside the payment URL."""
    title, duration, normal_fee, _money = CATALOGUE[code]
    return TenantKnowledge(
        tenant_id=tenant_id, kind="course", title=title,
        body=f"About {title}.",
        attributes=json.dumps({
            "duration": duration,
            "commercial": {"code": code, "currency": "INR",
                           "normal_total_fee": fee if fee is not None else normal_fee,
                           "emi_available": True, "offers": [],
                           "payment_url": url},
        }),
        is_active=True, sort_order=sort)


@pytest.fixture()
def seeded():
    """Oxford's four monetised courses, as RC2.5.5c-2 authored them: the
    catalogue content AND the same payment URLs b-2 pinned."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug="ox",
                              status="ACTIVE", billing_exempt=True))
        db.session.add(Tenant(id=B, name="Beta", slug="beta",
                              status="ACTIVE", billing_exempt=True))
        for i, code in enumerate(CODES, start=1):
            db.session.add(catrow(OX, code, URL[code], sort=i))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def state(course=""):
    return {"course": course, "stage": "start", "offer_course": ""}


def expected(code):
    """The payment CTA text the c-3 contract requires, assembled from the
    tenant catalogue plus the independently-resolved URL."""
    title, duration, _fee, money = CATALOGUE[code]
    return cta.payment_link_reply(code, title, money, duration, URL[code])


def legacy_of(code):
    """The pre-c-3 display name still sitting in persisted ConversationState."""
    return next(n for n, c in LEGACY_NAME.items() if c == code)


# ── Oxford output parity: the central proof, re-pointed at the catalogue ────

class TestOxfordPaymentOutputIsCatalogueSourced:
    """WAS TestOxfordByteIdenticalOutput.

    b-2 asserted each path reproduced the CONSTANT's text byte-for-byte. The
    assertion is still byte-for-byte -- the strongest form, deliberately kept
    -- but `expected()` is now assembled from the tenant catalogue. Anything
    that reads a display value from the constants again fails here.
    """

    @pytest.mark.parametrize("code", CODES)
    def test_enroll_reply_output_is_catalogue_sourced(self, seeded, code):
        """A conversation persisted BEFORE c-3 still holds the old name."""
        name = legacy_of(code)
        with _APP.app_context():
            got = cta.enroll_reply("Alice", name, state(name), OX)
        assert got == expected(code)

    @pytest.mark.parametrize("code", CODES)
    def test_enroll_reply_output_from_the_current_catalogue_title(self, seeded, code):
        """D1: the migrated router writes the CATALOGUE title into state. That
        title matches no COURSE_PAYMENT_LINKS key, which is why the pre-fix
        code issued no link at all on this path."""
        title = CATALOGUE[code][0]
        with _APP.app_context():
            got = cta.enroll_reply("Alice", title, state(title), OX)
        assert got == expected(code)

    @pytest.mark.parametrize("code", CODES)
    def test_handle_pay_intent_output_is_catalogue_sourced(self, seeded, code):
        name = legacy_of(code)
        with _APP.app_context():
            got = oh.handle_pay_intent(state(name), OX)
        assert got == expected(code)

    @pytest.mark.parametrize("digit", sorted(OFFER_MENU))
    def test_handle_offer_output_is_catalogue_sourced(self, seeded, digit):
        code = OFFER_MENU[digit][0]
        with _APP.app_context():
            got = oh.handle_offer(code, state(), OX)
        assert got == expected(code)

    @pytest.mark.parametrize("digit", sorted(OFFER_MENU))
    def test_handle_offer_number_output_is_catalogue_sourced(self, seeded, digit):
        code = OFFER_MENU[digit][0]
        with _APP.app_context():
            got = oh.handle_offer_number(digit, state(), OX)
        assert got == expected(code)

    @pytest.mark.parametrize("code", CODES)
    def test_the_live_url_is_actually_present(self, seeded, code):
        """UNCHANGED FROM b-2. Guards against parity passing because both
        sides are equally broken -- the real Razorpay URL must appear."""
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert URL[code] in text

    @pytest.mark.parametrize("code", CODES)
    def test_the_url_is_the_same_one_b2_pinned(self, seeded, code):
        """c-3 moved the display copy and NOTHING about payment routing: each
        payable course must still reach exactly the URL it always did."""
        assert URL[code] == COURSE_PAYMENT_LINKS[legacy_of(code)][4]
        with _APP.app_context():
            text, _ = oh.handle_offer(code, state(), OX)
        assert URL[code] in text


class TestOxfordPriceIsTheTenantCatalogues:
    """WAS TestOxfordPriceParity.

    b-2's blocking finding was "do not let TenantKnowledge re-price Oxford".
    RC2.5.5c-2 authored deliberate customer-facing prices into those rows and
    c-3 made them the source, so the guarantee INVERTS: the emitted price must
    be the tenant catalogue's, and the constant's must not appear.
    """

    @pytest.mark.parametrize("code", CODES)
    def test_price_comes_from_the_tenant_catalogue(self, seeded, code):
        money = CATALOGUE[code][3]
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert f"Fee: *{money}*" in text
        assert OBSOLETE_PRICE[code] not in text

    @pytest.mark.parametrize("code", CODES)
    def test_display_fields_all_come_from_the_tenant_catalogue(self, seeded, code):
        title, duration, _fee, money = CATALOGUE[code]
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", name, state(name), OX)
        assert code in text and title in text and money in text and duration in text
        assert OBSOLETE_TITLE[code] not in text
        assert OBSOLETE_PRICE[code] not in text

    @pytest.mark.parametrize("code", CODES)
    def test_the_rendered_price_is_the_catalogues_own_formatting(self, seeded, code):
        """Pins the literal customer-visible string against the formatter, so
        a change to either side has to be deliberate."""
        with _APP.app_context():
            record = cat.get_course(OX, code)
            rendered = cat.format_money(record.normal_total_fee)
        assert rendered == CATALOGUE[code][3]
        assert record.normal_total_fee == CATALOGUE[code][2]

    def test_the_tenant_row_is_the_price_source(self, seeded):
        """INVERTED FROM test_a_different_tenant_price_never_leaks_into_oxfords_message.

        b-2 pinned the opposite: a TenantKnowledge price had to be IGNORED.
        c-3 makes the row authoritative, so editing it must move the quoted
        price. Written as an edit rather than a constant so it fails if the
        CTA ever goes back to reading a hardcoded table.
        """
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title=CATALOGUE["PGDCA"][0]).first()
            attrs = json.loads(row.attributes)
            attrs["commercial"]["normal_total_fee"] = 21000
            row.attributes = json.dumps(attrs)
            db.session.commit()
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert "₹21,000" in text
        assert CATALOGUE["PGDCA"][3] not in text
        assert OBSOLETE_PRICE["PGDCA"] not in text

    def test_another_tenants_price_never_leaks_into_oxfords_message(self, seeded):
        """The isolation half of the original test, kept: a foreign row for
        the same code must not change what Oxford quotes."""
        with _APP.app_context():
            db.session.add(catrow(B, "PGDCA", URL_B, fee=999))
            db.session.commit()
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert CATALOGUE["PGDCA"][3] in text
        assert "₹999" not in text and URL_B not in text

    @pytest.mark.parametrize("code", CODES)
    def test_no_obsolete_price_reaches_any_payment_path(self, seeded, code):
        """One assertion across all four emission paths, so a regression in
        any single one of them is caught here too."""
        name = legacy_of(code)
        with _APP.app_context():
            texts = [cta.enroll_reply("Alice", name, state(name), OX)[0],
                     oh.handle_pay_intent(state(name), OX)[0],
                     oh.handle_offer(code, state(), OX)[0],
                     oh.offer_menu_reply(OX)[0]]
        for text in texts:
            assert OBSOLETE_PRICE[code] not in text
            assert CATALOGUE[code][3] in text


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

    @pytest.mark.parametrize("code", CODES)
    def test_offer_course_is_the_code_never_the_display_name(self, seeded, code):
        """The state key downstream CRM writes read. It must stay the stable
        code even though the title the customer sees has changed."""
        title = CATALOGUE[code][0]
        st = state(title)
        with _APP.app_context():
            cta.enroll_reply("Alice", title, st, OX)
        assert st["offer_course"] == code

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
        """INVERTED. b-2 asserted the fall-through menu ended with "reply with
        a course number" -- which it could always do, because every tenant was
        answered with Oxford's four courses at Oxford's prices. That IS the
        defect c-3 removed: tenant B has no course in OFFER_MENU, so the menu
        now says so instead of asking for a number under an empty list.

        offer_menu_reply() interpolates a RANDOM urgency line, so two calls
        are not comparable; assert the stable parts instead.
        """
        st = state("PGDCA")
        with _APP.app_context():
            text, preset = oh.handle_pay_intent(st, B)
        assert st["stage"] == "offer_menu"
        assert preset == "OFFER"
        assert "*Special Offer" in text
        assert "rzp.io" not in text
        # No Oxford course, price or obsolete price reaches another tenant.
        for code in CODES:
            assert CATALOGUE[code][0] not in text
            assert CATALOGUE[code][3] not in text
            assert OBSOLETE_PRICE[code] not in text
        # Not a dead end: it points at the real catalogue instead.
        assert "COURSES" in text
        assert "course number reply cheyyoo" not in text

    def test_oxford_offer_menu_still_asks_for_a_number(self, seeded):
        """The other half of the same branch: a tenant that DOES have the
        offered courses keeps the original affordance and copy."""
        with _APP.app_context():
            text, preset = oh.offer_menu_reply(OX)
        assert preset == "OFFER"
        assert "Seat reserve cheyyan course number reply cheyyoo." in text
        for code in CODES:
            assert CATALOGUE[code][0] in text and CATALOGUE[code][3] in text
            assert OBSOLETE_PRICE[code] not in text


# ── isolation ───────────────────────────────────────────────────────────────

class TestForeignTenantCannotReceiveOxfordUrls:

    @pytest.mark.parametrize("code", CODES)
    def test_enroll_reply_gives_tenant_b_no_oxford_url(self, seeded, code):
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Bob", name, state(name), B)
        assert URL[code] not in text and "rzp.io" not in text

    @pytest.mark.parametrize("code", CODES)
    def test_enroll_reply_gives_tenant_b_no_oxford_price(self, seeded, code):
        """c-3 addition: the price is now tenant data too, so it needs the
        same isolation guarantee the URL has always had."""
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = cta.enroll_reply("Bob", name, state(name), B)
        assert CATALOGUE[code][3] not in text

    @pytest.mark.parametrize("code", CODES)
    def test_pay_intent_gives_tenant_b_no_oxford_url(self, seeded, code):
        name = legacy_of(code)
        with _APP.app_context():
            text, _ = oh.handle_pay_intent(state(name), B)
        assert URL[code] not in text and "rzp.io" not in text

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
        assert URL["PGDCA"] not in text

    def test_tenant_b_with_its_own_catalogue_gets_only_that(self, seeded):
        """c-3 addition: B's own title and price, never Oxford's."""
        with _APP.app_context():
            db.session.add(catrow(B, "PGDCA", URL_B, fee=1234))
            db.session.commit()
            text, _ = cta.enroll_reply("Bob", "PGDCA", state("PGDCA"), B)
        assert URL_B in text and "₹1,234" in text
        assert URL["PGDCA"] not in text and CATALOGUE["PGDCA"][3] not in text

    def test_oxford_is_unaffected_by_tenant_b_rows(self, seeded):
        with _APP.app_context():
            db.session.add(krow(B, "PGDCA", URL_B, title="B PGDCA"))
            db.session.commit()
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), OX)
        assert URL["PGDCA"] in text and URL_B not in text
        assert CATALOGUE["PGDCA"][3] in text


class TestTenantIdIsRequired:

    @pytest.mark.parametrize("bad", [None, "", 0, False])
    def test_no_tenant_means_no_link(self, seeded, bad):
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", "PGDCA", state("PGDCA"), bad)
        assert "rzp.io" not in text

    @pytest.mark.parametrize("bad", [None, "", 0, False])
    def test_no_tenant_means_no_payment_cta_at_all(self, seeded, bad):
        """c-3 addition, and the reason the previous test is not enough.

        Catalogue content fails SAFE: a falsy tenant degrades to the platform
        default catalogue, which is built from app.bot.constants and therefore
        still carries the obsolete prices. That is allowed -- it is a browse
        fallback -- but it must never become a payment CTA. No link, no
        payment_pending, no "Seat Reserve" screen.
        """
        st = state("PGDCA")
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "PGDCA", st, bad)
        assert "Seat Reserve Cheyyam" not in text
        assert st["stage"] != "payment_pending" and st["offer_course"] == ""
        assert preset == "COURSE"

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

    def test_link_less_course_reaches_the_counselor_branch(self, seeded):
        """A catalogue course the tenant never authored a payment URL for.
        Catalogue resolution succeeds; payment resolution fails closed, and
        the CTA must not fall back to the constant's URL for that code."""
        st = state("PGDCA")
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title=CATALOGUE["PGDCA"][0]).one()
            attrs = json.loads(row.attributes)
            attrs["commercial"].pop("payment_url")
            row.attributes = json.dumps(attrs)
            db.session.commit()
            assert cat.get_course(OX, "PGDCA") is not None   # still catalogued
            text, preset = cta.enroll_reply("Alice", "PGDCA", st, OX)
        assert "rzp.io" not in text and preset == "COURSE"
        assert st["stage"] != "payment_pending"

    def test_unknown_course_name_reaches_the_counselor_branch(self, seeded):
        """WAS test_link_less_oxford_course_is_unchanged. A name that resolves
        to no course at all must never be guessed into one."""
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "Python Programming",
                                            state("Python Programming"), OX)
        assert self._counselor("Python Programming") in text
        assert preset == "COURSE" and "rzp.io" not in text

    def test_no_course_selected_is_unchanged(self, seeded):
        with _APP.app_context():
            text, preset = cta.enroll_reply("Alice", "", state(), OX)
        assert preset == "GOAL" and "course select cheyyoo" in text

    def test_no_course_selected_lists_no_obsolete_price(self, seeded):
        """c-3 addition: that branch used to hardcode PGDCA at Rs.15,999 and
        DCA Fast Track at Rs.6,400 as example rows."""
        with _APP.app_context():
            text, _ = cta.enroll_reply("Alice", "", state(), OX)
        for code in CODES:
            assert OBSOLETE_PRICE[code] not in text

    def test_unknown_offer_code_still_returns_none(self, seeded):
        with _APP.app_context():
            assert oh.handle_offer("NOSUCH", state(), OX) is None
            assert oh.handle_offer_number("9", state(), OX) is None


# ── no constant fallback anywhere ───────────────────────────────────────────

_EMITTERS = [("enroll_reply", "app/bot/cta_handlers.py"),
             ("handle_pay_intent", "app/bot/offer_handlers.py"),
             ("handle_offer", "app/bot/offer_handlers.py"),
             ("handle_offer_number", "app/bot/offer_handlers.py")]


def _fn(mod, name):
    tree = ast.parse(_src(mod))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


class TestNoConstantFallback:

    @pytest.mark.parametrize("fn_name,mod", _EMITTERS)
    def test_no_path_reads_a_display_or_url_column(self, fn_name, mod):
        """WIDENED. b-2 forbade index [4] (the URL). c-3 also retires columns
        [1] title, [2] price and [3] duration, so only [0] -- the stable code,
        which is what makes OFFER_MENU still usable as the offer SET -- may be
        read from a catalogue tuple.
        """
        bad = [n.slice.value for n in ast.walk(_fn(mod, fn_name))
               if isinstance(n, ast.Subscript)
               and isinstance(n.slice, ast.Constant)
               and isinstance(n.slice.value, int)
               and n.slice.value != 0]
        assert bad == [], f"{fn_name} reads catalogue column(s) {bad}"

    @pytest.mark.parametrize("fn_name,mod", _EMITTERS)
    def test_no_path_names_a_retired_price_constant(self, fn_name, mod):
        loaded = {n.id for n in ast.walk(_fn(mod, fn_name))
                  if isinstance(n, ast.Name)}
        for gone in ("COURSE_PAYMENT_LINKS", "COURSE_FEES", "FULL_FEE_TABLE"):
            assert gone not in loaded, f"{fn_name} reads {gone}"

    @pytest.mark.parametrize("mod", ["app/bot/cta_handlers.py",
                                     "app/bot/offer_handlers.py"])
    def test_module_does_not_import_the_payment_link_catalogue(self, mod):
        """Structural: the constant is not even in scope, so no future edit
        inside these modules can reach for it without also changing this."""
        tree = ast.parse(_src(mod))
        imported = {a.asname or a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "COURSE_PAYMENT_LINKS" not in imported

    @pytest.mark.parametrize("mod", ["app/bot/cta_handlers.py",
                                     "app/bot/offer_handlers.py"])
    def test_no_hardcoded_payment_url_literal(self, mod):
        tree = ast.parse(_src(mod))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and ("rzp.io" in n.value or "razorpay" in n.value.lower()):
                raise AssertionError(f"{mod} hardcodes a payment URL")

    @pytest.mark.parametrize("fn_name,mod", _EMITTERS)
    def test_no_hardcoded_price_literal(self, fn_name, mod):
        """c-3 addition. The obsolete prices did not only live in the
        constants -- two of them were typed straight into enroll_reply."""
        bad = [n.value for n in ast.walk(_fn(mod, fn_name))
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and re.search(r"₹\s?\d", n.value)]
        assert bad == [], f"{fn_name} hardcodes price literal(s) {bad}"

    def test_link_variable_originates_only_from_the_resolver(self):
        """UNCHANGED FROM b-2. In each emitting function `link` must be
        assigned from the resolver call and from nothing else."""
        for mod, fns in [("app/bot/cta_handlers.py", ["enroll_reply"]),
                         ("app/bot/offer_handlers.py",
                          ["handle_pay_intent", "handle_offer"])]:
            for fname in fns:
                fn = _fn(mod, fname)
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

    @pytest.mark.parametrize("fn_name,mod", _EMITTERS[:3])
    def test_the_resolver_is_keyed_by_the_stable_code(self, fn_name, mod):
        """c-3 addition, and the D1 guard: keying payment by the display name
        broke silently the moment the title changed."""
        calls = [n for n in ast.walk(_fn(mod, fn_name)) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "resolve_payment_url"]
        assert len(calls) == 1, f"{fn_name}: expected one resolver call"
        arg = calls[0].args[1]
        assert (isinstance(arg, ast.Attribute) and arg.attr == "code") or \
               (isinstance(arg, ast.Name) and arg.id.endswith("code")), \
               f"{fn_name}: payment keyed by something other than the code"

    @pytest.mark.parametrize("fn_name,mod", [_EMITTERS[0], _EMITTERS[1]])
    def test_the_stored_course_name_goes_through_the_catalogue(self, fn_name, mod):
        """The two name-keyed paths must resolve st["course"] through
        resolve_legacy_name, or a pre-c-3 conversation loses its course."""
        called = {n.func.attr for n in ast.walk(_fn(mod, fn_name))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "resolve_legacy_name" in called, \
            f"{fn_name}: stored course name not resolved through the catalogue"

    def test_catalogue_constants_are_untouched(self):
        """UNCHANGED FROM b-2, and it matters more now: the constants are
        retired as a customer-facing source but must not be EDITED either --
        they remain the historical record and OFFER_MENU still defines the
        offer set and its 1-2-3-4 positions.
        """
        assert len(COURSE_PAYMENT_LINKS) == 4 and len(OFFER_MENU) == 4
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"
        assert OFFER_MENU["4"][4] == "https://rzp.io/rzp/KAQ2C7t"
        for entry in COURSE_PAYMENT_LINKS.values():
            assert len(entry) == 5, "catalogue tuple shape changed"


class TestRouterThreadsTenantId:

    @pytest.mark.parametrize("callee", ["handle_offer", "handle_pay_intent",
                                        "handle_offer_number",
                                        "offer_menu_reply"])
    def test_router_passes_tenant_id(self, callee):
        """WIDENED by c-3a: offer_menu_reply now takes a tenant too -- without
        it the menu falls back to the platform default for every tenant, which
        is the defect D2 described.
        """
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
    """b-2 touched three bot modules; c-3 touched the same three plus the
    catalogue read path. The payment BOUNDARY is out of scope for both."""

    def test_payment_link_reply_signature_unchanged(self):
        fn = _fn("app/bot/cta_handlers.py", "payment_link_reply")
        assert [a.arg for a in fn.args.args] == \
            ["code", "full_name", "price", "dur", "link"]

    def test_resolver_module_not_modified(self):
        """UNCHANGED FROM b-2. The payment boundary carries no authorisation
        to change in b-2, c-3 or c-3a."""
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/payment_link_service.py"],
            cwd=_ROOT, capture_output=True, text=True).stdout
        assert out.strip() == "", "payment_link_service.py changed"

    def test_resolver_semantics_are_untouched(self):
        """c-3a addition alongside the zero-diff pin above: that pin only says
        the working tree is clean, so it also passes for a file whose changes
        were committed. This pins the contract itself -- keyed by tenant and
        code, never returning a literal URL.
        """
        fn = _fn("app/services/payment_link_service.py", "resolve_payment_url")
        assert [a.arg for a in fn.args.args][:2] == ["tenant_id", "code"]
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, str):
                assert not n.value.value.startswith("http"), \
                    "resolver returns a hardcoded URL"

    def test_resolver_does_not_import_bot_modules(self):
        tree = ast.parse(_src("app/services/payment_link_service.py"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
            elif isinstance(n, ast.Import):
                mods.update(a.name for a in n.names)
        assert not any(m.startswith("app.bot") for m in mods)

    def test_constants_changed_only_where_rc254cx6b1_authorised(self):
        """NARROWED BY RC2.5.4c-x-6b1 (was test_constants_file_is_not_edited,
        a plain working-tree pin).

        That phase is authorised to remove two unconditional EMI marketing
        lines -- one from TRUST_LINES, one from FEES_VALUE_LINES -- because
        they contradicted the per-course commercial.emi_available flag inside
        a single message.

        The pin is narrowed, not dropped, and to something STRONGER: every
        other top-level construct must be byte-identical to HEAD. So
        COURSE_PAYMENT_LINKS, OFFER_MENU, FULL_FEE_TABLE, COURSE_FEES,
        ALL_COURSES and every other payment- or price-bearing constant is
        still pinned exactly -- which is what this test existed to protect.
        Once b1 is committed the diff is empty and every comparison is
        trivially equal.
        """
        _assert_constants_only_emi_lines_changed(_ROOT)
