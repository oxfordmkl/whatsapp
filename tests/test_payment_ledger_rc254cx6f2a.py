"""Phase RC2.5.4c-x-6f2a: the tenant-scoped payment ledger FOUNDATION.

WHAT THIS PHASE IS
-------------------
Schema only. x-6f1 stopped the bot treating customer text as proof of
payment; the x-6f2 audit found there is nowhere to record a payment even when
verification becomes possible. This phase adds that table and NOTHING else:
no writer, no verification, no provider credentials, no change to
payment_pending behaviour.

So these tests prove three distinct things:
  1. the ledger's shape enforces what the architecture depends on -- tenant
     ownership, provider identity, integer money, stable course code;
  2. duplicate provider payments are impossible at the DATABASE level, across
     customers and across tenants (TestIdempotency);
  3. nothing writes to it yet (TestNoWriterExists) -- the property that keeps
     this phase a foundation rather than a live payment path.

Source-level assertions are AST-based where a substring could match this
module's own prose. Import isolation and the throwaway-SQLite harness follow
test_payment_link_isolation_rc255b.py.
"""
import ast
import os
import sys
import tempfile

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6f2a_payment_ledger.db")
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
from app.models import ConversationState, Payment, Tenant               # noqa: E402

OX = "t-ox"
B = "t-b"

_APP = create_app()
_APP.config["TESTING"] = True

_MIGRATION = os.path.join(_ROOT, "migrations", "versions",
                          "e6d1b9a37f24_rc2_5_4c_x_6f2a_payment_ledger.py")
with open(_MIGRATION, encoding="utf-8") as _fh:
    _MIG_SRC = _fh.read()

T = Payment.__table__


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def payment(tenant_id=OX, provider="razorpay", provider_payment_id="pay_1",
            *, conversation_state_id=None, course_code="PGDCA",
            expected_amount_minor=1954000, paid_amount_minor=1954000,
            currency="INR", status=Payment.STATUS_VERIFIED,
            verification_source=Payment.SOURCE_WEBHOOK):
    """A ledger row built from DUMMY values. No real payment reference."""
    return Payment(
        tenant_id=tenant_id, provider=provider,
        provider_payment_id=provider_payment_id,
        conversation_state_id=conversation_state_id, course_code=course_code,
        expected_amount_minor=expected_amount_minor,
        paid_amount_minor=paid_amount_minor, currency=currency,
        status=status, verification_source=verification_source)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Tenant(id=OX, name="Oxford", slug="ox", status="ACTIVE",
                   billing_exempt=True),
            Tenant(id=B, name="Beta", slug="beta", status="ACTIVE",
                   billing_exempt=True),
        ])
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


def _add(*rows):
    with _APP.app_context():
        db.session.add_all(rows)
        db.session.commit()


def _count(**filters):
    with _APP.app_context():
        return Payment.query.filter_by(**filters).count()


# ── the table exists and is created by the model ────────────────────────────

class TestLedgerCreation:

    def test_table_is_named_payments(self):
        assert T.name == "payments"

    def test_the_model_creates_its_table(self, seeded):
        with _APP.app_context():
            assert sa.inspect(db.engine).has_table("payments")

    def test_a_row_can_be_created_and_read_back(self, seeded):
        _add(payment())
        with _APP.app_context():
            row = Payment.query.one()
            assert row.tenant_id == OX
            assert row.provider == "razorpay"
            assert row.paid_amount_minor == 1954000
            assert row.currency == "INR"
            assert row.status == Payment.STATUS_VERIFIED
            assert row.created_at is not None, "created_at must default"

    def test_ledger_starts_empty(self, seeded):
        assert _count() == 0


# ── tenant isolation ────────────────────────────────────────────────────────

