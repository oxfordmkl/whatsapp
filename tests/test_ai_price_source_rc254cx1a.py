"""Phase RC2.5.4c-x-1a: one price reaches the AI, not two.

THE DEFECT
----------
Two fields hold one course's customer price:

    commercial.base_price        written by the RC2.5.4b admin UI
    commercial.normal_total_fee  written by RC2.5.5c-2's backfill

catalogue_service._record_from_row applies a precedence rule --
normal_total_fee first, base_price only when it is absent (RC2.5.4c-x) --
and every DETERMINISTIC customer path reads the resulting CourseRecord.

knowledge_service._flatten_attrs does not. It walks the raw attributes JSON
and yielded BOTH keys verbatim, so the AI path had no precedence rule at all.

Observed in production (RC2.5.4c-x-1 audit): PGDCA carried
normal_total_fee 19540 and base_price 16000 for roughly seventeen hours, and
the composed prompt contained both numbers for one course with nothing to say
which to quote. The deterministic flow said 19540 throughout.

THE FIX
-------
`base_price` joins `payment_url` and `legacy_payment_url` in
_NON_RENDERABLE_KEYS -- the SAME bare-key, whole-subtree exclusion, not a new
mechanism and not a string special-case. Rendering-time only: the field stays
in storage, stays readable by _attributes(), stays the admin UI's write
target, and stays the RC2.5.4c-x catalogue fallback.

OUT OF SCOPE: the admin write path, dual-write, removing base_price from
storage, the canonical-field decision, and the separate keyword defect
(FOLLOW-UP -- COURSE KEYWORD RESOLUTION / FST01).
"""
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx1a_ai_price.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc254cx1a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc254cx1a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc254cx1a-broadcast-key")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.services import knowledge_service as ks                        # noqa: E402
from app.services import catalogue_service as cat                       # noqa: E402

OX = "t-ox"
B = "t-b"

_APP = create_app()
_APP.config["TESTING"] = True

PAY = "https://rzp.io/rzp/KAQ2C7t"
LEGACY = "https://rzp.io/rzp/OLD-LINK"


