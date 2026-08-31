"""Phase RC2.5.3a — knowledge renderer nested-attributes fix.

THE GAP
-------
_render_row() only rendered top-level scalar attributes
(isinstance(v, (str, int, float))). The approved pricing schema --
commercial{base_price, currency, payment_url, offers[]} and
regulatory{source, as_of, components[]} -- is nested, so it parsed and
stored correctly but was SILENTLY DROPPED from every composed prompt: no
error, no log, just absent.

WHAT THIS PHASE DOES
---------------------
_flatten_attrs() walks dicts/lists into dotted/indexed keys
("commercial.offers[0].final_price") so nested scalars reach the rendered
block. Two independent, small bounds (_MAX_ATTR_DEPTH, _MAX_LIST_ITEMS) sit
alongside the existing MAX_ITEMS/MAX_CHARS. Rendering only -- no arithmetic,
no "active offer" judgement; both are explicitly deferred to a future,
separately-authorised consumer.

OUT OF SCOPE, NOT TOUCHED: models, migrations, COURSE_PAYMENT_LINKS,
AALIZA_PROMPT, prompt_composer, tenant isolation (_base_query/fetch_knowledge
untouched), admin UI, WhatsApp, CRM. No production data written -- confirmed
in the implementation report via a fresh read-only production check.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc253a_nested_attrs.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc253an-admin-key")
os.environ.setdefault("SECRET_KEY", "rc253an-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc253an-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
os.environ.setdefault("GEMINI_API_KEY", "rc253an-fake-gemini-key")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, TenantKnowledge                          # noqa: E402
from app.services import knowledge_service as ks                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KS_PY = os.path.join(ROOT, "app", "services", "knowledge_service.py")

OX = "t-ox"
TA = "t-alpha"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


def _k(tenant_id, kind, title, body=None, attrs=None, active=True, order=0):
    return TenantKnowledge(
        tenant_id=tenant_id, kind=kind, title=title, body=body,
        attributes=json.dumps(attrs if attrs is not None else {}),
        is_active=active, sort_order=order,
    )


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True))
        db.session.add(Tenant(id=TA, name="Alpha", slug=TA, status="ACTIVE",
                              billing_exempt=True))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


PRICING_ATTRS = {
    "duration": "12 Months",
    "emi_available": True,
    "commercial": {
        "currency": "INR",
        "base_price": 15999,
        "payment_url": "https://rzp.io/rzp/KAQ2C7t",
        "offers": [
            {"id": "diwali-2026", "label": "Diwali Offer",
             "final_price": 13999, "valid_from": "2026-10-15",
             "valid_until": "2026-11-05"},
        ],
    },
    "regulatory": {
        "source": "Kerala State Rutronix fee card",
        "as_of": "2026",
        "components": [
            {"type": "registration_fee", "label": "Registration Fee", "amount": 4500},
            {"type": "net_tuition_fee", "label": "Net Tuition Fee to ATC", "amount": 15040},
        ],
    },
}


# ═══ Requirement 1 — existing scalar behaviour byte-identical ═════════════

class TestScalarRenderingUnchanged:

    def test_flat_scalar_attrs_render_exactly_as_before(self, seeded):
        """The exact pre-fix algorithm, run in parallel, must match."""
        attrs = {"fee": "ALPHA-FEE-9999", "duration": "12 weeks", "z_last": "x"}
        old_rendered = [
            f"{k}: {v}" for k, v in sorted(attrs.items())
            if isinstance(v, (str, int, float)) and str(v).strip()
        ]
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "Bootcamp", "A body.", attrs))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert line == "Bootcamp — A body. — " + " | ".join(old_rendered)

    def test_empty_attrs_render_as_before(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_FAQ, "Refunds?", "7 day policy.", {}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert line == "Refunds? — 7 day policy."

    def test_bool_and_zero_still_render(self, seeded):
        """bool is a subclass of int in the original check too -- False/0
        must still render, only None and blank strings are dropped."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"emi_available": False, "seats_left": 0,
                               "note": None, "blank": ""}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "emi_available: False" in line
        assert "seats_left: 0" in line
        assert "note" not in line
        assert "blank" not in line


# ═══ Requirements 2/3 — nested commercial/regulatory render ═══════════════

