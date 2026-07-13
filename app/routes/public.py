from flask import Blueprint, request, render_template, redirect, url_for, flash
import re
import uuid
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash
from app.extensions import db
from app.models import Tenant, User

public_bp = Blueprint('public', __name__)

def generate_slug(name):
    # Basic slugify
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    if not slug:
        slug = uuid.uuid4().hex[:8]
    return slug

@public_bp.route("/", methods=["GET"])
def index():
    return render_template("public/index.html")

@public_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        business_name = request.form.get("business_name", "").strip()
        admin_name = request.form.get("admin_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        industry = request.form.get("industry", "Education").strip()
        password = request.form.get("password", "")

        if not (business_name and admin_name and email and password):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("public.register"))

        # Duplicate email protection
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("This email is already registered. Please login or use a different email.", "danger")
            return redirect(url_for("public.register"))

        # Generate slug and handle duplicates
        slug = generate_slug(business_name)
        existing_slug = Tenant.query.filter_by(slug=slug).first()
        if existing_slug:
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        try:
            # Single transaction creation
            new_tenant = Tenant(
                name=business_name,
                slug=slug,
                status='PENDING',
                industry=industry,
                billing_email=email
            )
            db.session.add(new_tenant)
            db.session.flush() # flush to get new_tenant.id
            
            # Wait, the prompt says "Admin Name" -> the user's username?
            # Phase 13-A2 Identity Schema notes: "username uniqueness is now enforced per-tenant... username=admin_name"
            # It's better to use a derived username from admin_name or just admin_name
            username = admin_name if admin_name else "admin"

            new_user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role='ADMIN',
                tenant_id=new_tenant.id,
                is_active=True
            )
            db.session.add(new_user)
            db.session.commit()
            
            # Phase 15C.5-B: Dispatch verification email gracefully
            from app.services.email_service import email_service
            try:
                success = email_service.send_verification_email(user_email=email, user_name=admin_name)
                if not success:
                    import logging
                    logging.error("Failed to dispatch verification email to newly registered user.")
            except Exception as e:
                import logging
                logging.error(f"Email dispatch exception during registration: {str(e)}")
                
            flash("Registration successful. Check your email to verify your account.", "success")
            return redirect(url_for("public.pending"))
            
        except IntegrityError:
            db.session.rollback()
            flash("An unexpected error occurred during registration. Please try again.", "danger")
            return redirect(url_for("public.register"))

    return render_template("public/register.html")

@public_bp.route("/pending", methods=["GET"])
def pending():
    return render_template("public/pending.html")

@public_bp.route("/verify-email/<token>", methods=["GET"])
def verify_email(token):
    from app.services.email_service import email_service
    from itsdangerous import SignatureExpired, BadSignature
    from datetime import datetime, timezone
    from flask import current_app
    import logging
    
    max_age = current_app.config.get("VERIFY_EMAIL_EXPIRY_SECONDS", 86400)
    
    try:
        email = email_service.verify_token(token, max_age=max_age)
    except SignatureExpired:
        flash("The verification link has expired. Please request a new one.", "warning")
        return redirect(url_for('admin.crm_login'))
    except BadSignature:
        flash("Invalid verification link.", "danger")
        return redirect(url_for('admin.crm_login'))
    except Exception as e:
        logging.error(f"Verification token failure: {str(e)}")
        flash("An error occurred during verification.", "danger")
        return redirect(url_for('admin.crm_login'))
        
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin.crm_login'))
        
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Your email has been successfully verified! You may now log in if your account is approved.", "success")
    else:
        flash("Your email is already verified.", "info")
        
        
    return redirect(url_for('admin.crm_login'))

@public_bp.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    import logging
    from app.services.email_service import email_service
    
    if request.method == "POST":
        ip = get_client_ip()
        email = request.form.get("email", "").strip().lower()
        
        # Rate limit: 3 per IP per 15 mins (900s)
        if not check_rate_limit(f"resend_ip_{ip}", 3, 900):
            return "Too many requests. Please try again later.", 429
            
        if email:
            # Rate limit: 3 per Email per 15 mins
            if not check_rate_limit(f"resend_email_{email}", 3, 900):
                return "Too many requests. Please try again later.", 429
                
            user = User.query.filter_by(email=email, role="ADMIN").first()
            if user and user.email_verified_at is None:
                logging.info(f"RESEND_VERIFICATION_REQUESTED: User {user.id}")
                success = email_service.send_verification_email(user.email, user.username)
                if success:
                    logging.info(f"RESEND_VERIFICATION_EMAIL_SENT: User {user.id}")
                else:
                    logging.error(f"RESEND_VERIFICATION_EMAIL_FAILED: User {user.id}")
            else:
                # User not found or already verified - prevent enumeration
                pass
                
        flash("If an unverified account with that email exists, a verification email has been sent.", "success")
        return redirect(url_for("admin.crm_login"))
        
    return render_template("public/resend_verification.html")

