"""Phase H4-e — identifier columns must NEVER be truncated. Audit outcome.

H4-d's closing note said the bounded VARCHARs are written uncapped while only
the TEXT bodies are, and implied truncation as the natural follow-up. The
H4-e audit concluded the opposite for most of those fields, and this file is
the record so a later phase does not "finish the job" and break something.

WHAT THE AUDIT FOUND
--------------------
Production lengths vs declared limits:

    wa_message_id   max  78 / 100   <- the ONLY externally-bounded risk
    phone           max  12 /  20
    tenant_id       max  32 /  36   (uuid4().hex is ALWAYS 32)
    event_type      max  22 /  50   (internal constants)
    direction       max   8 /  10   ('outbound'/'incoming' — literals)
    staff_name      max   5 / 100   (fed by username, String(64))

A percentage-of-limit heuristic flagged direction (80%) and tenant_id (89%)
as "at risk". Both are false positives: those columns hold fixed internal
values that cannot grow. Who CONTROLS the value is the question, not how
close the current maximum sits to the ceiling.

WHY TRUNCATION IS THE WRONG FIX HERE
------------------------------------
These are IDENTIFIERS, not display text:

  * wa_message_id is the webhook's deduplication key
    (webhook.py: ConversationMessage.query.filter_by(wa_message_id=wamid)).
    Truncating it lets two distinct messages collide on one stored id, so a
    real inbound message is silently discarded as a "duplicate". That is
    worse than the DataError it would prevent.

  * phone is a lookup key at ~28 filter_by(phone=...) sites. A shortened
    phone attaches a message to the WRONG LEAD.

  * tenant_id is the tenant boundary. Truncating it is a cross-tenant defect
    by construction.

  * event_type drives 15 legacy readers that match on exact strings.

H4-d already gave these the correct failure mode: an over-limit write raises,
is logged, and is rolled back — the request survives and nothing is corrupted.
Failing loudly beats silently storing a wrong key.

WHAT THIS FILE DOES NOT CLAIM
-----------------------------
It does not claim the risk is zero. wa_message_id has 22 characters of
headroom and is supplied by WhatsApp, whose id format is not contractually
bounded. The audit's Option B — widening that ONE column to String(255) —
remains available and would need a migration. This file pins the decision
that was taken (Option A: no code change), not a proof that none is needed.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h4e_contract.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h4e-admin-key")
os.environ.setdefault("SECRET_KEY", "h4e-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h4e-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.models import (MessageLog, ConversationMessage, LeadEvent,     # noqa: E402
                        User, ConversationState)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGSVC = os.path.join(ROOT, "app", "services", "log_service.py")
WEBHOOK = os.path.join(ROOT, "app", "routes", "webhook.py")

_APP = create_app()

#: Columns that must NEVER be silently shortened, and why.
IDENTIFIER_COLUMNS = {
    "wa_message_id": "webhook deduplication key — a collision drops a real message",
    "phone":         "lead lookup key at ~28 filter_by(phone=...) sites",
    "tenant_id":     "the tenant boundary — truncation is a cross-tenant defect",
    "event_type":    "matched exactly by the legacy event readers",
}


def _fn(path, name):
    """Executable source of one function, docstring stripped."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    m = ast.parse(ast.unparse(fn)).body[0]
    if (m.body and isinstance(m.body[0], ast.Expr)
            and isinstance(m.body[0].value, ast.Constant)
            and isinstance(m.body[0].value.value, str)):
        m.body.pop(0)
    return ast.unparse(m)


WRITERS = ["log_message", "save_conversation_message", "log_lead_event"]


# ═══ the rule: no truncation of identifiers ══════════════════════════════════