def _row(tenant, title, commercial, *, order=0, body="About the course."):
    return TenantKnowledge(
        tenant_id=tenant, kind="course", title=title, body=body,
        attributes=json.dumps({"duration": "12 Months",
                               "categories": ["job"],
                               "commercial": commercial}),
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
        # the PGDCA shape that actually diverged in production
        db.session.add(_row(OX, "PGDCA – Computer Applications", {
            "code": "PGDCA", "currency": "INR",
            "normal_total_fee": 19540, "base_price": 16000,
            "payment_url": PAY, "legacy_payment_url": LEGACY,
            "offers": []}, order=1))
        # the FST01 shape: base_price only, no normal_total_fee
        db.session.add(_row(OX, "full stack web development", {
            "code": "FST01", "currency": "INR",
            "base_price": 37000, "offers": []}, order=2))
        db.session.add(_row(B, "Beta Yoga", {
            "code": "BETA1", "currency": "INR",
            "normal_total_fee": 1234, "base_price": 4321,
            "offers": []}, order=1))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def _block(tenant=OX, query="pgdca fee price"):
    with _APP.app_context():
        return ks.render_knowledge_block(tenant, query=query)


# ═══ 1, 2 — one price reaches the prompt, and it is the canonical one ═════

class TestPriceKeyExclusion:

    def test_base_price_is_excluded_from_the_rendered_block(self, seeded):
        blk = _block()
        assert "base_price" not in blk
        assert "16000" not in blk, "the non-canonical price reached the prompt"

    def test_normal_total_fee_is_still_rendered(self, seeded):
        blk = _block()
        assert "commercial.normal_total_fee: 19540" in blk

    def test_the_prompt_now_agrees_with_the_deterministic_price(self, seeded):
        """The whole point: one course, one price, and it is the one every
        deterministic path quotes."""
        blk = _block()
        with _APP.app_context():
            rec = cat.get_course(OX, "PGDCA")
        assert rec.normal_total_fee == 19540
        assert str(rec.normal_total_fee) in blk
        assert "16000" not in blk

    def test_a_course_with_only_base_price_contributes_no_price_line(self, seeded):
        """FST01 has no normal_total_fee. The AI gets NO price for it rather
        than one no deterministic path would have quoted -- and must not
        invent one. The deterministic paths are unaffected (see
        test_catalogue_fallback_is_untouched)."""
        blk = _block(query="full stack web development")
        assert "37000" not in blk
        assert "full stack web development" in blk, "the course itself must still render"


# ═══ 3, 6 — the existing bare-key mechanism, unchanged ════════════════════

class TestExclusionMechanism:

    def test_it_is_the_existing_bare_key_set(self, seeded):
        assert "base_price" in ks._NON_RENDERABLE_KEYS
        assert isinstance(ks._NON_RENDERABLE_KEYS, frozenset)

    def test_excluded_by_bare_key_at_any_nesting_depth(self, seeded):
        """Same semantics as legacy_payment_url: matched by bare key name,
        not dotted path, wherever it appears in the tree."""
        with _APP.app_context():
            db.session.add(_row(OX, "Nested Case", {
                "code": "NEST", "offers": [],
                "vendor": {"pricing": {"base_price": 98765}}}, order=9))
            db.session.commit()
        blk = _block(query="Nested Case")
        assert "98765" not in blk
        assert "base_price" not in blk

    def test_the_whole_subtree_under_the_key_is_skipped(self, seeded):
        """Not merely skipped as a scalar: nothing nested under an excluded
        key may leak out under a sub-key."""
        with _APP.app_context():
            db.session.add(_row(OX, "Subtree Case", {
                "code": "SUB", "offers": [],
                "base_price": {"amount": 55555, "note": "LEAKMARKER"}}, order=8))
            db.session.commit()
        blk = _block(query="Subtree Case")
        assert "55555" not in blk
        assert "LEAKMARKER" not in blk

    def test_a_similarly_named_key_is_not_swept_up(self, seeded):
        """Exclusion is exact-match, not substring -- it must not silently
        swallow other fields."""
        with _APP.app_context():
            db.session.add(_row(OX, "Similar Case", {
                "code": "SIM", "offers": [],
                "base_price_note": "KEEPME", "rebase_price": 4242}, order=7))
            db.session.commit()
        blk = _block(query="Similar Case")
        assert "KEEPME" in blk
        assert "4242" in blk


# ═══ 4, 5 — the two payment exclusions are unchanged ══════════════════════

class TestPaymentExclusionsUnregressed:

    def test_payment_url_remains_excluded(self, seeded):
        blk = _block()
        assert "payment_url" not in blk
        assert PAY not in blk
        assert "rzp.io" not in blk

    def test_legacy_payment_url_remains_excluded(self, seeded):
        blk = _block()
        assert "legacy_payment_url" not in blk
        assert LEGACY not in blk

    def test_all_three_keys_are_present_in_the_set(self, seeded):
        assert ks._NON_RENDERABLE_KEYS == frozenset(
            {"legacy_payment_url", "payment_url", "base_price"})


# ═══ 7 — rendering changes nothing on disk ════════════════════════════════

class TestStorageUntouched:

    def test_rendering_does_not_mutate_the_stored_row(self, seeded):
        _block()
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="PGDCA – Computer Applications").one()
            commercial = json.loads(row.attributes)["commercial"]
        assert commercial["base_price"] == 16000
        assert commercial["normal_total_fee"] == 19540
        assert commercial["payment_url"] == PAY
        assert commercial["legacy_payment_url"] == LEGACY

    def test_attributes_parser_still_returns_every_key(self, seeded):
        """The exclusion is rendering-time only: _attributes() is untouched."""
        with _APP.app_context():
            row = TenantKnowledge.query.filter_by(
                tenant_id=OX, title="PGDCA – Computer Applications").one()
            attrs = ks._attributes(row)
        assert attrs["commercial"]["base_price"] == 16000
        assert attrs["commercial"]["payment_url"] == PAY


# ═══ 8 — catalogue_service is entirely unaffected ═════════════════════════

class TestCatalogueUnaffected:

    def test_catalogue_still_reads_normal_total_fee(self, seeded):
        with _APP.app_context():
            assert cat.get_course(OX, "PGDCA").normal_total_fee == 19540

    def test_catalogue_fallback_is_untouched(self, seeded):
        """RC2.5.4c-x still applies: FST01 has no normal_total_fee and is
        still quoted its base_price by every deterministic path, even though
        that number no longer reaches the AI."""
        with _APP.app_context():
            rec = cat.get_course(OX, "FST01")
            idx, _ = cat.catalogue_index_with_provenance(OX)
        assert rec.normal_total_fee == 37000
        line = next(e for e in idx if e.startswith("FST01"))
        assert "₹37,000" in line

    def test_the_catalogue_index_is_not_routed_through_the_flattener(self, seeded):
        """The two producers are separate: the index comes from CourseRecord,
        the knowledge block from _flatten_attrs. Excluding a key from one must
        not remove it from the other."""
        with _APP.app_context():
            idx, _ = cat.catalogue_index_with_provenance(OX)
        assert any("19,540" in e for e in idx)


# ═══ 10 — isolation ═══════════════════════════════════════════════════════

class TestTenantIsolation:

    def test_no_cross_tenant_price_in_either_block(self, seeded):
        ox, b = _block(OX, "fee"), _block(B, "fee")
        assert "1234" not in ox and "Beta Yoga" not in ox
        assert "19540" not in b and "PGDCA" not in b

    def test_the_exclusion_applies_to_every_tenant(self, seeded):
        b = _block(B, "fee")
        assert "4321" not in b and "base_price" not in b
        assert "commercial.normal_total_fee: 1234" in b
