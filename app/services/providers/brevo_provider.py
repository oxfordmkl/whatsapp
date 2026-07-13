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

            if response.status_code >= 400:
                logging.error(
                    "Brevo HTTP Failure (%s): %s",
                    response.status_code,
                    response.text
                )
                return False

            return True

        except requests.exceptions.RequestException as e:
            logging.exception("Brevo request failed")
            return False
