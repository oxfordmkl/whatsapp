"""Phase 16.5A6-J — deterministic mutation tests for the ORM adapters (ADR-020).

Covers the blocking defect found by the Phase 16.5A6-LA audit: the `course`
adapter returned a stale Offering.name after backfill because
`_sync_offering_link` was a no-op.

Every test drives the adapter through `setattr(row, field, value)` — byte-for-byte
what `app/state.py:49 _db_save` does — so the tests exercise the real production
write path rather than a proxy for it.

Runs on an isolated in-memory SQLite database. Touches no production data.
No pytest dependency (not installed in this repo).

    python tests/test_adapter_sync_16_5a6j.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Config import requires these; values are irrelevant — the tests use SQLite.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ADMIN_KEY", "test_admin_key_not_a_secret_x9")
os.environ.setdefault("SECRET_KEY", "test_secret_key_not_a_secret_x9")
os.environ.setdefault("BROADCAST_API_KEY", "test_broadcast_key_not_a_secret_x9")
os.environ.setdefault("WABA_ENCRYPTION_KEY",
                      "FZsAc8GY_ayHq0cAxKXMMlUvSbJO2hKhpZOdGnaxO18=")

from flask import Flask                                    # noqa: E402
from app.extensions import db                              # noqa: E402
from app.models import (                                   # noqa: E402
    ConversationOffering, ConversationState, Offering,
    PipelineDefinition, PipelineStage, Tenant,
)

CANON = ["new", "goal_selection", "course_recommendation", "course_viewed",
         "demo_time_ask", "demo_date_ask", "demo_booked", "offer_menu",
         "payment_pending", "enrolled", "not_sure", "done"]

_results = []


def check(name, condition, detail=""):
    _results.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    line = f"  [{status}]  {name}"
    if detail and not condition:
        line += f"\n           {detail}"
    print(line)
    return bool(condition)


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def seed(tenant_id, slug, courses):
    """Create a tenant with a compat pipeline, 12 stages, and Offerings."""
    db.session.add(Tenant(id=tenant_id, name=slug, slug=slug))
    db.session.flush()
    pipe = PipelineDefinition(tenant_id=tenant_id, internal_key="legacy_compat",
                              name="Legacy Compatibility Pipeline",
                              is_default=True, is_active=True)
    db.session.add(pipe)
    db.session.flush()
    for i, k in enumerate(CANON):
        db.session.add(PipelineStage(
            pipeline_id=pipe.id, internal_key=k, display_name=k,
            stage_category="won" if k == "enrolled" else "open",
            order_index=i, is_entry=(k == "new"),
            is_terminal=k in ("enrolled", "done"), is_active=True))
    for c in courses:
        db.session.add(Offering(tenant_id=tenant_id,
                                internal_key=c.lower().replace(" ", "_")[:50],
                                name=c, is_active=True))
    db.session.flush()
    return pipe


def make_lead(tenant_id, pipe, phone, course, stage="new", linked=True):
    """Create a lead; when linked=True simulate the backfill (bridge + link)."""
    lead = ConversationState(phone=phone, tenant_id=tenant_id, stage=stage,
                             course=course)
    db.session.add(lead)
    db.session.flush()
    if linked:
        if course:
            off = Offering.query.filter_by(tenant_id=tenant_id, name=course).first()
            if off:
                db.session.add(ConversationOffering(
                    conversation_state_id=lead.id, offering_id=off.id))
        lead.pipeline_stage_id = PipelineStage.query.filter_by(
            pipeline_id=pipe.id, internal_key=stage).first().id
    db.session.commit()
    return lead


def write(lead, field, value):
    """Exactly what state.py:49 _db_save does, then force a fresh read."""
    setattr(lead, field, value)
    db.session.commit()
    db.session.expire_all()
    return ConversationState.query.filter_by(phone=lead.phone,
                                             tenant_id=lead.tenant_id).first()


def bridges(lead):
    return (ConversationOffering.query
            .filter_by(conversation_state_id=lead.id)
            .order_by(ConversationOffering.id).all())


def main():
    app = make_app()
    with app.app_context():
        db.create_all()
        # 8 Offerings exist (as backfill would create). GST & Payroll and
        # DCA Fast Track deliberately DO NOT — the bot can still assign them.
        pipe = seed("t1", "oxford-computers", [
            "PGDCA", "Python Programming", "AIDM Digital Marketing",
            "SAP Financial Accounting", "Computer Teacher Training",
            "Corporate Business Accounting", "Word Processing & Data Entry",
            "Professional Web Designing"])

        print("=" * 74)
        print("T1 — ADAPTER ROUND-TRIP, GATE OPEN (the original defect)")
        print("=" * 74)
        lead = make_lead("t1", pipe, "911", "PGDCA", "new")
        r = write(lead, "stage", "demo_booked")
        check("stage  write->read", r.stage == "demo_booked", f"got {r.stage!r}")
        r = write(r, "course", "Python Programming")
        check("course write->read", r.course == "Python Programming",
              f"got {r.course!r} (legacy={r._course!r}) — THE REGRESSION")
        r = write(r, "offer_course", "NEWOFFER")
        check("offer_course write->read", r.offer_course == "NEWOFFER",
              f"got {r.offer_course!r}")
        r = write(r, "batch_time", "Evening (6-8 PM)")
        check("batch_time write->read", r.batch_time == "Evening (6-8 PM)",
              f"got {r.batch_time!r}")

        print()
        print("=" * 74)
        print("T2 — MULTIPLE SUCCESSIVE COURSE WRITES")
        print("=" * 74)
        seq = ["PGDCA", "Python Programming", "AIDM Digital Marketing",
               "PGDCA", "SAP Financial Accounting"]
        ok = True
        for c in seq:
            r = write(r, "course", c)
            good = (r.course == c and r._course == c)
            ok &= good
            print(f"     wrote {c!r:32s} read {r.course!r:32s} "
                  f"{'ok' if good else 'STALE'}")
        check("5 successive course writes all round-trip", ok)
        check("bridge count stays exactly 1 after churn", len(bridges(r)) == 1,
              f"got {len(bridges(r))} bridges")

        print()
        print("=" * 74)
        print("T3 — BRIDGE SYNCHRONISATION")
        print("=" * 74)
        r = write(r, "course", "Python Programming")
        bl = bridges(r)
        target = db.session.get(Offering, bl[0].offering_id) if bl else None
        check("bridge repointed to the new Offering",
              target is not None and target.name == "Python Programming",
              f"bridge -> {target.name if target else None!r}")
        check("bridge Offering.name is byte-identical to legacy course",
              target is not None and target.name == r._course)

        print()
        print("=" * 74)
        print("T4 — DUPLICATE PREVENTION")
        print("=" * 74)
        for _ in range(5):
            r = write(r, "course", "PGDCA")     # same value repeatedly
        check("idempotent rewrite creates no duplicate bridges",
              len(bridges(r)) == 1, f"got {len(bridges(r))} bridges")
        check("uq_conv_offering never violated", r.course == "PGDCA")
        # Pre-existing duplicate bridges must be collapsed, not multiplied.
        extra = Offering.query.filter_by(tenant_id="t1",
                                         name="AIDM Digital Marketing").first()
        db.session.add(ConversationOffering(conversation_state_id=r.id,
                                            offering_id=extra.id))
        db.session.commit()
        check("precondition: 2 bridges present", len(bridges(r)) == 2)
        r = write(r, "course", "Python Programming")
        check("stale duplicate bridges collapsed to 1", len(bridges(r)) == 1,
              f"got {len(bridges(r))}")
        check("course still correct after collapse",
              r.course == "Python Programming", f"got {r.course!r}")

        print()
        print("=" * 74)
        print("T5 — EMPTY COURSE (router.py:219  st['course'] = '')")
        print("=" * 74)
        r = write(r, "course", "")
        check("empty course reads back as ''", r.course == "", f"got {r.course!r}")
        check("bridge removed on empty course", len(bridges(r)) == 0,
              f"got {len(bridges(r))} bridges")
        r = write(r, "course", "PGDCA")
        check("recovers from empty -> real course", r.course == "PGDCA",
              f"got {r.course!r}")

        print()
        print("=" * 74)
        print("T6 — COURSE WITH NO OFFERING (bot can assign 10, only 8 seeded)")
        print("=" * 74)
        r = write(r, "course", "GST & Payroll")
        check("no-Offering course falls back to legacy column",
              r.course == "GST & Payroll", f"got {r.course!r}")
        check("stale bridge removed so fallback engages",
              len(bridges(r)) == 0, f"got {len(bridges(r))} bridges")
        r = write(r, "course", "DCA Fast Track")
        check("second no-Offering course also correct",
              r.course == "DCA Fast Track", f"got {r.course!r}")
        r = write(r, "course", "PGDCA")
        check("returns to a real Offering cleanly", r.course == "PGDCA"
              and len(bridges(r)) == 1)

        print()
        print("=" * 74)
        print("T7 — TENANT ISOLATION (both tenants own an Offering named 'PGDCA')")
        print("=" * 74)
        pipe2 = seed("t2", "other-tenant", ["PGDCA", "Python Programming"])
        db.session.commit()
        lead2 = make_lead("t2", pipe2, "922", "PGDCA", "new")
        r2 = write(lead2, "course", "Python Programming")
        b2 = bridges(r2)
        off2 = db.session.get(Offering, b2[0].offering_id)
        check("t2 lead links to t2's Offering, never t1's",
              off2.tenant_id == "t2", f"linked to tenant {off2.tenant_id!r}")
        check("t2 course correct", r2.course == "Python Programming")
        # t1 lead must be unaffected
        r = ConversationState.query.filter_by(phone="911", tenant_id="t1").first()
        check("t1 lead unaffected by t2 activity", r.course == "PGDCA",
              f"got {r.course!r}")
        b1 = bridges(r)
        off1 = db.session.get(Offering, b1[0].offering_id)
        check("t1 bridge still points at t1's Offering", off1.tenant_id == "t1")

        print()
        print("=" * 74)
        print("T8 — GATE CLOSED (pre-backfill behaviour must be unchanged)")
        print("=" * 74)
        unlinked = make_lead("t1", pipe, "933", "PGDCA", "new", linked=False)
        check("precondition: pipeline_stage_id IS NULL",
              unlinked.pipeline_stage_id is None)
        u = write(unlinked, "course", "Python Programming")
        check("unlinked lead round-trips via legacy column",
              u.course == "Python Programming", f"got {u.course!r}")
        check("no bridge created while gate is closed", len(bridges(u)) == 0,
              f"got {len(bridges(u))} bridges — sync must not activate pre-backfill")

        print()
        print("=" * 74)
        print("T9 — ADR-018: is_admitted never touched by course/stage writes")
        print("=" * 74)
        r = ConversationState.query.filter_by(phone="911", tenant_id="t1").first()
        r.is_admitted = True
        db.session.commit()
        r = write(r, "course", "Python Programming")
        check("is_admitted survives a course write", r.is_admitted is True)
        r = write(r, "stage", "enrolled")
        check("is_admitted survives a stage write to 'enrolled'",
              r.is_admitted is True)
        r = write(r, "stage", "new")
        check("is_admitted survives a stage write away from 'enrolled'",
              r.is_admitted is True, "ADR-018: stage must never drive is_admitted")

    print()
    print("=" * 74)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failed = [n for n, ok, _ in _results if not ok]
    print(f"RESULT: {passed}/{total} checks passed")
    if failed:
        print()
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    print("ALL ADAPTER MUTATION TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