class TestIdentifiersAreNotTruncated:

    @pytest.mark.parametrize("writer", WRITERS)
    @pytest.mark.parametrize("col", sorted(IDENTIFIER_COLUMNS))
    def test_writer_does_not_slice_the_identifier(self, writer, col):
        """Fails if someone adds `col=(value or '')[:N]` to a writer.

        The reason is in IDENTIFIER_COLUMNS[col] — read it before "fixing"
        this test.
        """
        src = _fn(LOGSVC, writer)
        tree = ast.parse(src)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            for kw in call.keywords:
                if kw.arg != col:
                    continue
                # A slice anywhere in the value expression is a truncation.
                assert not any(isinstance(sub, ast.Subscript)
                               and isinstance(sub.slice, ast.Slice)
                               for sub in ast.walk(kw.value)), (
                    f"{writer} truncates {col}: {IDENTIFIER_COLUMNS[col]}")

    @pytest.mark.parametrize("writer", WRITERS)
    def test_only_the_text_bodies_are_capped(self, writer):
        """_MAX_TEXT applies to free text only. If a slice appears anywhere
        else in a writer, this phase's decision is being reversed."""
        src = _fn(LOGSVC, writer)
        slices = [ln for ln in src.splitlines() if "[:" in ln]
        for ln in slices:
            assert ("message_text=" in ln or "message=" in ln), \
                f"{writer} truncates something other than the text body: {ln.strip()}"

    def test_max_text_is_unchanged(self):
        with open(LOGSVC, encoding="utf-8") as fh:
            src = fh.read()
        assert "_MAX_TEXT = 5000" in src


# ═══ why: the dedup semantics that truncation would break ════════════════════

class TestDedupSemantics:

    def test_webhook_dedupes_on_the_FULL_wa_message_id(self):
        """Equality on the whole id. Truncating on write while matching in
        full here would ALSO break dedup — in the other direction."""
        with open(WEBHOOK, encoding="utf-8") as fh:
            src = fh.read()
        assert "filter_by(wa_message_id=wamid)" in src

    def test_wa_message_id_column_is_wide_enough_for_observed_ids(self):
        """Production wamids run 62-78 chars against a 100 limit.

        This is the audit's ONE real exposure. If WhatsApp ever emits a longer
        id the write raises, is logged and rolled back (H4-d) — it does not
        corrupt. Widening the column to String(255) is Option B and needs a
        migration; this test records the current headroom, not a guarantee.
        """
        limit = ConversationMessage.__table__.c.wa_message_id.type.length
        assert limit == 100, "column width changed — revisit the H4-e decision"


# ═══ the latent schema mismatch, recorded ════════════════════════════════════

class TestLatentDisplayNameMismatch:

    def test_display_name_is_wider_than_staff_name(self):
        """A 101-120 char display_name would not fit staff_name.

        Not currently reachable: only actor['username'] (String(64)) is passed
        as staff_name. This test states the mismatch so it is not rediscovered
        from a production error.
        """
        assert User.__table__.c.display_name.type.length == 120
        assert ConversationMessage.__table__.c.staff_name.type.length == 100

    def test_no_caller_passes_a_display_name_as_staff_name(self):
        """The guard that keeps the mismatch latent. If a caller starts
        passing display_label()/display_name, EITHER widen staff_name to 120
        OR truncate it — staff_name is display text, so truncation is safe
        there, unlike the identifier columns above.
        """
        import subprocess
        out = subprocess.run(
            ["git", "grep", "-n", "staff_name=", "--", "app/"],
            cwd=ROOT, capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "log_service.py" in line or "models.py" in line:
                continue
            assert "display_name" not in line and "display_label" not in line, \
                f"a display name now feeds staff_name(100): {line}"


# ═══ the failure mode H4-d guarantees ════════════════════════════════════════

class TestOverLimitFailsSafely:

    @pytest.mark.parametrize("writer", WRITERS)
    def test_writer_rolls_back(self, writer):
        """What makes 'no truncation' an acceptable answer: an over-limit
        write fails loudly and the session survives."""
        assert "db.session.rollback()" in _fn(LOGSVC, writer)

    def test_identifier_columns_are_still_not_null_where_declared(self):
        """Unchanged by this phase — recorded so a later migration notices."""
        assert MessageLog.__table__.c.tenant_id.nullable is False
        assert LeadEvent.__table__.c.tenant_id.nullable is False
        assert ConversationMessage.__table__.c.tenant_id.nullable is False


# ═══ scope: H4-e changed no production code ══════════════════════════════════

class TestPhaseScope:

    def test_this_phase_is_audit_only(self):
        """H4-e is an AUDIT phase: its outcome was a decision, not a change.

        Asserted against the COMMIT that introduces this file once committed —
        the same commit-scoped form adopted after the working-tree assertions
        in the H4-a/H4-b suites broke on later phases.
        """
        import subprocess
        rel = os.path.relpath(__file__, ROOT).replace(os.sep, "/")
        sha = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "-1", "--", rel],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not sha:
            pytest.skip("H4-e is not committed yet")
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=ROOT, capture_output=True, text=True).stdout.split()
        prod = [f for f in files if f.startswith("app/")]
        assert prod == [], f"H4-e touched production code: {prod}"

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out