class TestTenantIsolation:

    def test_tenant_id_is_mandatory(self, seeded):
        assert T.c.tenant_id.nullable is False

    def test_tenant_id_is_a_foreign_key_to_tenants(self):
        assert [fk.target_fullname for fk in T.c.tenant_id.foreign_keys] == \
               ["tenants.id"]

    def test_tenant_id_type_matches_the_project_convention(self):
        assert isinstance(T.c.tenant_id.type, sa.String)
        assert T.c.tenant_id.type.length == 36

    def test_a_payment_without_a_tenant_is_rejected(self, seeded):
        with pytest.raises(IntegrityError):
            _add(payment(tenant_id=None))

    def test_each_tenants_payments_are_separate(self, seeded):
        _add(payment(OX, provider_payment_id="pay_ox"),
             payment(B, provider_payment_id="pay_b"))
        assert _count(tenant_id=OX) == 1
        assert _count(tenant_id=B) == 1
        with _APP.app_context():
            assert Payment.query.filter_by(tenant_id=OX).one().provider_payment_id \
                == "pay_ox"

    def test_the_same_course_code_under_two_tenants_stays_isolated(self, seeded):
        """PGDCA in tenant B is not PGDCA in Oxford. The ledger must be able to
        hold both, and a tenant-scoped query must return only its own."""
        _add(payment(OX, provider_payment_id="pay_ox", course_code="PGDCA"),
             payment(B, provider_payment_id="pay_b", course_code="PGDCA"))
        assert _count(course_code="PGDCA") == 2
        assert _count(tenant_id=OX, course_code="PGDCA") == 1
        assert _count(tenant_id=B, course_code="PGDCA") == 1

    def test_the_tenant_indexes_exist(self):
        names = {i.name for i in T.indexes}
        for expected in ("ix_payments_tenant_provider",
                         "ix_payments_tenant_status",
                         "ix_payments_tenant_course_code"):
            assert expected in names, f"missing index {expected}"

    def test_each_tenant_index_leads_with_tenant_id(self):
        for index in T.indexes:
            if index.name.startswith("ix_payments_tenant_") and \
                    len(index.columns) > 1:
                assert list(index.columns)[0].name == "tenant_id", (
                    f"{index.name} does not lead with tenant_id")

    def test_no_primary_tenant_or_global_fallback_in_the_model(self):
        """The ledger must not know about a default tenant."""
        src = _src("app/models.py")
        tree = ast.parse(src)
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "Payment")
        body = ast.get_source_segment(src, cls)
        assert "PRIMARY_TENANT_ID" not in body
        assert "default_tenant" not in body


# ── idempotency: the constraint, not a lookup ───────────────────────────────

class TestIdempotency:

    def test_provider_payment_identity_is_unique(self):
        uniques = {c.name: [col.name for col in c.columns]
                   for c in T.constraints
                   if isinstance(c, sa.UniqueConstraint)}
        assert uniques.get("uq_payment_provider_payment_id") == \
               ["provider", "provider_payment_id"]

    def test_the_same_payment_id_cannot_be_inserted_twice(self, seeded):
        _add(payment(provider_payment_id="pay_dup"))
        with pytest.raises(IntegrityError):
            _add(payment(provider_payment_id="pay_dup",
                         conversation_state_id=None))

    def test_the_same_payment_id_cannot_be_claimed_by_two_tenants(self, seeded):
        """Cross-tenant replay: B must not be able to record Oxford's payment."""
        _add(payment(OX, provider_payment_id="pay_shared"))
        with pytest.raises(IntegrityError):
            _add(payment(B, provider_payment_id="pay_shared"))

    def test_the_same_payment_id_cannot_serve_two_courses(self, seeded):
        _add(payment(provider_payment_id="pay_two_courses", course_code="PGDCA"))
        with pytest.raises(IntegrityError):
            _add(payment(provider_payment_id="pay_two_courses",
                         course_code="AIDM"))

    def test_different_tenants_may_have_their_own_distinct_payments(self, seeded):
        _add(payment(OX, provider_payment_id="pay_a"),
             payment(B, provider_payment_id="pay_b"))
        assert _count() == 2

    def test_the_same_id_under_a_different_provider_is_a_different_payment(self, seeded):
        _add(payment(provider="razorpay", provider_payment_id="pay_x"),
             payment(provider="stripe", provider_payment_id="pay_x"))
        assert _count() == 2

    def test_provider_identity_columns_are_not_nullable(self):
        """NULLs are distinct under UNIQUE in PostgreSQL, so nullable identity
        would permit unlimited duplicates and defeat the constraint."""
        assert T.c.provider.nullable is False
        assert T.c.provider_payment_id.nullable is False

    def test_a_payment_without_a_provider_is_rejected(self, seeded):
        with pytest.raises(IntegrityError):
            _add(payment(provider=None))

    def test_a_payment_without_a_provider_payment_id_is_rejected(self, seeded):
        with pytest.raises(IntegrityError):
            _add(payment(provider_payment_id=None))

    def test_there_is_exactly_one_uniqueness_mechanism(self):
        uniques = [c for c in T.constraints if isinstance(c, sa.UniqueConstraint)]
        unique_indexes = [i for i in T.indexes if i.unique]
        assert len(uniques) == 1 and unique_indexes == [], (
            "a second uniqueness mechanism would fragment idempotency")


# ── money, currency, status, bindings ────────────────────────────────────────

