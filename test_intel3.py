import sys
sys.path.append(r'd:\oxford\2026\theoxfordedu-main\whatsapp API\oxford-whatsapp_2')

# Mock flask
import sys, types
mock_flask = types.ModuleType("flask")
mock_flask.Blueprint = lambda *args, **kwargs: type("Blueprint", (), {"route": lambda *a, **k: lambda f: f})()
mock_flask.request = type("request", (), {"args": {}})()
mock_flask.jsonify = lambda *args, **kwargs: {}
mock_flask.render_template = lambda *args, **kwargs: ""
mock_flask.redirect = lambda *args, **kwargs: ""
sys.modules["flask"] = mock_flask

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
print("LIVE DISK ACTION:", intel["recommended_action"])
