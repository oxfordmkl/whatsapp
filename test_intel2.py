EVENT_SCORE_MAP = {
    "LEAD_CREATED": 2,
    "FIRST_MESSAGE_RECEIVED": 3,
    "AI_RESPONSE_SENT": 5,
    "COURSE_VIEWED": 10,
    "PLACEMENT_ASKED": 15,
    "FEES_REQUESTED": 20,
    "DEMO_REQUESTED": 25,
    "PAYMENT_PENDING": 30
}

def calculate_lead_intelligence(manual_score, events):
    unique_event_types = set(e.event_type for e in events)
    auto_score = sum(EVENT_SCORE_MAP.get(et, 0) for et in unique_event_types)
    final_score = min((manual_score or 0) + auto_score, 100)
    
    if final_score >= 80:
        temperature = "HOT"
    elif final_score >= 50:
        temperature = "WARM"
    else:
        temperature = "COLD"
        
    if "PAYMENT_PENDING" in unique_event_types:
        action = "Payment Follow-up"
    elif "DEMO_REQUESTED" in unique_event_types:
        action = "Send Demo"
    elif "FEES_REQUESTED" in unique_event_types:
        action = "Send Fees"
    elif "PLACEMENT_ASKED" in unique_event_types:
        action = "Discuss Placement"
    elif final_score >= 80:
        action = "Call Today"
    elif "LEAD_CREATED" in unique_event_types and final_score <= 15:
        action = "Qualify Lead"
    else:
        action = "Admission Follow-up"
        
    return {
        "final_score": final_score,
        "temperature": temperature,
        "recommended_action": action,
        "_events": list(unique_event_types)
    }

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