class TestNestedRendering:

    def test_commercial_base_price_currency_payment_url_render(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, PRICING_ATTRS))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.base_price: 15999" in line
        assert "commercial.currency: INR" in line
        assert "commercial.payment_url: https://rzp.io/rzp/KAQ2C7t" in line

    def test_offers_array_renders(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, PRICING_ATTRS))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.offers[0].label: Diwali Offer" in line
        assert "commercial.offers[0].final_price: 13999" in line

    def test_regulatory_components_render(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, PRICING_ATTRS))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "regulatory.components[0].label: Registration Fee" in line
        assert "regulatory.components[0].amount: 4500" in line
        assert "regulatory.components[1].label: Net Tuition Fee to ATC" in line

    def test_nested_rendering_reaches_the_composed_block(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, PRICING_ATTRS))
            db.session.commit()
            block = ks.render_knowledge_block(TA)
        assert "commercial.base_price: 15999" in block
        assert "regulatory.components[0].amount: 4500" in block

    def test_dict_keys_sorted_deterministically(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"z_top": {"b": 1, "a": 2}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert line.index("z_top.a") < line.index("z_top.b")

    def test_list_item_order_preserved_not_sorted(self, seeded):
        """List order is authored and meaningful -- registration fee before
        net tuition, not alphabetised."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "regulatory": {"components": [
                    {"label": "zzz first"}, {"label": "aaa second"},
                ]}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert line.index("[0].label: zzz first") < line.index("[1].label: aaa second")


# ═══ Requirement 9 — empty/null nested structures ══════════════════════════

class TestEmptyNestedStructures:

    def test_empty_offers_list_produces_no_offers_line(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": {"base_price": 100, "offers": []}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.base_price: 100" in line
        assert "offers" not in line

    def test_empty_dict_produces_nothing(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": {}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert line == "X"

    def test_null_nested_value_not_rendered_as_none_string(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": {"base_price": 100, "payment_url": None}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "None" not in line


# ═══ Requirement 8 — malformed nested data fails open ══════════════════════

class TestMalformedNestedDataFailsOpen:

    def test_commercial_as_wrong_type_does_not_raise(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": "not a dict", "duration": "6 Months"}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "duration: 6 Months" in line
        assert "commercial: not a dict" in line  # a bare string IS a scalar

    def test_offers_as_wrong_type_does_not_raise(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": {"base_price": 100, "offers": "oops"}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.base_price: 100" in line
        assert "commercial.offers: oops" in line

    def test_malformed_top_level_json_still_fails_open(self, seeded):
        """Unchanged pre-existing contract: unparsable attributes -> {}."""
        with _APP.app_context():
            row = _k(TA, ks.KIND_COURSE, "X", None, {})
            db.session.add(row)
            db.session.commit()
            row.attributes = "{not valid json"
            db.session.commit()
            line = ks._render_row(row)
        assert line == "X"

    def test_deeply_nested_garbage_does_not_raise(self, seeded):
        pathological = {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, pathological))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)  # must not raise
        assert isinstance(line, str)

    def test_depth_cap_actually_excludes_content_beyond_it(self, seeded):
        """Not raising is necessary but not sufficient -- this proves
        _MAX_ATTR_DEPTH actually bounds what gets rendered, not merely that
        deep JSON happens not to crash Python's own recursion limit (a
        6-level dict never would, cap or no cap)."""
        nested = {}
        cursor = nested
        for i in range(ks._MAX_ATTR_DEPTH + 6):
            cursor["lvl"] = {}
            cursor = cursor["lvl"]
        cursor["marker"] = "SHOULD-NOT-APPEAR"
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, nested))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "SHOULD-NOT-APPEAR" not in line

    def test_content_within_depth_cap_still_renders(self, seeded):
        """Companion to the above -- proves the cap doesn't just blank
        everything, only content past the boundary."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"a": {"b": {"c": "within-cap-value"}}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "within-cap-value" in line

    def test_composer_survives_malformed_pricing_data(self, seeded):
        from app.services import prompt_composer
        from app.bot.prompts import AALIZA_PROMPT
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": ["not", "a", "dict", "either"]}))
            db.session.commit()
            out = prompt_composer.compose_system_prompt(TA)
        assert out != AALIZA_PROMPT  # knowledge present -> composed differently
        assert "PLATFORM RULES" in out  # still safe, still well-formed