class TestFieldContracts:

    @pytest.mark.parametrize("column", ["expected_amount_minor",
                                        "paid_amount_minor"])
    def test_money_is_integer_minor_units(self, column):
        col = T.c[column]
        assert isinstance(col.type, sa.Integer)
        assert not isinstance(col.type, (sa.Float, sa.Numeric))

    def test_the_model_declares_no_float_or_decimal_money(self):
        src = _src("app/models.py")
        cls = next(n for n in ast.parse(src).body
                   if isinstance(n, ast.ClassDef) and n.name == "Payment")
        body = ast.get_source_segment(src, cls)
        for banned in ("db.Float", "db.Numeric", "db.Decimal"):
            assert banned not in body, f"{banned} must never hold money"

    def test_paid_amount_is_required_and_expected_amount_is_not(self):
        """What the provider says arrived is always known; what the catalogue
        expected may not be, for a payment that binds to no course."""
        assert T.c.paid_amount_minor.nullable is False
        assert T.c.expected_amount_minor.nullable is True

    def test_a_payment_without_a_paid_amount_is_rejected(self, seeded):
        with pytest.raises(IntegrityError):
            _add(payment(paid_amount_minor=None))

    def test_expected_and_paid_amounts_are_stored_separately(self, seeded):
        """A mismatch must remain visible rather than being reconciled away."""
        _add(payment(expected_amount_minor=1954000, paid_amount_minor=100))
        with _APP.app_context():
            row = Payment.query.one()
            assert row.expected_amount_minor == 1954000
            assert row.paid_amount_minor == 100

    def test_currency_is_stored_and_required(self, seeded):
        assert isinstance(T.c.currency.type, sa.String)
        assert T.c.currency.type.length == 3
        assert T.c.currency.nullable is False
        _add(payment(currency="INR"))
        with _APP.app_context():
            assert Payment.query.one().currency == "INR"

    def test_a_payment_without_a_currency_is_rejected(self, seeded):
        with pytest.raises(IntegrityError):
            _add(payment(currency=None))

    def test_status_is_required_and_has_no_silent_default(self, seeded):
        assert T.c.status.nullable is False
        assert T.c.status.default is None and T.c.status.server_default is None
        with pytest.raises(IntegrityError):
            _add(payment(status=None))

    def test_status_values_follow_the_project_string_convention(self):
        """Plain strings, like BillingInvoice/Task/Campaign -- no DB enum, so a
        new value never needs a migration. The permitted set is pinned here."""
        assert Payment.STATUSES == ("submitted", "verified", "rejected")
        assert isinstance(T.c.status.type, sa.String)
        assert not isinstance(T.c.status.type, sa.Enum)

    @pytest.mark.parametrize("status", ["submitted", "verified", "rejected"])
    def test_each_declared_status_can_be_stored(self, seeded, status):
        _add(payment(provider_payment_id=f"pay_{status}", status=status))
        assert _count(status=status) == 1

    def test_verification_source_records_how_a_payment_was_proven(self, seeded):
        """A staff-verified row must be distinguishable from a webhook-verified
        one; without this the ledger cannot say what the evidence was."""
        assert Payment.VERIFICATION_SOURCES == ("webhook", "lookup", "staff")
        assert T.c.verification_source.nullable is True, (
            "an unverified row has no source yet")
        _add(payment(provider_payment_id="pay_staff",
                     verification_source=Payment.SOURCE_STAFF))
        assert _count(verification_source="staff") == 1

    def test_verified_at_is_nullable_until_verification(self, seeded):
        assert T.c.verified_at.nullable is True
        _add(payment())
        with _APP.app_context():
            assert Payment.query.one().verified_at is None

    def test_course_code_holds_the_stable_code_not_a_title(self):
        col = T.c.course_code
        assert isinstance(col.type, sa.String)
        assert col.type.length == 32, (
            "32 matches payment_link_service's _MAX_CODE_LEN -- a code, not a "
            "title")
        assert col.foreign_keys == set(), (
            "courses live in TenantKnowledge JSON; an FK would invent a table")
        src = _src("app/models.py")
        cls = next(n for n in ast.parse(src).body
                   if isinstance(n, ast.ClassDef) and n.name == "Payment")
        body = ast.get_source_segment(src, cls)
        for banned in ("course_title", "course_name", "display_name"):
            assert banned not in body, (
                f"{banned} must never be the course identity")

    def test_conversation_binding_is_a_nullable_fk(self):
        """A webhook can deliver an authentic payment we cannot yet attribute.
        Recording it unbound is honest; NOT NULL would force a writer to guess
        a conversation."""
        col = T.c.conversation_state_id
        assert [fk.target_fullname for fk in col.foreign_keys] == \
               ["conversation_state.id"]
        assert col.nullable is True

    def test_a_payment_can_be_bound_to_a_conversation(self, seeded):
        with _APP.app_context():
            state = ConversationState(phone="+910000000000", tenant_id=OX,
                                      name="Dummy")
            db.session.add(state)
            db.session.commit()
            state_id = state.id
        _add(payment(conversation_state_id=state_id))
        with _APP.app_context():
            assert Payment.query.one().conversation_state_id == state_id

    def test_no_provider_credentials_live_on_the_ledger(self):
        """Credentials are a separate phase and must never be payment columns."""
        names = {c.name for c in T.columns}
        for banned in ("api_key", "api_secret", "key_id", "key_secret",
                       "webhook_secret", "credential", "token"):
            assert not any(banned in n for n in names), (
                f"{banned} must not be a payments column")