# ── Lightweight In-Memory Rate Limiter ───────────────────────────────────────
import time
_RATE_LIMITS = {}

def check_rate_limit(key: str, max_reqs: int, window_seconds: int) -> bool:
    """Returns True if allowed, False if limit exceeded."""
    now = time.time()
    if key not in _RATE_LIMITS:
        _RATE_LIMITS[key] = []
    
    _RATE_LIMITS[key] = [t for t in _RATE_LIMITS[key] if now - t < window_seconds]
    
    if len(_RATE_LIMITS[key]) >= max_reqs:
        return False
        
    _RATE_LIMITS[key].append(now)
    return True

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

# ── Password Policy Validation ────────────────────────────────────────────────
def validate_password(password: str) -> bool:
    if not password or len(password) < 8 or len(password) > 128:
        return False
    if password.startswith(" ") or password.endswith(" "):
        return False
    if not re.search(r"[A-Z]", password):
        return False
    if not re.search(r"[a-z]", password):
        return False
    if not re.search(r"[0-9]", password):
        return False
    if not re.search(r"[^A-Za-z0-9]", password):
        return False
    return True

# ── Password Reset Routes ─────────────────────────────────────────────────────

@public_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    import logging
    from app.services.email_service import email_service
    
    if request.method == "POST":
        ip = get_client_ip()
        email = request.form.get("email", "").strip().lower()
        
        # Rate limit: 3 per IP per 15 mins (900s)
        if not check_rate_limit(f"fp_ip_{ip}", 3, 900):
            return "Too many requests. Please try again later.", 429
            
        if email:
            # Rate limit: 3 per Email per 15 mins
            if not check_rate_limit(f"fp_email_{email}", 3, 900):
                return "Too many requests. Please try again later.", 429
                
            user = User.query.filter_by(email=email).first()
            if user:
                logging.info(f"PASSWORD_RESET_REQUESTED: User {user.id}")
                success = email_service.send_password_reset_email(user.email, user.id, user.password_hash)
                if success:
                    logging.info(f"PASSWORD_RESET_EMAIL_SENT: User {user.id}")
                else:
                    logging.error(f"PASSWORD_RESET_EMAIL_DISPATCH_FAILED: User {user.id}")
            else:
                # To prevent enumeration, we act exactly the same but do nothing
                pass
                
        flash("If an account with that email exists, a password reset link has been sent.", "success")
        return redirect(url_for("admin.crm_login"))
        
    return render_template("public/forgot_password.html")

@public_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    import logging
    from app.services.email_service import email_service
    from itsdangerous import SignatureExpired, BadSignature
    
    ip = get_client_ip()
    # Rate limit: 5 per IP per hour (3600s)
    if not check_rate_limit(f"rp_ip_{ip}", 5, 3600):
        return "Too many requests. Please try again later.", 429

    try:
        payload = email_service.verify_password_reset_token(token, max_age=3600)
        user_id, hash_suffix = payload[0], payload[1]
    except SignatureExpired:
        logging.info("PASSWORD_RESET_EXPIRED")
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("public.forgot_password"))
    except BadSignature:
        logging.info("PASSWORD_RESET_INVALID_TOKEN")
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("public.forgot_password"))
    except Exception as e:
        logging.error("PASSWORD_RESET_INVALID_TOKEN (exception)")
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("public.forgot_password"))
        
    user = User.query.get(user_id)
    if not user:
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("public.forgot_password"))
        
    # Replay protection: Check if current hash suffix matches the token
    if user.password_hash[-12:] != hash_suffix:
        logging.info("PASSWORD_RESET_REPLAY")
        flash("This reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("public.forgot_password"))
        
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("public/reset_password.html", token=token)
            
        if not validate_password(password):
            flash("Password must be 8-128 chars, include upper, lower, number, special char, and no leading/trailing spaces.", "danger")
            return render_template("public/reset_password.html", token=token)
            
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        
        logging.info(f"PASSWORD_RESET_COMPLETED: User {user.id}")
        flash("Your password has been successfully reset. Please log in with your new password.", "success")
        return redirect(url_for("admin.crm_login"))
        
    return render_template("public/reset_password.html", token=token)