# ═══ Requirement 4 — large nested payload remains capped ═══════════════════

class TestNestedPayloadCapped:

    def test_long_offers_list_is_capped(self, seeded):
        many_offers = [{"label": f"Offer {i}", "final_price": i} for i in range(50)]
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": {"offers": many_offers}}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "[0].label: Offer 0" in line
        assert f"[{ks._MAX_LIST_ITEMS - 1}]" in line
        assert f"[{ks._MAX_LIST_ITEMS}]" not in line  # the (N+1)th item absent

    def test_max_chars_still_bounds_the_whole_block(self, seeded):
        """The outer render_knowledge_block() cap is untouched by this fix."""
        with _APP.app_context():
            for i in range(8):
                db.session.add(_k(TA, ks.KIND_COURSE, f"Course {i}", "x" * 900,
                                  {"commercial": {"base_price": i}}, order=i))
            db.session.commit()
            block = ks.render_knowledge_block(TA)
        assert len(block) < ks.MAX_CHARS + 500

    def test_max_items_still_bounds_row_count(self, seeded):
        with _APP.app_context():
            for i in range(20):
                db.session.add(_k(TA, ks.KIND_COURSE, f"C{i}", None,
                                  {"commercial": {"base_price": i}}, order=i))
            db.session.commit()
            assert len(ks.fetch_knowledge(TA)) <= ks.MAX_ITEMS


# ═══ Requirement 5 — no pricing arithmetic ══════════════════════════════════

class TestNoArithmetic:

    def test_final_price_rendered_verbatim_not_derived(self, seeded):
        """No discount/base_price subtraction performed anywhere -- the
        offer's stated final_price is the only number that appears for it."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "commercial": {"base_price": 15999, "offers": [
                    {"label": "Sale", "final_price": 13999}
                ]}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.base_price: 15999" in line
        assert "commercial.offers[0].final_price: 13999" in line
        assert "2000" not in line  # no computed discount amount anywhere

    def test_renderer_source_performs_no_arithmetic_on_data(self):
        """AST check, scoped correctly: `depth + 1` is recursion bookkeeping,
        not pricing arithmetic, so a blanket "zero BinOp" ban would false-
        positive on it. The actual invariant is that no BinOp operates on the
        tenant-authored VALUE being rendered -- only on `depth`, the
        recursion counter. _render_row has no legitimate arithmetic at all."""
        tree = ast.parse(open(KS_PY, encoding="utf-8").read())

        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_render_row")
        assert not any(isinstance(n, ast.BinOp) for n in ast.walk(fn)), \
            "_render_row must contain no arithmetic at all"

        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_flatten_attrs")
        binops = [n for n in ast.walk(fn) if isinstance(n, ast.BinOp)]
        assert binops, "expected the depth+1 recursion counter to exist"
        for node in binops:
            is_depth_increment = (
                isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Name) and node.left.id == "depth"
                and isinstance(node.right, ast.Constant) and node.right.value == 1
            )
            assert is_depth_increment, (
                "unexpected arithmetic in _flatten_attrs on something other "
                f"than the depth counter: {ast.dump(node)}"
            )

    def test_no_offer_judged_active_by_date(self, seeded):
        """valid_from/valid_until are rendered as plain facts; nothing
        compares them to "now" or filters offers by validity."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "commercial": {"offers": [
                    {"label": "Expired", "final_price": 1,
                     "valid_until": "2020-01-01"}
                ]}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        # An expired offer still renders -- filtering by date is explicitly
        # NOT this function's job.
        assert "commercial.offers[0].label: Expired" in line


# ═══ RC2.5.3a-K — legacy_payment_url must never render ═════════════════════
#
# THE GAP THIS CLASS CLOSES
# --------------------------
# commercial.legacy_payment_url is intentionally STORED as a non-active
# reference (RC2.5.3a-K's own manifest design), but the generic flattener
# has no concept of "store this, never say it" -- it treats every scalar
# uniformly. During RC2.5.3a-K's own production validation this put
# https://rzp.io/rzp/KAQ2C7t (PGDCA's real legacy Razorpay link) into
# Oxford's LIVE composed AI prompt, which is what triggered that phase's
# rollback. Reproduced here with the exact same URL, but only ever against
# the test SQLite database this file already uses -- never production.

