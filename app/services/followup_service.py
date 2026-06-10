import time
import threading
from datetime import datetime, timedelta
from app.services.whatsapp_service import send_text, send_automation
from app.services.crm_service import update_lead_status

# Populated by init_followup_service() called from create_app()
_app = None
scheduler_started = False

FOLLOWUP_TEMPLATES = [
    {
        "day": 1,
        "hours": 24,
        "message": (
            "Hi {name} 😊 Aaliza here from The Oxford Computers.\n\n"
            "Course about alochichu nokkiyo?\n"
            "Confusion undenkil njan help cheyyam.\n\n"
            "Oru free demo class attend cheythal clarity varum 🎓\n"
            "*DEMO* reply cheythal book cheyyam."
        ),
    },
    {
        "day": 3,
        "hours": 72,
        "message": (
            "{name}, small reminder 😊\n\n"
            "Next batch starting soon aanu.\n"
            "Late aayal next batch wait cheyyendi varum.\n\n"
            "Ningalkku job-oriented course venel njan best option suggest cheyyam.\n"
            "*COURSES* / *DEMO* reply cheyyoo."
        ),
    },
    {
        "day": 7,
        "hours": 168,
        "message": (
            "{name}, last follow-up aanu 😊\n\n"
            "This batch-il free demo + EMI option available aanu.\n"
            "Seat limited aanu.\n\n"
            "Interested aanenkil *DEMO* or *VISIT* reply cheyyoo.\n"
            "All the best from The Oxford Computers 🎓"
        ),
    },
]


def schedule_followups(phone: str, name: str):
    """Write follow-up jobs to DB instead of an in-memory list."""
    from app.models import FollowUpJob
    from app.extensions import db
    # Phase 12-C1: Resolve tenant_id dynamically — never hardcoded
    from app.services.log_service import _get_default_tenant_id
    tenant_id = _get_default_tenant_id()

    now = datetime.now()
    for tmpl in FOLLOWUP_TEMPLATES:
        job = FollowUpJob(
            phone=phone,
            name=name,
            send_at=now + timedelta(hours=tmpl["hours"]),
            message=tmpl["message"].format(name=name),
            day=tmpl["day"],
            done=False,
            tenant_id=tenant_id,  # Phase 12-C1: Required after Phase 12-B migration
        )
        db.session.add(job)
    db.session.commit()
    print(f"📅 Follow-ups scheduled (DB) for {name}")


def _followup_worker():
    """
    Polls DB every 5 minutes for pending follow-up jobs.
    Runs in a daemon thread with its own Flask app context per cycle.
    """
    global scheduler_started
    while True:
        try:
            with _app.app_context():
                from app.models import FollowUpJob, ConversationState
                from app.extensions import db

                now = datetime.now()
                pending = (
                    FollowUpJob.query
                    .filter_by(done=False)
                    .filter(FollowUpJob.send_at <= now)
                    .all()
                )

                scheduler_started = True

                for job in pending:
                    try:
                        # Skip if lead was active in the last 6 hours
                        state_row = ConversationState.query.filter_by(phone=job.phone).first()
                        
                        # Phase 11-D1 Task D: Opt-Out Check
                        if state_row and getattr(state_row, 'is_opted_out', False):
                            job.done = True
                            db.session.commit()
                            print(f"🚫 Follow-up skipped — {job.name} opted out")
                            continue

                        if state_row and state_row.last_msg:
                            try:
                                last_dt = datetime.fromisoformat(state_row.last_msg)
                                if (now - last_dt).total_seconds() < 21_600:
                                    job.done = True
                                    db.session.commit()
                                    print(f"⏭️  Follow-up skipped — {job.name} recently active")
                                    continue
                            except ValueError:
                                pass  # Malformed datetime — proceed with sending

                        # Phase 11-D3B2: Automation Interceptor
                        name_to_use = job.name if job.name else "Student"
                        response = send_automation(job.phone, job.message, name=name_to_use)
                        if response.status_code != 200:
                            raise Exception(f"API Error {response.status_code}: {response.text}")

                        threading.Thread(
                            target=update_lead_status,
                            args=(job.phone, f"Follow-up Day {job.day} Sent"),
                        ).start()
                        # ── Log outbound followup message ──
                        from app.services.log_service import log_message, save_conversation_message
                        log_message(
                            phone=job.phone,
                            direction="outbound",
                            message_type="followup",
                            message_text=job.message,
                            meta_json=f'{{"day": {job.day}}}',
                        )
                        # Phase 10N-G Fix 2: Persist follow-up into CRM conversation timeline.
                        # App context is active (worker runs inside with _app.app_context()).
                        # Canonical direction value for conversation_message outbound = "outgoing".
                        save_conversation_message(
                            phone=job.phone,
                            direction="outgoing",
                            message=job.message,
                            message_type="text",
                            source="followup",
                        )
                        job.done = True
                        db.session.commit()
                        print(f"\U0001f4e4 Follow-up Day {job.day} \u2192 {job.name}")
                    
                    except Exception as e:
                        # Phase 11-D1 Task E & Phase 11-D2C/D3B2: Followup Failure Protection & Retry Backoff
                        print(f"⚠️  Follow-up failed for {job.name} ({job.phone}): {e}")
                        job.retry_count = (job.retry_count or 0) + 1
                        job.last_attempt_at = datetime.utcnow()
                        job.failure_reason = str(e)

                        if job.retry_count >= 3:
                            job.done = True # Prevent infinite retry
                            print(f"🛑 Follow-up permanently failed after 3 retries for {job.phone}")
                        else:
                            from datetime import timedelta
                            # Exponential backoff: push send_at forward by 15 mins * retry_count
                            job.send_at = datetime.utcnow() + timedelta(minutes=15 * job.retry_count)
                            print(f"⏳ Follow-up retrying at {job.send_at} (Attempt {job.retry_count}/3)")

                        db.session.commit()
                        from app.services.log_service import log_message
                        log_message(
                            phone=job.phone,
                            direction="outbound",
                            message_type="system",
                            message_text=f"Follow-up Day {job.day} failed: {e}",
                            meta_json=f'{{"day": {job.day}, "error": "api_failure", "retry_count": {job.retry_count}}}',
                        )

        except Exception as e:
            print(f"⚠️  Follow-up worker outer error: {e}")

        time.sleep(300)


def init_followup_service(app):
    """
    Called once from create_app().
    Stores the app reference so the worker thread can open its own app context.
    """
    global _app
    _app = app
    threading.Thread(target=_followup_worker, daemon=True).start()
    print("✅ Follow-up scheduler started (DB-backed)")
