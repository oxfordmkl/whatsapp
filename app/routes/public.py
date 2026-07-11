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