class TestLegacyPaymentUrlExclusion:

    def test_active_payment_url_renders_when_populated(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "commercial": {"base_price": 100,
                              "payment_url": "https://rzp.io/rzp/ACTIVE123"}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.payment_url: https://rzp.io/rzp/ACTIVE123" in line

    def test_legacy_payment_url_does_not_appear_in_rendered_block(self, seeded):
        """Reproduces the exact RC2.5.3a-K production finding: PGDCA's real
        legacy URL, test database only."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE,
                              "Post Graduate Diploma in Computer Applications (PGDCA)",
                              None, {
                "commercial": {
                    "currency": "INR",
                    "base_price": 19540,
                    "payment_url": None,
                    "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t",
                    "offers": [],
                }
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "rzp.io/rzp/KAQ2C7t" not in line
        assert "legacy_payment_url" not in line

    def test_row_with_both_fields_renders_active_excludes_legacy(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "commercial": {
                    "base_price": 19540,
                    "payment_url": "https://rzp.io/rzp/NEWLINK",
                    "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t",
                }
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.payment_url: https://rzp.io/rzp/NEWLINK" in line
        assert "rzp.io/rzp/KAQ2C7t" not in line
        assert "legacy_payment_url" not in line

    def test_other_scalar_attributes_still_render_normally(self, seeded):
        """The exclusion is scoped to one key name -- everything else in the
        same nested structure is unaffected."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "duration": "12 Months",
                "commercial": {
                    "currency": "INR",
                    "base_price": 19540,
                    "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t",
                },
                "regulatory": {
                    "components": [{"type": "registration_fee", "amount": 4500}],
                },
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "duration: 12 Months" in line
        assert "commercial.currency: INR" in line
        assert "commercial.base_price: 19540" in line
        assert "regulatory.components[0].amount: 4500" in line
        assert "rzp.io/rzp/KAQ2C7t" not in line

    def test_legacy_payment_url_excluded_regardless_of_nesting_location(self, seeded):
        """The rule is a bare key-name match, not a fixed dotted path --
        proven by putting the field somewhere other than commercial.*."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "regulatory": {"legacy_payment_url": "https://rzp.io/rzp/SHOULDNOTAPPEAR"}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "SHOULDNOTAPPEAR" not in line

    def test_legacy_payment_url_excluded_from_the_composed_block(self, seeded):
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, {
                "commercial": {"base_price": 19540,
                              "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t"}
            }))
            db.session.commit()
            block = ks.render_knowledge_block(TA)
        assert "rzp.io/rzp/KAQ2C7t" not in block
        assert "commercial.base_price: 19540" in block

    def test_composed_prompt_has_no_legacy_url_but_keeps_active_one(self, seeded):
        """End-to-end through prompt_composer: the exact property that
        matters -- what actually reaches Gemini's system_instruction."""
        from app.services import prompt_composer
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "PGDCA", None, {
                "commercial": {
                    "base_price": 19540,
                    "payment_url": "https://rzp.io/rzp/CURRENTLINK",
                    "legacy_payment_url": "https://rzp.io/rzp/KAQ2C7t",
                }
            }))
            db.session.commit()
            out = prompt_composer.compose_system_prompt(TA)
        assert "rzp.io/rzp/KAQ2C7t" not in out
        assert "rzp.io/rzp/CURRENTLINK" in out

    def test_malformed_attrs_still_fail_open_with_the_new_rule_present(self, seeded):
        """The exclusion check must not weaken the existing fail-open
        contract for unrelated malformed data."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None,
                              {"commercial": "not a dict",
                               "legacy_payment_url": "https://rzp.io/top-level"}))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)  # must not raise
        # A top-level (not nested under commercial) legacy_payment_url key is
        # ALSO excluded -- the rule is key-name-based, not path-based.
        assert "rzp.io/top-level" not in line
        assert "commercial: not a dict" in line  # unrelated data unaffected

    def test_does_not_suppress_a_key_that_merely_contains_the_word_legacy(self, seeded):
        """The exclusion is an EXACT key-name match, not a substring/prefix
        match -- verifies the fix isn't accidentally over-broad."""
        with _APP.app_context():
            db.session.add(_k(TA, ks.KIND_COURSE, "X", None, {
                "commercial": {"legacy_notes": "still renders",
                              "base_price": 100}
            }))
            db.session.commit()
            row = TenantKnowledge.query.filter_by(tenant_id=TA).first()
            line = ks._render_row(row)
        assert "commercial.legacy_notes: still renders" in line


