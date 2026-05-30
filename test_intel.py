import sys
sys.path.append(r'd:\oxford\2026\theoxfordedu-main\whatsapp API\oxford-whatsapp_2')

from app.routes.admin import calculate_lead_intelligence

class DummyEvent:
    def __init__(self, t):
        self.event_type = t

events = [
    DummyEvent("LEAD_CREATED"),
    DummyEvent("FIRST_MESSAGE_RECEIVED"),
    DummyEvent("AI_RESPONSE_SENT")
]

intel = calculate_lead_intelligence(0, events)
print("ACTION:", intel["recommended_action"])
print("FINAL SCORE:", intel["final_score"])
print("EVENTS:", intel["_events"])
