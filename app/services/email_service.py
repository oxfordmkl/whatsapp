import logging
from flask import current_app, url_for, render_template
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from app.services.providers.brevo_provider import BrevoProvider

class EmailService:
    """
    Business logic layer for email communications.
    Delegates transport to provider implementations.
    """
    
    def __init__(self, app=None):
        if app:
            self.init_app(app)
            
    def init_app(self, app):
        self.provider = app.config.get("EMAIL_PROVIDER", "brevo")
        self.secret_key = app.config["SECRET_KEY"]
        self.app_url = app.config.get("APP_URL", "http://localhost:5000").rstrip('/')
        
        # Initialize providers
        if self.provider == "brevo":
            self.email_client = BrevoProvider(
                api_key=app.config.get("BREVO_API_KEY", ""),
                sender_email=app.config.get("BREVO_SENDER_EMAIL", "noreply@oxfordedu.com"),
                sender_name=app.config.get("BREVO_SENDER_NAME", "Oxford CRM"),
                timeout=app.config.get("EMAIL_TIMEOUT_SECONDS", 5)
            )
        else:
            self.email_client = None

    def get_serializer(self):
        return URLSafeTimedSerializer(self.secret_key)

    def generate_verification_token(self, email: str) -> str:
        """Generates a signed, stateless token."""
        return self.get_serializer().dumps(email, salt='email-verify')

    def verify_token(self, token: str, max_age: int) -> str:
        """
        Validates the token. Returns email if valid.
        Raises SignatureExpired or BadSignature on failure.
        """
        return self.get_serializer().loads(token, salt='email-verify', max_age=max_age)

    def send_verification_email(self, user_email: str, user_name: str) -> bool:
        """
        Constructs and dispatches the verification email.
        """
        if not self.email_client:
            logging.error("Email client not configured.")
            return False
            
        token = self.generate_verification_token(user_email)
        
        # We must generate the absolute URL using the configured APP_URL to ensure it works anywhere
        verify_link = f"{self.app_url}/verify-email/{token}"
        
        try:
            html_content = render_template(
                'email/verify_email.html', 
                verify_link=verify_link,
                user_name=user_name
            )
        except Exception as e:
            logging.error(f"Template rendering failed: {str(e)}")
            return False

        return self.email_client.send_email(
            to_email=user_email,
            to_name=user_name,
            subject="Verify your Oxford CRM account",
            html_content=html_content
        )

# Global instance
email_service = EmailService()