# ── nothing writes to the ledger yet ────────────────────────────────────────

class TestNoWriterExists:
    """The property that keeps this phase a FOUNDATION.

    AST-based: this module and the model both discuss Payment in prose, and a
    substring search would match the explanation instead of the code.
    """

    @staticmethod
    def _references_payment(rel):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Payment":
                return True
            if isinstance(node, ast.Attribute) and node.attr == "Payment":
                return True
            if isinstance(node, ast.ImportFrom):
                if any(a.name == "Payment" for a in node.names):
                    return True
        return False

    @pytest.mark.parametrize("rel", [
        "app/bot/offer_handlers.py",
        "app/bot/router.py",
        "app/bot/cta_handlers.py",
        "app/routes/webhook.py",
        "app/routes/billing.py",
        "app/routes/admin.py",
        "app/routes/tenant.py",
        "app/services/payment_link_service.py",
        "app/services/crm_service.py",
        "app/state.py",
    ])
    def test_no_module_creates_ledger_rows(self, rel):
        assert not self._references_payment(rel), (
            f"{rel} references the Payment model -- this phase adds NO writer")

    def test_handle_payment_still_writes_nothing(self):
        """x-6f1's containment is untouched: no ledger row, no state write, no
        CRM write, no confirmation.

        AST references, never substrings: handle_payment's docstring QUOTES the
        old "Payment Received" copy while explaining why it is gone, and a
        substring check would match that explanation and fail on correct code.
        """
        src = _src("app/bot/offer_handlers.py")
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "handle_payment")
        banned = {"Payment", "update_lead_status", "payment_confirmed_reply",
                  "commit", "session", "Thread", "add"}
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                assert node.id not in banned, f"handle_payment references {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned, (
                    f"handle_payment references .{node.attr}")
            assert not (isinstance(node, ast.Assign)
                        and isinstance(node.targets[0], ast.Subscript)), (
                "handle_payment must not write conversation state")

    def test_the_whole_application_has_no_payment_writer(self):
        """Exhaustive sweep: only app/models.py may name the model."""
        offenders = []
        for base, _dirs, files in os.walk(os.path.join(_ROOT, "app")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(base, name), _ROOT) \
                    .replace(os.sep, "/")
                if rel == "app/models.py":
                    continue
                if self._references_payment(rel):
                    offenders.append(rel)
        assert offenders == [], f"unauthorised Payment writer(s): {offenders}"

    def test_payment_pending_behaviour_is_unchanged(self):
        """The router still routes payment_pending to the contained handler."""
        src = _src("app/bot/router.py")
        assert "handle_payment(raw, name, st, phone, tenant_id)" in src

    def test_no_verification_is_implemented(self):
        """No provider SDK is imported anywhere in the app -- this phase adds
        none.

        Imports are read from the AST, not by substring: billing.py legitimately
        imports the NAMES RAZORPAY_WEBHOOK_SECRET and STRIPE_WEBHOOK_SECRET from
        app.config, and "import razorpay" matches that line as a substring.
        """
        offenders = []
        for base, _dirs, files in os.walk(os.path.join(_ROOT, "app")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(base, name), _ROOT) \
                    .replace(os.sep, "/")
                for node in ast.walk(ast.parse(_src(rel))):
                    mods = []
                    if isinstance(node, ast.Import):
                        mods = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        mods = [node.module or ""]
                    for mod in mods:
                        if mod.split(".")[0] in ("razorpay", "stripe"):
                            offenders.append(f"{rel}: {mod}")
        assert offenders == [], f"a provider SDK is integrated: {offenders}"


