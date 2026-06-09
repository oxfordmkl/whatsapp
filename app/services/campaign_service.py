import threading
import time
from flask import current_app
from app.services.whatsapp_service import send_text
from app.services.log_service import save_conversation_message
from app.models import ConversationState

def _campaign_worker(app_ref, audience_phones, message_text, campaign_name):
    """
    Background worker to send campaigns sequentially with a 1.5s delay.
    Runs inside a dedicated app context.
    """
    with app_ref.app_context():
        # 1. Format the final message body safely according to constraints
        full_message = f"[CAMPAIGN: {campaign_name}]\n\n{message_text}"
        
        # 2. Iterate through phones
        for phone in audience_phones:
            try:
                # Phase 11-D1 Task D: Opt-Out Check
                state = ConversationState.query.filter_by(phone=phone).first()
                if state and getattr(state, 'is_opted_out', False):
                    print(f"🚫 Campaign skipped — {phone} opted out")
                    continue

                # Execute send
                response = send_text(phone, full_message)
                success = response.status_code == 200
                
                # Log to CRM if successful
                if success:
                    save_conversation_message(
                        phone=phone,
                        direction="outgoing",
                        message=full_message,
                        message_type="text",
                        source="campaign"
                    )
            except Exception as e:
                # Fail gracefully for individual leads without breaking the campaign
                print(f"⚠️ Campaign worker error for {phone}: {e}")
                pass
                
            # 3. Mandatory 1.5s rate limit
            time.sleep(1.5)

def start_campaign(audience_phones, message_text, campaign_name):
    """
    Validates audience size and spawns the campaign worker thread.
    """
    if len(audience_phones) > 100:
        raise ValueError("Campaign audience exceeds maximum limit of 100 leads.")
        
    app_ref = current_app._get_current_object()
    
    thread = threading.Thread(
        target=_campaign_worker,
        args=(app_ref, audience_phones, message_text, campaign_name),
        daemon=True
    )
    thread.start()
