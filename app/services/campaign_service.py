import threading
import time
from flask import current_app
from app.services.whatsapp_service import send_whatsapp_message
from app.services.log_service import save_conversation_message

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
                # Execute send
                success = send_whatsapp_message(phone, full_message)
                
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