# ── the migration ───────────────────────────────────────────────────────────

class TestMigration:
    """Source-level, matching this repo's migration-test convention (see
    test_audience_segment_migration.py -- no Alembic runtime in the harness).
    The RUNTIME shape is proven above against the model's own metadata on a
    throwaway SQLite database."""

    def test_revision_id(self):
        assert "revision = 'e6d1b9a37f24'" in _MIG_SRC

    def test_it_descends_from_the_current_head(self):
        assert "down_revision = 'c1a7e93b45d2'" in _MIG_SRC

    def test_there_is_exactly_one_head_in_the_chain(self):
        import re
        versions = os.path.join(_ROOT, "migrations", "versions")
        revs = {}
        for name in os.listdir(versions):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(versions, name), encoding="utf-8") as fh:
                text = fh.read()
            rev = re.search(r"^revision\s*=\s*'([^']+)'", text, re.M)
            down = re.search(r"^down_revision\s*=\s*'([^']+)'", text, re.M)
            revs[rev.group(1)] = down.group(1) if down else None
        children = {d for d in revs.values() if d}
        heads = [r for r in revs if r not in children]
        assert heads == ["e6d1b9a37f24"], f"expected one head, found {heads}"

    def test_upgrade_creates_the_payments_table(self):
        assert "op.create_table(" in _MIG_SRC and "'payments'" in _MIG_SRC

    def test_upgrade_creates_the_uniqueness_constraint(self):
        assert "uq_payment_provider_payment_id" in _MIG_SRC
        assert "sa.UniqueConstraint('provider', 'provider_payment_id'" in _MIG_SRC

    def test_upgrade_creates_the_foreign_keys(self):
        assert "['tenants.id']" in _MIG_SRC
        assert "['conversation_state.id']" in _MIG_SRC

    @pytest.mark.parametrize("index", ["ix_payments_tenant_provider",
                                       "ix_payments_tenant_status",
                                       "ix_payments_tenant_course_code"])
    def test_upgrade_creates_each_index(self, index):
        assert index in _MIG_SRC

    def test_it_uses_batch_alter_table(self):
        assert "batch_alter_table" in _MIG_SRC

    def test_downgrade_drops_the_table(self):
        assert "def downgrade()" in _MIG_SRC
        assert "op.drop_table('payments')" in _MIG_SRC

    def test_money_columns_are_integers_in_the_migration(self):
        assert "sa.Column('paid_amount_minor', sa.Integer(), nullable=False)" in _MIG_SRC
        assert "sa.Column('expected_amount_minor', sa.Integer(), nullable=True)" in _MIG_SRC
        for banned in ("sa.Float", "sa.Numeric", "sa.Decimal"):
            assert banned not in _MIG_SRC

    def test_tenant_id_is_not_nullable_in_the_migration(self):
        assert "sa.Column('tenant_id', sa.String(length=36), nullable=False)" in _MIG_SRC

    def test_it_alters_no_existing_table(self):
        for banned in ("add_column", "drop_column", "alter_column"):
            assert banned not in _MIG_SRC, (
                "this migration creates one table and alters nothing")

    def test_it_backfills_nothing(self):
        """Checked over the CODE only. The module docstring explains why
        idempotency is a constraint rather than a "lookup-then-insert" check,
        and a whole-file substring search matches that prose."""
        tree = ast.parse(_MIG_SRC)
        code = "\n".join(
            ast.get_source_segment(_MIG_SRC, n) or ""
            for n in tree.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
        ).upper()
        for banned in ("INSERT", "UPDATE ", "DELETE", "EXECUTE("):
            assert banned not in code, "no data may be written by this migration"

    def test_it_creates_no_credential_columns(self):
        low = _MIG_SRC.lower()
        for banned in ("api_key", "api_secret", "key_secret", "webhook_secret",
                       "token"):
            assert banned not in low


# ── the model matches the migration ─────────────────────────────────────────

class TestModelMigrationAgreement:

    @pytest.mark.parametrize("column", [
        "id", "tenant_id", "provider", "provider_payment_id",
        "conversation_state_id", "course_code", "expected_amount_minor",
        "paid_amount_minor", "currency", "status", "verification_source",
        "verified_at", "created_at"])
    def test_every_model_column_is_in_the_migration(self, column):
        assert f"'{column}'" in _MIG_SRC

    def test_the_migration_adds_no_column_the_model_lacks(self):
        import re
        declared = set(re.findall(r"sa\.Column\('([a-z_]+)'", _MIG_SRC))
        assert declared == {c.name for c in T.columns}