# ═══ Requirement 7 — no tenant isolation regression ═══════════════════════

class TestIsolationUnregressed:

    def test_cross_tenant_leakage_still_impossible_with_nested_data(self, seeded):
        with _APP.app_context():
            db.session.add(_k(OX, ks.KIND_COURSE, "Oxford Course", None,
                              {"commercial": {"base_price": 1, "payment_url": "OXFORD-SECRET"}}))
            db.session.add(_k(TA, ks.KIND_COURSE, "Alpha Course", None,
                              {"commercial": {"base_price": 2, "payment_url": "ALPHA-SECRET"}}))
            db.session.commit()
            oxford_block = ks.render_knowledge_block(OX)
            alpha_block = ks.render_knowledge_block(TA)
        assert "ALPHA-SECRET" not in oxford_block
        assert "OXFORD-SECRET" not in alpha_block

    def test_base_query_and_fetch_knowledge_unmodified(self):
        """Only _render_row/_attributes and the new helpers changed --
        the isolation-critical functions must be byte-identical to the
        already-validated RC2.5.3a commit."""
        import subprocess
        head_src = subprocess.run(
            ["git", "show", "741b28041aad6feeca9b010eba4c739d8f5f8e3e:app/services/knowledge_service.py"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
        working_src = open(KS_PY, encoding="utf-8").read()

        def body_of(src, name):
            tree = ast.parse(src)
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            lines = src.splitlines()
            return "\n".join(lines[fn.lineno - 1:fn.end_lineno])

        for fn_name in ("_base_query", "fetch_knowledge"):
            assert body_of(head_src, fn_name) == body_of(working_src, fn_name), \
                f"{fn_name} changed -- out of scope for this phase"


# ═══ Scope ═══════════════════════════════════════════════════════════════

class TestScope:

    def test_only_knowledge_service_and_this_test_file_changed(self):
        """Scoped to app/ + tests/ + migrations/ only, matching every prior
        phase's scope-audit convention in this session -- the repo root also
        holds ~80 pre-existing untracked files (some with spaces in their
        names, e.g. a PDF handbook), unrelated to any phase and not worth
        enumerating here. Porcelain lines are parsed by fixed offset (status
        code is always exactly 2 chars + 1 space), not by whitespace-split,
        so a filename containing a space is not mis-parsed."""
        import subprocess
        allowed_new = {
            "app/services/knowledge_service.py",
            "tests/test_knowledge_nested_attrs_rc253a.py",
        }
        for scope in ("app/", "tests/", "migrations/"):
            out = subprocess.run(["git", "status", "--porcelain", "--", scope],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            # NOT out.strip().splitlines() -- .strip() on the whole multi-line
            # blob eats the leading space off only the FIRST line, corrupting
            # its fixed 3-char status-code offset. Split first, strip nothing.
            for ln in out.splitlines():
                if not ln:
                    continue
                path = ln[3:].strip('"')
                if path == "app/bot/screens.py":
                    continue  # one of the 12 pre-existing modifications
                if path.endswith(".rc253akbak") or path.endswith(".bak"):
                    continue  # the mutation harness's own temp backup file
                assert path in allowed_new, f"unexpected change: {path}"

    def test_no_migration_files_touched(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        assert out.strip() == ""

    def test_models_untouched(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "app/models.py"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        assert out.strip() == ""

    def test_payment_links_untouched(self):
        from app.bot.constants import COURSE_PAYMENT_LINKS
        assert COURSE_PAYMENT_LINKS["PGDCA"][4] == "https://rzp.io/rzp/KAQ2C7t"

    def test_prompt_composer_and_aaliza_prompt_untouched(self):
        import subprocess
        for f in ("app/services/prompt_composer.py", "app/bot/prompts.py"):
            out = subprocess.run(["git", "status", "--porcelain", "--", f],
                                 cwd=ROOT, capture_output=True, text=True).stdout
            assert out.strip() == "", f"{f} unexpectedly changed"
