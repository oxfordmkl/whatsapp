from unittest.mock import patch, MagicMock
from app import create_app
from app.extensions import db

app = create_app()

with app.app_context():
    with patch('app.routes.admin.send_text') as mock_send_text:
        with patch('app.routes.admin.ConversationState') as mock_db:
            with patch('app.services.log_service.save_conversation_message') as mock_save:
                with patch('app.services.log_service.log_lead_event') as mock_log:
                    # Mock successful WhatsApp send
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_send_text.return_value = mock_response
                    
                    # Mock DB lead lookup
                    mock_lead = MagicMock()
                    mock_lead.assigned_staff = 'Test Staff'
                    mock_db.query.filter_by.return_value.first.return_value = mock_lead
                    
                    # Test Request
                    with app.test_client() as client:
                        response = client.post(
                            '/crm/lead/919999999999/send?key=oxford_admin_2026',
                            data={'manual_message': 'Test Message'}
                        )
                        
                        print("Response Status:", response.status_code)
                        print("Redirect Location:", response.location)
                        print("Save called:", mock_save.called)
                        print("Log event called:", mock_log.called)
