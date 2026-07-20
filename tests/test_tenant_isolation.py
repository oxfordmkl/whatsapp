"""Phase 0 Sprint 2 — Tenant Isolation Test Suite (Constitution I.1).

Proves, with two seeded tenants (A = primary, B = other), that:
  1. Tenant A cannot READ tenant B records (state, tasks, logs).
  2. Tenant A cannot MODIFY tenant B records.
  3. Tenant A cannot DELETE tenant B records.
  4. Webhook tenant resolution routes each message to the correct tenant
     (matched WABA id / unknown WABA id dropped / suspended tenant dropped).
  5. Staff task actions remain tenant-scoped (TaskForbidden / invisible).
  6. resolve_tenant_id(): explicit wins; None resolves to PRIMARY_TENANT_ID,
     NOT to Tenant.query.first() — the exact regression that mis-filed 25
     lead_event rows in production (repaired Phase 17.1-C).

This suite is the foundation for security regression testing: it must run
(and block) in CI on every push, forever.

Run: python tests/test_tenant_isolation.py   (exit 0 = pass)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "_test_isolation.db"))
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("ADMIN_KEY", "x")
os.environ.setdefault("SECRET_KEY", "x")
os.environ.setdefault("BROADCAST_API_KEY", "x")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("WABA_ENCRYPTION_KEY", "FZsAc8GY_ayHq0cAxKXMMlUvSbJO2hKhpZOdGnaxO18=")
# Deliberately set so tenant B sorts FIRST: if any code path regresses to
# Tenant.query.first(), tests that expect the primary tenant will fail loudly.
os.environ["PRIMARY_TENANT_ID"] = "tenant-a-primary"

from app import create_app                     # noqa: E402
from app.extensions import db                  # noqa: E402
from app.models import (                       # noqa: E402
    Tenant, ConversationState, ConversationMessage, LeadEvent, MessageLog, Task,
)

app = create_app()

results = []


def chk(name, cond, detail=""):
    ok = bool(cond)
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}]  {name}" + (f"  — {detail}" if detail and not ok else ""))


with app.app_context():
    db.create_all()

    # ── Seed: tenant B inserted FIRST so Tenant.query.first() == B ─────────
    tb = Tenant(id="aaa-tenant-b-first", name="Tenant B", slug="tenant-b",
                status="ACTIVE", waba_phone_number_id="PHONE_B")
    ta = Tenant(id="tenant-a-primary", name="Tenant A", slug="tenant-a",
                status="ACTIVE", waba_phone_number_id="PHONE_A")
    ts = Tenant(id="zzz-tenant-suspended", name="Suspended", slug="suspended",
                status="SUSPENDED", waba_phone_number_id="PHONE_S")
    db.session.add_all([tb, ta, ts])
    db.session.commit()

    first = Tenant.query.first()
    chk("precondition: Tenant.query.first() is NOT the primary tenant",
        first.id == tb.id, f"first={first.id}")

    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== 6. resolve_tenant_id() — the crutch regression test ===")
    from app.services.log_service import resolve_tenant_id

    chk("explicit tenant_id always wins",
        resolve_tenant_id(tb.id) == tb.id)
    chk("None resolves to PRIMARY_TENANT_ID (config), not first row",
        resolve_tenant_id(None) == ta.id,
        f"got {resolve_tenant_id(None)}")
    chk("None does NOT resolve to Tenant.query.first()",
        resolve_tenant_id(None) != tb.id)

    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== 1. Tenant A cannot READ tenant B records ===")
    from app.state import get_or_create_state, phone_exists

    # Same phone number exists under both tenants — rows must be independent.
    PHONE = "919000000001"
    sa = get_or_create_state(PHONE, "Alice-A", tenant_id=ta.id)
    sb = get_or_create_state(PHONE, "Bob-B", tenant_id=tb.id)
    chk("same phone creates separate per-tenant state rows",
        ConversationState.query.filter_by(phone=PHONE).count() == 2)
    chk("tenant A read returns A's row, not B's",
        get_or_create_state(PHONE, "x", tenant_id=ta.id)["name"] == "Alice-A")
    chk("tenant B read returns B's row, not A's",
        get_or_create_state(PHONE, "x", tenant_id=tb.id)["name"] == "Bob-B")

    PHONE_B_ONLY = "919000000002"
    get_or_create_state(PHONE_B_ONLY, "OnlyB", tenant_id=tb.id)
    chk("phone_exists(A) is False for a B-only lead",
        phone_exists(PHONE_B_ONLY, tenant_id=ta.id) is False)
    chk("phone_exists(B) is True for the same lead",
        phone_exists(PHONE_B_ONLY, tenant_id=tb.id) is True)

    from app.services.task_service import list_tasks, create_task
    create_task(tenant_id=tb.id, title="B secret task", created_by="admin-b",
                assigned_staff="staff-b")
    a_tasks = list_tasks(tenant_id=ta.id)
    chk("tenant A task list does not contain tenant B's task",
        all(t.title != "B secret task" for t in a_tasks),
        str([t.title for t in a_tasks])[:120])

    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== 2. Tenant A cannot MODIFY tenant B records ===")
    from app.state import StateProxy

    # A write through tenant-A scope must not touch B's row for the same phone.
    sa2 = get_or_create_state(PHONE, "Alice-A", tenant_id=ta.id)
    sa2["stage"] = "course_viewed"          # StateProxy auto-saves, A-scoped
    b_row = ConversationState.query.filter_by(phone=PHONE, tenant_id=tb.id).first()
    chk("A-scoped stage write does not modify B's row",
        b_row.stage != "course_viewed", f"B.stage={b_row.stage}")

    from app.services.task_service import staff_update, TaskError
    b_task = Task.query.filter_by(tenant_id=tb.id).first()
    denied = False
    try:
        # Actor from tenant A attempts to update tenant B's task via A's scope:
        staff_update(tenant_id=ta.id, task_id=b_task.id,
                     actor="staff-a", is_admin=True, status="COMPLETED")
    except TaskError:
        denied = True
    chk("cross-tenant task update is denied (A cannot complete B's task)", denied)
    db.session.rollback()
    chk("B's task is unchanged after the denied attempt",
        Task.query.filter_by(tenant_id=tb.id).first().status != "COMPLETED")

    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== 3. Tenant A cannot DELETE tenant B records ===")
    from app.services.task_service import delete_task
    denied = False
    try:
        delete_task(tenant_id=ta.id, task_id=b_task.id, actor="admin-a")
    except TaskError:
        denied = True
    db.session.rollback()
    chk("cross-tenant task delete is denied", denied)
    chk("B's task still exists after the denied delete",
        Task.query.filter_by(tenant_id=tb.id, id=b_task.id).count() == 1)

    # ═══════════════════════════════════════════════════════════════════════
    print("\n=== 5. Log writes remain tenant-scoped ===")
    from app.services.log_service import log_message, save_conversation_message, log_lead_event

    log_message(PHONE, "inbound", "user", "hello A", tenant_id=ta.id)
    save_conversation_message(PHONE, "incoming", "hello A", tenant_id=ta.id)
    log_lead_event(PHONE, "COURSE_VIEWED", tenant_id=ta.id)
    chk("MessageLog write lands under tenant A only",
        MessageLog.query.filter_by(tenant_id=ta.id).count() == 1
        and MessageLog.query.filter_by(tenant_id=tb.id).count() == 0)
    chk("ConversationMessage write lands under tenant A only",
        ConversationMessage.query.filter_by(tenant_id=ta.id).count() == 1
        and ConversationMessage.query.filter_by(tenant_id=tb.id).count() == 0)
    chk("LeadEvent write lands under tenant A only",
        LeadEvent.query.filter_by(tenant_id=ta.id).count() == 1
        and LeadEvent.query.filter_by(tenant_id=tb.id).count() == 0)

    # Implicit resolution (tenant_id=None) must file under PRIMARY, never first-row
    log_lead_event("919000000003", "LEAD_CREATED", tenant_id=None)
    chk("implicit log write files under PRIMARY tenant, not first-row tenant",
        LeadEvent.query.filter_by(tenant_id=ta.id, phone="919000000003").count() == 1
        and LeadEvent.query.filter_by(tenant_id=tb.id, phone="919000000003").count() == 0)

# ═══════════════════════════════════════════════════════════════════════════
print("\n=== 4. Webhook tenant resolution ===")


def wa_payload(phone_number_id, from_number, text, wamid):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "type": "text", "id": wamid,
                      "text": {"body": text}}],
        "contacts": [{"profile": {"name": "WebhookUser"}}],
    }}]}]}


app.config["TESTING"] = True
with app.test_client() as client:
    with app.app_context():
        before_b = ConversationState.query.filter_by(tenant_id="aaa-tenant-b-first").count()

    # 4a. Message to tenant B's WABA number → state row under tenant B
    r = client.post("/webhook", json=wa_payload("PHONE_B", "919111111111", "hi", "wamid.iso.1"))
    with app.app_context():
        chk("webhook returns 200", r.status_code == 200)
        chk("message to PHONE_B creates state under tenant B",
            ConversationState.query.filter_by(
                phone="919111111111", tenant_id="aaa-tenant-b-first").count() == 1)
        chk("message to PHONE_B creates nothing under tenant A",
            ConversationState.query.filter_by(
                phone="919111111111", tenant_id="tenant-a-primary").count() == 0)

    # 4b. Unknown WABA phone id → dropped, no rows anywhere
    client.post("/webhook", json=wa_payload("PHONE_UNKNOWN", "919222222222", "hi", "wamid.iso.2"))
    with app.app_context():
        chk("unknown WABA id creates no state row",
            ConversationState.query.filter_by(phone="919222222222").count() == 0)

    # 4c. Suspended tenant → dropped
    client.post("/webhook", json=wa_payload("PHONE_S", "919333333333", "hi", "wamid.iso.3"))
    with app.app_context():
        chk("suspended tenant's message is dropped",
            ConversationState.query.filter_by(phone="919333333333").count() == 0)

# ═══════════════════════════════════════════════════════════════════════════
passed = sum(1 for _, ok in results if ok)
print("\n" + "=" * 60)
print(f"RESULT: {passed}/{len(results)} tenant-isolation checks passed")
if passed == len(results):
    print("ALL TENANT ISOLATION TESTS PASS")
sys.exit(0 if passed == len(results) else 1)
