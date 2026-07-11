import requests
import logging

class BrevoProvider:
    """
    Direct REST API integration for Brevo.
    Does not use the Brevo SDK to minimize dependency bloat.
    """
    
    API_URL = "https://api.brevo.com/v3/smtp/email"
    
    def __init__(self, api_key: str, sender_email: str, sender_name: str, timeout: int = 5):
        self.api_key = api_key
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.timeout = timeout
        
    def send_email(self, to_email: str, to_name: str, subject: str, html_content: str) -> bool:
        """
        Sends an email using Brevo's REST API.
        Returns True if successful, False if the request failed.
        """
        if not self.api_key:
            logging.error("Brevo API Key not configured.")
            return False
            
        headers = {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json"
        }
        
        payload = {
            "sender": {"name": self.sender_name, "email": self.sender_email},
            "to": [{"email": to_email, "name": to_name}],
            "subject": subject,
            "htmlContent": html_content
        }
        
        try:
            response = requests.post(
                self.API_URL,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            # We explicitly do NOT log the payload or headers to prevent secret leakage
            logging.error(f"Brevo HTTP Failure: {str(e)}")
            return False
