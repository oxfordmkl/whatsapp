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


#: Maximum stored length — matches User.phone (String(20)). A longer input is
#: refused rather than truncated: a silently cut number is a WRONG number, and
#: a wrong number is worse than an absent one.
_PHONE_MAX_LEN = 20


def normalize_user_phone(raw):
    """Normalise a CRM user's phone to a stored digit string. "" if unusable.

    Deliberately NOT admin.normalize_lead_phone(). That function serves the
    CUSTOMER domain and unconditionally prefixes "91", which is correct there:
    an Indian education business's leads are domestic, and the rule exists so a
    hand-typed walk-in collides with the same row as an inbound WhatsApp
    message. Applying it here would silently turn a tenant owner's "+1 555 012
    3456" into "915550123456" -- a real, different, Indian number. Storing a
    corrupted identity is worse than storing none, so this rule differs in
    exactly one respect:

        input begins with "+"  -> already international; keep the digits as-is
        otherwise              -> domestic: strip leading zeros, prefix 91

    The "+" case is not hypothetical: the registration form's own placeholder
    reads "+91 98765 43210", so the form actively invites that spelling.

    The domestic branch is byte-for-byte the existing rule, so a user who
    enters a bare Indian number is stored in the SAME form as the lead tables
    use. That matters for a later phase that may need to relate the two.

    NOT promoted to a service module yet: registration is the only writer in
    this phase. The phase that adds phone login should move it, with its tests.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    international = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if international:
        # Trust the caller's country code. Leading zeros are not stripped:
        # in an E.164 number there are none to strip, and removing a digit
        # from an explicit international number would corrupt it.
        return digits if len(digits) <= _PHONE_MAX_LEN else ""
    digits = digits.lstrip("0")
    if not digits:
        return ""
    if not digits.startswith("91"):
        digits = "91" + digits
    return digits if len(digits) <= _PHONE_MAX_LEN else ""

@public_bp.route("/", methods=["GET"])
def index():
    return render_template("public/index.html")

#: Phase RC2.5.17 Gate A.1: registration-abuse limit.
#:
#: /register was the only unauthenticated write surface on the platform with NO
#: throttle at all, while /resend-verification, /forgot-password and
#: /reset-password have had one since 15C.5-B. Each accepted POST creates a
#: Tenant, a User, a sales pipeline, a settings blob and an outbound
#: verification email, in one transaction -- the most expensive anonymous
#: request the application serves.
#:
#: Deliberately generous. This is an ABUSE ceiling, not a product rule: a real
#: business registers once, while a script can register thousands of times. A
#: tight limit would start refusing legitimate retries (a mistyped password, a
#: browser back-button resubmit) for no security gain, and several colleagues
#: signing up from one office NAT must not lock each other out.
_REGISTER_MAX_PER_IP = 5
_REGISTER_WINDOW_SECONDS = 3600


@public_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        # ── Throttle FIRST, before any database work ──────────────────────
        # Placed above form parsing and every query so a flood costs one dict
        # lookup rather than a duplicate-email SELECT, a slug SELECT, a
        # password hash and a five-table transaction.
        #
        # Keyed on IP ALONE, deliberately not on email: an attacker chooses the
        # email freely, so an email-keyed bucket would be reset on every
        # request and enforce nothing. IP is the only identity an anonymous
        # caller does not fully control -- see the trust boundary in
        # audit_service.request_ip().
        #
        # An unresolvable IP is NOT throttled. "" would otherwise become a
        # single shared bucket, and one client with a malformed header could
        # lock out every other unknown-IP visitor. Failing open for that narrow
        # case is the lesser harm; it cannot be reached through Railway's edge,
        # which always supplies an address.
        _ip = get_client_ip()
        if _ip and not check_rate_limit(f"register_ip_{_ip}",
                                        _REGISTER_MAX_PER_IP,
                                        _REGISTER_WINDOW_SECONDS):
            # Byte-identical in shape to the three existing limiters
            # (public.py:298, :392, :425): a plain body with 429, no flash and
            # no redirect. Matching them matters -- a different shape here
            # would be a second convention for the same condition.
            #
            # The message reveals nothing about any account: it is the same
            # whether the email exists, is new, or was never valid, so the
            # RC2.5.15 enumeration fix is not weakened.
            return "Too many requests. Please try again later.", 429

        business_name = request.form.get("business_name", "").strip()
        admin_name = request.form.get("admin_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        industry = request.form.get("industry", "Education").strip()
        password = request.form.get("password", "")

        if not (business_name and admin_name and email and password):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("public.register"))

        # Phase RC2.5.15: normalise BEFORE any use. An empty result means the
        # field was blank or held nothing usable; phone is optional, so that
        # stores NULL rather than rejecting an otherwise valid registration.
        phone_normalised = normalize_user_phone(phone)

        # Duplicate email protection.
        #
        # RC2.5.15: this used to answer "This email is already registered",
        # which told any visitor, unauthenticated and unlimited, whether a
        # given address holds an account. /crm/login is already careful to
        # answer identically for a bad password and an unknown user; this
        # route handed back the fact that login refuses to.
        #
        # The response is now INDISTINGUISHABLE from a successful signup: same
        # flash, same redirect. The duplicate is still not created -- we simply
        # stop announcing why.
        #
        # Known trade-off, stated rather than hidden: a legitimate visitor who
        # forgot they had signed up now sees "check your email" and receives
        # nothing. Closing that properly means mailing the existing address
        # "you already have an account", which needs a new template and a new
        # email_service method -- out of this phase's scope. The enumeration
        # oracle is the security defect; the UX gap is a follow-up.
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Registration successful. Check your email to verify your account.", "success")
            return redirect(url_for("public.pending"))

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
                # Phase RC2.5.15: the form has collected this since 13-A2B and
                # the value was discarded for want of a column. "" -> None so
                # an absent number is NULL, not an empty string: two spellings
                # of "no phone" would defeat any future uniqueness rule.
                phone=phone_normalised or None,
                # phone_verified_at is deliberately NOT set. Supplying a number
                # at registration proves nothing about holding it, and no
                # verification channel exists yet. A later phase owns that.
                password_hash=generate_password_hash(password),
                role='ADMIN',
                tenant_id=new_tenant.id,
                is_active=True
            )
            db.session.add(new_user)

            # ── Phase 14D: provision the tenant IN THE SAME TRANSACTION ────
            # Registration previously committed here, leaving a tenant with no
            # sales pipeline and no settings row — signed up but unable to use
            # the product. provision_tenant() never commits, so it joins this
            # transaction: if it raises, the rollback below removes the Tenant
            # and User too. A half-provisioned tenant cannot exist.
            from app.services.tenant_provisioning_service import provision_tenant
            provision_tenant(new_tenant.id)

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
        except Exception:
            # Phase 14D: provisioning joined this transaction, so a failure
            # there must roll the WHOLE registration back. Previously only
            # IntegrityError was caught; anything else escaped as a 500 with
            # the session left dirty, which would have committed a partially
            # provisioned tenant on the next write in the same request.
            db.session.rollback()
            import logging
            logging.exception("Registration failed during tenant provisioning")
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
                
            # Phase RC2.5.6a (P1-7): STAFF are eligible too -- /crm/login admits
            # ADMIN and STAFF and requires a verified email from both, so a
            # STAFF account with no way to get a link could never sign in.
            # SUPER_ADMIN stays excluded; the response below is unchanged.
            user = User.query.filter(User.email == email,
                                     User.role.in_(("ADMIN", "STAFF"))).first()
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
    """Delegates to audit_service.request_ip(). ONE rule, not two.

    RC2.5.17 Gate A.1. This used to be its own one-liner --
        request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0]
    -- a second, slightly different implementation of the same decision that
    audit_service.request_ip() was already making for ~25 audit call sites.
    Two copies of a trust boundary is one too many: hardening either alone
    would have left the other trusting an unvalidated header, and they could
    drift so that the rate limiter and the audit log disagreed about who made
    a request. The trust boundary and its evidence are documented there.

    Returns "" when no usable address exists. A caller keying a rate limit MUST
    NOT treat "" as an identity -- every unknown client would share one bucket.
    """
    from app.services.audit_service import request_ip
    return request_ip()

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
