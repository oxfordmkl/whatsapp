import time
import threading
from datetime import datetime, timedelta
from app.state import follow_up_queue, conversation_state
from app.services.whatsapp_service import send_text
from app.services.crm_service import update_lead_status

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
    now = datetime.now()
    for tmpl in FOLLOWUP_TEMPLATES:
        follow_up_queue.append({
            "phone":   phone,
            "name":    name,
            "send_at": now + timedelta(hours=tmpl["hours"]),
            "message": tmpl["message"].format(name=name),
            "day":     tmpl["day"],
            "done":    False,
        })
    print(f"📅 Follow-ups scheduled for {name}")

def _followup_worker():
    while True:
        try:
            now = datetime.now()
            for item in follow_up_queue:
                if item["done"] or now < item["send_at"]:
                    continue
                # Skip if lead was active in the last 6 hours
                st = conversation_state.get(item["phone"], {})
                last = st.get("last_msg", "")
                if last:
                    delta = (now - datetime.fromisoformat(last)).total_seconds()
                    if delta < 21_600:
                        item["done"] = True
                        print(f"⏭️  Follow-up skipped — {item['name']} recently active")
                        continue
                send_text(item["phone"], item["message"])
                threading.Thread(
                    target=update_lead_status,
                    args=(item["phone"], f"Follow-up Day {item['day']} Sent"),
                ).start()
                item["done"] = True
                print(f"📤 Follow-up Day {item['day']} → {item['name']}")
        except Exception as e:
            print(f"⚠️  Follow-up worker error: {e}")
        time.sleep(300)

threading.Thread(target=_followup_worker, daemon=True).start()
print("✅ Follow-up scheduler started")
